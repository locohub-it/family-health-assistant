import base64
import json

import httpx
import pytest

from famiglia.consult import MAX_REPORTS, split_message
from famiglia.service import Service

from helpers import FakeAnswerer, FakeReader, appuntamento, altro, lab, referto

JPEG = b"\xff\xd8\xff fake jpeg"


@pytest.fixture
def make(tmp_path, root):
    def _make(answerer=None):
        answerer = answerer or FakeAnswerer()
        svc = Service(tmp_path / "data", "chiave-di-test", root, reader=FakeReader(), answerer=answerer)
        svc.settings.update({"documents_dir": "Documenti"})
        svc.mario = svc.users.add(111, "Mario Rossi", "papà")
        svc.anna = svc.users.add(222, "Anna Bianchi", "mamma")
        svc.luca = svc.users.add(333, "Luca Rossi", "figlio")
        svc.answerer = answerer
        return svc

    return _make


async def upload(svc, sender, extraction):
    svc.documents._gemini = FakeReader(extraction)
    return await svc.documents.process(sender, JPEG, "image/jpeg")


# --- split_message -------------------------------------------------------------


def test_short_text_is_not_split():
    assert split_message("ciao") == ["ciao"]
    assert split_message("") == []


def test_long_text_is_split_at_paragraphs_under_the_limit():
    text = "\n\n".join(f"Paragrafo {i} " + "x" * 90 for i in range(10))
    parts = split_message(text, limit=300)
    assert all(len(p) <= 300 for p in parts) and len(parts) > 1
    assert "\n\n".join(parts) == text  # tagliato solo a fine paragrafo, niente perso né spezzato a metà


def test_text_without_any_break_is_cut_hard():
    parts = split_message("y" * 950, limit=400)
    assert [len(p) for p in parts] == [400, 400, 150]


# --- Chi vede cosa -------------------------------------------------------------


async def test_a_user_sees_only_their_own_data(make):
    svc = make()
    await upload(svc, svc.mario, referto(results=[lab("Glicemia", "95")]))
    await upload(svc, svc.anna, referto(results=[lab("Ferritina", "20", "ng/mL", "15-150")]))
    context = svc.consultant.build_context(svc.consultant.accessible_patients(svc.anna))
    assert "Ferritina" in context and "Glicemia" not in context and "Mario" not in context


async def test_a_user_also_sees_family_members_whose_documents_they_sent(make):
    svc = make()
    await upload(svc, svc.luca, referto(patient="Mario Rossi", results=[lab("Glicemia", "95")]))  # Luca carica per papà
    assert [u.name for u in svc.consultant.accessible_patients(svc.luca)] == ["Luca Rossi", "Mario Rossi"]
    assert [u.name for u in svc.consultant.accessible_patients(svc.mario)] == ["Mario Rossi"]  # il contrario no
    assert [u.name for u in svc.consultant.accessible_patients(svc.anna)] == ["Anna Bianchi"]


# --- Contesto ------------------------------------------------------------------


async def test_context_lists_values_flags_reference_visits_and_other_documents(make):
    svc = make()
    await upload(svc, svc.mario, referto(date="2025-10-25", results=[lab("Colesterolo totale", "240", "mg/dL", "<200", "alto")]))
    await upload(svc, svc.mario, appuntamento(date="2026-11-03", time="09:30", place="Ospedale Nord"))
    await upload(svc, svc.mario, altro("ricetta"))
    context = svc.consultant.build_context([svc.mario])
    assert "## Mario Rossi (papà)" in context
    assert "25/10/2025: Colesterolo totale 240 mg/dL (rif. <200) [ALTO]" in context
    assert "03/11/2026 09:30: Visita cardiologica – Ospedale Nord" in context
    assert "(ricetta)" in context


async def test_context_says_when_there_is_nothing(make):
    svc = make()
    context = svc.consultant.build_context([svc.mario])
    assert "Nessun referto salvato." in context and "Nessuna visita salvata." in context


async def test_latest_report_comes_first_and_old_ones_are_capped(make):
    svc = make()
    for day in range(1, MAX_REPORTS + 4):
        await upload(svc, svc.mario, referto(date=f"2025-01-{day:02d}", results=[lab("Glicemia", str(day))]))
    lines = [l for l in svc.consultant.build_context([svc.mario]).splitlines() if l.startswith("- ") and "Glicemia" in l]
    assert len(lines) == MAX_REPORTS
    assert lines[0].startswith(f"- {MAX_REPORTS + 3:02d}/01/2025") and "Glicemia 18" in lines[0]


# --- ask -----------------------------------------------------------------------


async def test_ask_passes_context_and_question_and_returns_the_answer(make):
    svc = make(FakeAnswerer("Il colesterolo è alto."))
    await upload(svc, svc.mario, referto(results=[lab("Colesterolo totale", "240", "mg/dL", "<200", "alto")]))
    answer = await svc.consultant.ask(svc.mario, question="Cosa comporta il colesterolo a 240?")
    assert answer == "Il colesterolo è alto."
    call = svc.answerer.calls[0]
    assert call["sender"] == "Mario Rossi" and call["question"] == "Cosa comporta il colesterolo a 240?"
    assert "Colesterolo totale 240" in call["context"]
    assert svc.recent_activity(1)[0]["kind"] == "domanda"


async def test_ask_with_audio(make):
    svc = make()
    await svc.consultant.ask(svc.mario, audio=b"OggS...", audio_mime="audio/ogg")
    call = svc.answerer.calls[0]
    assert call["audio"] == b"OggS..." and call["mime"] == "audio/ogg" and call["question"] is None


# --- Gemini vero (SDK) contro server finto -------------------------------------


@pytest.fixture
def gemini(tmp_path, root):
    def _make(text="Risposta."):
        seen = []

        def transport(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})

        svc = Service(tmp_path / "data", "chiave-di-test", root, gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        svc.settings.update({"gemini_api_key": "AIza-test"})
        return svc.gemini, seen

    return _make


async def test_gemini_answer_uses_web_search_and_the_family_data(gemini):
    client, seen = gemini("  Ecco la risposta.  ")
    answer = await client.answer("## Mario\n- 25/10/2025: Glicemia 95", "Mario Rossi", question="Come va la glicemia?")
    assert answer == "Ecco la risposta."
    body = seen[0]
    assert any("googleSearch" in tool for tool in body["tools"])
    assert "responseMimeType" not in body.get("generationConfig", {}) and "responseSchema" not in body.get("generationConfig", {})
    text = body["contents"][0]["parts"][0]["text"]
    assert "Scrive Mario Rossi" in text and "Glicemia 95" in text and "Come va la glicemia?" in text
    system = json.dumps(body["systemInstruction"])
    assert "Non fare diagnosi" in system and "non istruzioni" in system and "112" in system


async def test_gemini_answer_sends_voice_as_audio(gemini):
    client, seen = gemini()
    await client.answer("## Mario", "Mario Rossi", audio=b"OggS-voce", audio_mime="audio/ogg")
    parts = seen[0]["contents"][0]["parts"]
    assert "messaggio vocale" in parts[0]["text"]
    assert parts[1]["inlineData"]["mimeType"] == "audio/ogg"
    assert base64.urlsafe_b64decode(parts[1]["inlineData"]["data"]) == b"OggS-voce"


# --- Dettagli dei documenti ----------------------------------------------------


async def test_prescription_details_reach_the_context_so_answers_can_use_them(make):
    svc = make()
    await upload(svc, svc.mario, altro("ricetta", details="Occhio destro: sfera -2.50, cilindro -0.75\nOcchio sinistro: sfera -3.00", date="2026-03-10"))
    context = svc.consultant.build_context([svc.mario])
    assert "10/03/2026 (ricetta)" in context and "Dettagli:" in context
    assert "    Occhio destro: sfera -2.50, cilindro -0.75" in context and "    Occhio sinistro: sfera -3.00" in context


async def test_report_notes_are_in_the_context_too(make):
    svc = make()
    await upload(svc, svc.mario, referto(details="Conclusioni: nella norma, controllo tra 12 mesi."))
    assert "Note del referto:" in svc.consultant.build_context([svc.mario])
    assert "controllo tra 12 mesi" in svc.consultant.build_context([svc.mario])


async def test_documents_without_details_add_no_empty_section(make):
    svc = make()
    await upload(svc, svc.mario, altro("ricetta"))
    assert "Dettagli:" not in svc.consultant.build_context([svc.mario])


async def test_details_are_saved_in_the_database(make):
    svc = make()
    outcome = await upload(svc, svc.mario, altro("ricetta", details="  Metformina 500 mg, due volte al giorno  "))
    assert svc.records.get_document(outcome.document_id)["details"] == "Metformina 500 mg, due volte al giorno"
