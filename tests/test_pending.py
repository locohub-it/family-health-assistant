"""Visite prescritte con un'impegnativa ma non ancora prenotate: si salvano «in sospeso» e la data si dà più avanti."""

import httpx
import pytest

from famiglia.ai import Endpoint
from famiglia.gemini import Extraction, Gemini
from famiglia.openai_compat import OpenAICompat, parse_extraction
from famiglia.service import Service
from famiglia.visits import Visits, wants_pending

from helpers import FakeAnswerer, FakeComposio, FakeReader, altro, appuntamento, referto
from test_visits import NOW, Message, codes, context, labels, tap_update, update

JPEG = b"\xff\xd8\xff fake jpeg"
GROQ = Endpoint("groq", "Groq", "https://api.groq.com/openai/v1", "gsk_test", "llama-test")


def impegnativa(*visits, kind="ricetta", details="Impegnativa: visita cardiologica"):
    return altro(kind, details=details).model_copy(update={"pending_visits": list(visits)})


@pytest.fixture
def make(tmp_path, root):
    def _make(extraction=None, users=1, composio=None):
        composio = composio or FakeComposio(connected={"famiglia-111", "famiglia-222"})
        reader = FakeReader(extraction or impegnativa("Visita cardiologica"))
        answerer = FakeAnswerer("Risposta alla domanda.")
        svc = Service(tmp_path / "data", "chiave-di-test", root, reader=reader, answerer=answerer, composio_factory=lambda k: composio)
        svc.settings.update({"documents_dir": "Documenti", "composio_api_key": "ak_test"})
        svc.mario = svc.users.add(111, "Mario Rossi", "papà")
        svc.anna = svc.users.add(222, "Anna Bianchi", "mamma") if users > 1 else None
        svc.composio, svc.answerer, svc.reader = composio, answerer, reader
        svc.visits = Visits(svc.users, svc.documents, now=lambda: NOW)
        return svc

    return _make


def pending_rows(svc):
    return svc.db.execute("SELECT * FROM appointments WHERE starts_at = '' ORDER BY id")


# --- Dal documento ------------------------------------------------------------------------------


async def test_a_prescription_for_a_visit_is_kept_as_a_pending_visit(make):
    svc = make()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "Ho salvato la ricetta di Mario Rossi" in outcome.text
    assert "«Visita cardiologica» è tra le visite in sospeso" in outcome.text and "/in_sospeso" in outcome.text
    (row,) = pending_rows(svc)
    assert (row["title"], row["user_id"], row["event_id"]) == ("Visita cardiologica", svc.mario.id, "")
    assert svc.records.get_document(outcome.document_id)["kind"] == "ricetta"  # l'impegnativa resta salvata come documento
    assert svc.composio.executed == []  # nessun evento sul calendario finché manca la data


async def test_several_visits_on_one_prescription_are_listed_together(make):
    svc = make(impegnativa("Visita cardiologica", "Elettrocardiogramma"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "Queste visite sono tra quelle in sospeso" in outcome.text
    assert "• Visita cardiologica" in outcome.text and "• Elettrocardiogramma" in outcome.text
    assert [r["title"] for r in pending_rows(svc)] == ["Visita cardiologica", "Elettrocardiogramma"]


async def test_a_prescription_for_medicines_only_creates_nothing_pending(make):
    svc = make(impegnativa(details="Metformina 500 mg"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "in sospeso" not in outcome.text and pending_rows(svc) == []


async def test_a_booking_with_a_date_is_not_pending(make):
    svc = make(appuntamento(date="2099-11-03"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "in sospeso" not in outcome.text and pending_rows(svc) == []
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2099-11-03T09:30"


async def test_a_booking_read_without_a_date_becomes_pending_with_its_title(make):
    svc = make(appuntamento(date="", time="", title="Visita oculistica"))
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "non c'è ancora una data" in outcome.text and "«Visita oculistica» è tra le visite in sospeso" in outcome.text
    assert [r["title"] for r in pending_rows(svc)] == ["Visita oculistica"]


async def test_a_booking_without_date_and_title_is_called_generic_visit(make):
    svc = make(appuntamento(date="", time="", title="  "))
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert [r["title"] for r in pending_rows(svc)] == ["Visita medica"]


async def test_the_same_visit_named_twice_is_saved_once(make):
    extraction = appuntamento(date="", time="", title="Visita cardiologica").model_copy(update={"pending_visits": ["visita cardiologica ", "Ecografia"]})
    svc = make(extraction)
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert [r["title"] for r in pending_rows(svc)] == ["Visita cardiologica", "Ecografia"]


async def test_a_runaway_list_is_capped(make):
    svc = make(impegnativa(*[f"Esame {i}" for i in range(20)]))
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert len(pending_rows(svc)) == 6


async def test_undo_removes_the_prescription_and_its_pending_visit(make):
    svc = make()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    await svc.documents.undo(svc.mario, outcome.document_id)
    assert pending_rows(svc) == [] and svc.db.execute("SELECT * FROM documents") == []


# --- La risposta del modello ------------------------------------------------------------------------


def test_an_answer_without_the_new_field_means_no_pending_visits():
    old = referto().model_dump()
    del old["pending_visits"]
    assert Extraction.model_validate(old).pending_visits == []


@pytest.mark.parametrize(
    "value,expected",
    [(["Visita cardiologica"], ["Visita cardiologica"]), ("Visita cardiologica", ["Visita cardiologica"]), (None, []), ([" ", ""], []), ([1, "Ecg"], ["1", "Ecg"])],
)
def test_openai_compatible_answers_are_normalised(value, expected):
    import json

    body = json.dumps({"kind": "ricetta", "summary": "", "pending_visits": value})
    assert parse_extraction(body, GROQ).pending_visits == expected


def test_the_instructions_tell_models_where_to_put_a_prescribed_visit():
    from famiglia.gemini import ANALYZE_INSTRUCTIONS
    from famiglia.openai_compat import JSON_RULES

    assert "pending_visits" in ANALYZE_INSTRUCTIONS and "NON è una prenotazione" in ANALYZE_INSTRUCTIONS and "Non inserire i farmaci" in ANALYZE_INSTRUCTIONS
    assert '"pending_visits": []' in JSON_RULES  # l'esempio è vuoto: nessun modello lo copia su un referto


async def test_gemini_is_asked_for_the_pending_visits_in_its_schema():
    seen = []

    def transport(request):
        import json

        seen.append(json.loads(request.content))
        body = impegnativa("Visita cardiologica").model_dump_json()
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": body}]}, "finishReason": "STOP"}]})

    gemini = Gemini(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    result = await gemini.analyze_document(Endpoint("gemini", "Gemini", "", "AIza", "gemini-test"), JPEG, "image/jpeg")
    assert result.pending_visits == ["Visita cardiologica"]
    schema = str(seen[0]["generationConfig"].get("responseJsonSchema") or seen[0]["generationConfig"].get("responseSchema"))
    assert "pending_visits" in schema


async def test_pending_visits_are_in_the_context_for_questions_but_not_counted_as_booked(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    context_text = svc.consultant.build_context([svc.mario])
    assert "Visite prescritte ma ancora da prenotare (manca la data):\n- Visita cardiologica" in context_text
    assert "Nessuna visita salvata." in context_text  # non c'è nessuna visita con una data
    assert "Visite ed esami prenotati" not in context_text


# --- /in_sospeso ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["appuntamenti in sospeso", "Appuntamenti in sospeso.", "  gli appuntamenti in sospeso "])
def test_the_typed_phrase_opens_the_pending_list(text):
    assert wants_pending(text)


@pytest.mark.parametrize("text", ["ho appuntamenti in sospeso?", "in sospeso", "quali appuntamenti in sospeso ho", "appuntamenti", "elimina appuntamenti in sospeso"])
def test_no_other_sentence_opens_the_pending_list(text):
    assert not wants_pending(text)


async def test_the_list_shows_the_name_from_the_prescription_with_a_button_for_the_date(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    header, card = svc.visits.list_pending(svc.mario)
    assert "in sospeso" in header.text
    assert "Data da fissare" in card.text and "«Visita cardiologica» – Mario Rossi" in card.text
    assert [label for row in card.buttons for label, _ in row] == ["📅 Imposta la data", "🗑️ Elimina"]


async def test_an_empty_list_says_so(make):
    svc = make()
    (reply,) = svc.visits.list_pending(svc.mario)
    assert "Non hai visite in sospeso" in reply.text and reply.buttons == []


async def test_pending_visits_are_not_shown_among_the_dated_ones(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "Non ho visite in programma" in svc.visits.list_cards(svc.mario)[0].text


async def test_giving_the_date_saves_it_and_puts_the_visit_on_the_calendar(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    state = {}
    ask = await svc.visits.on_button(svc.mario, state, f"a:ew:{pid}")
    assert "Quando hai prenotato?" in ask.text

    reply = await svc.visits.on_text(svc.mario, state, "26 ottobre 2099 alle 15")
    assert "Fatto, ho fissato la visita." in reply.text and "26/10/2099 alle 15:00" in reply.text
    assert "L'ho aggiunta al calendario di Mario Rossi" in reply.text  # è il primo inserimento, non un aggiornamento
    row = svc.db.execute("SELECT * FROM appointments")[0]
    assert (row["starts_at"], row["title"], row["event_id"]) == ("2099-10-26T15:00", "Visita cardiologica", "evt123")
    create = [(a["summary"], user) for slug, a, user in svc.composio.executed if slug == "GOOGLECALENDAR_CREATE_EVENT"]
    assert create == [("Visita cardiologica", "famiglia-111")]
    assert [label for r in reply.buttons for label, _ in r] == ["✏️ Modifica", "🗑️ Elimina"]  # ora è una visita come le altre

    assert pending_rows(svc) == [] and "Non hai visite in sospeso" in svc.visits.list_pending(svc.mario)[0].text
    assert "26/10/2099" in svc.visits.list_cards(svc.mario)[1].text  # e compare tra le visite in programma


async def test_the_coordinator_gets_the_visit_when_the_date_is_given(make):
    svc = make(users=2)
    svc.settings.update({"coordinator_user_id": str(svc.anna.id)})
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{pid}")
    reply = await svc.visits.on_text(svc.mario, state, "26/10/2099 alle 15")
    assert "coordinatore" in reply.text
    users = [user for slug, _, user in svc.composio.executed if slug == "GOOGLECALENDAR_CREATE_EVENT"]
    assert users == ["famiglia-111", "famiglia-222"]


async def test_a_date_that_is_not_understood_leaves_the_visit_pending(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{pid}")
    assert "Non ho capito la data" in (await svc.visits.on_text(svc.mario, state, "boh")).text
    assert "già passata" in (await svc.visits.on_text(svc.mario, state, "1/1/2020")).text
    assert len(pending_rows(svc)) == 1 and svc.composio.executed == [] and state["kind"] == "edit"


async def test_the_visit_is_dated_even_if_the_calendar_is_not_configured(make):
    svc = make()
    svc.settings.update({"composio_api_key": ""})
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{pid}")
    reply = await svc.visits.on_text(svc.mario, state, "26/10/2099 alle 15")
    assert "Fatto, ho fissato la visita." in reply.text and pending_rows(svc) == []


async def test_deleting_a_pending_visit_keeps_the_prescription(make, root):
    svc = make()
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    ask = await svc.visits.on_button(svc.mario, {}, f"a:d:{pid}")
    assert "in sospeso" in ask.text and "La ricetta resta salvata" in ask.text and "calendario" not in ask.text
    done = await svc.visits.on_button(svc.mario, {}, f"a:dy:{pid}")
    assert "Ho eliminato la visita «Visita cardiologica»" in done.text
    assert pending_rows(svc) == [] and svc.composio.executed == []
    document = svc.records.get_document(outcome.document_id)
    assert document is not None and (root / document["file_path"]).exists()


async def test_cancelling_the_date_request_goes_back_to_the_pending_card(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    state = {}
    await svc.visits.on_button(svc.mario, state, f"a:ew:{pid}")
    back = await svc.visits.on_button(svc.mario, state, f"a:b:{pid}")
    assert "Data da fissare" in back.text and state == {}


async def test_someone_else_cannot_see_or_date_my_pending_visits(make):
    svc = make(users=2)
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    assert "Non hai visite in sospeso" in svc.visits.list_pending(svc.anna)[0].text
    state = {}
    assert "non puoi cambiarla" in (await svc.visits.on_button(svc.anna, state, f"a:ew:{pid}")).text
    assert len(pending_rows(svc)) == 1


async def test_who_sent_a_relatives_prescription_can_date_it(make):
    svc = make(impegnativa("Visita cardiologica").model_copy(update={"patient_name": "Anna Bianchi"}), users=2)
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")  # la manda Mario, è di Anna
    assert svc.db.execute("SELECT user_id FROM appointments")[0]["user_id"] == svc.anna.id
    (_, card) = svc.visits.list_pending(svc.mario)
    assert "Anna Bianchi" in card.text
    state = {}
    await svc.visits.on_button(svc.mario, state, card.buttons[0][0][1])  # «Imposta la data»
    await svc.visits.on_text(svc.mario, state, "26/10/2099 alle 15")
    assert svc.composio.executed[-1][2] == "famiglia-222"  # sul calendario di Anna


# --- Dal bot ----------------------------------------------------------------------------------------------------


async def test_the_command_lists_pending_visits_with_buttons(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    message = Message()
    await svc.bot._list_pending(update(111, message), context())
    header, card = message.replies
    assert "in sospeso" in header.text and codes(card.markup) == [f"a:ew:{pid}", f"a:d:{pid}"]


async def test_the_typed_phrase_lists_them_too_and_interrupts_a_walk(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    ctx = context()
    await svc.bot._new_visit(update(111, Message()), ctx)
    message = Message("appuntamenti in sospeso")
    await svc.bot._text(update(111, message), ctx)
    assert ctx.user_data == {} and "Data da fissare" in message.replies[1].text and svc.answerer.calls == []


async def test_full_walk_through_the_bot_buttons(make):
    svc = make()
    await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    pid = pending_rows(svc)[0]["id"]
    ctx = context()
    upd, tap = tap_update(111, f"a:ew:{pid}")
    await svc.bot._visit_button(upd, ctx)
    assert "Quando hai prenotato?" in tap.edited[0][0]
    message = Message("26/10/2099 alle 15")
    await svc.bot._text(update(111, message), ctx)
    assert "Fatto, ho fissato la visita." in message.replies[0].text and labels(message.replies[0].markup) == ["✏️ Modifica", "🗑️ Elimina"]


def test_both_command_names_and_the_menu_entry_exist(make):
    from telegram.ext import Application

    from famiglia.bot import MENU

    svc = make()
    app = Application.builder().token("1:abc").build()
    svc.bot._register(app)
    commands = [set(h.commands) for group in app.handlers.values() for h in group if hasattr(h, "commands")]
    assert {"in_sospeso", "appuntamenti_in_sospeso"} in commands
    assert ("in_sospeso", "Visite ancora da prenotare: dai la data") in MENU
    assert all(name == name.lower() and " " not in name and len(name) <= 32 for name, _ in MENU)  # regole di Telegram sui comandi


async def test_start_and_unknown_command_mention_the_new_command(make):
    svc = make()
    message = Message()
    await svc.bot._start(update(111, message), context())
    assert "/in_sospeso" in message.replies[0].text
    other = Message()
    await svc.bot._unknown_command(update(111, other), context())
    assert "/in_sospeso" in other.replies[0].text
