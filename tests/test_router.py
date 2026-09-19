import json

import httpx
import pytest

from famiglia.ai import AiError
from famiglia.service import Service

from helpers import appuntamento, referto

JPEG = b"\xff\xd8\xff fake jpeg"
GROQ_MODELS = [{"id": i} for i in ("llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct", "whisper-large-v3", "whisper-large-v3-turbo", "openai/gpt-oss-120b")]


def completion(text):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest.fixture
def world(tmp_path, root):
    """Servizio con Gemini e un provider compatibile OpenAI, entrambi finti; registra chi viene chiamato."""

    def _make(ai=None, gemini=None):
        calls: list[tuple[str, str, str]] = []

        def ai_transport(request):
            calls.append(("ai", request.method, request.url.path))
            return ai(request) if ai else httpx.Response(500)

        def gemini_transport(request):
            calls.append(("gemini", request.method, request.url.path))
            if gemini:
                return gemini(request)
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "Risposta di Gemini."}]}, "finishReason": "STOP"}]})

        svc = Service(
            tmp_path / "data", "chiave-di-test", root,
            gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(gemini_transport)),
            ai_http=httpx.AsyncClient(transport=httpx.MockTransport(ai_transport)),
        )
        svc.settings.update({"gemini_api_key": "AIza-test", "groq_api_key": "gsk_test", "documents_dir": "Documenti"})
        svc.mario = svc.users.add(111, "Mario Rossi")
        svc.calls = calls
        return svc

    return _make


def use_groq(svc, docs="meta-llama/llama-4-scout-17b-16e-instruct", chat="llama-3.3-70b-versatile"):
    svc.settings.update({"ai_docs_provider": "groq", "ai_docs_model": docs, "ai_chat_provider": "groq", "ai_chat_model": chat})


# --- Instradamento -------------------------------------------------------------


async def test_default_is_gemini_and_never_touches_the_other_provider(world):
    svc = world()
    assert await svc.ai.answer("## Mario", "Mario", question="Ciao?") == "Risposta di Gemini."
    assert [c[0] for c in svc.calls] == ["gemini"]


async def test_chat_role_can_go_to_groq_while_documents_stay_on_gemini(world):
    svc = world(ai=lambda r: completion("Risposta di Groq."))
    svc.settings.update({"ai_chat_provider": "groq", "ai_chat_model": "llama-3.3-70b-versatile"})
    assert await svc.ai.answer("## Mario", "Mario", question="Ciao?") == "Risposta di Groq."
    assert svc.calls == [("ai", "POST", "/openai/v1/chat/completions")]
    assert svc.ai.provider("docs") == "gemini"


async def test_documents_role_goes_to_groq_with_the_chosen_model(world):
    seen = []

    def ai(request):
        seen.append(json.loads(request.content))
        return completion(referto(patient="Mario Rossi").model_dump_json())

    svc = world(ai=ai)
    use_groq(svc)
    result = await svc.ai.analyze_document(JPEG, "image/jpeg")
    assert result.kind == "referto" and seen[0]["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert all(c[0] == "ai" for c in svc.calls)


async def test_gemini_model_chosen_for_a_role_is_used(world):
    seen = []
    svc = world(gemini=lambda r: (seen.append(r.url.path), httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}))[1])
    svc.settings.update({"ai_chat_model": "gemini-scelto-per-le-domande"})
    await svc.ai.answer("## Mario", "Mario", question="Ciao?")
    assert "gemini-scelto-per-le-domande" in seen[0]


async def test_unknown_provider_value_falls_back_to_gemini(world):
    svc = world()
    svc.settings.update({"ai_chat_provider": "sconosciuto"})
    assert svc.ai.provider("chat") == "gemini"


# --- Configurazione mancante ---------------------------------------------------


async def test_missing_model_key_or_address_give_clear_errors(world):
    svc = world()
    svc.settings.update({"ai_chat_provider": "groq"})
    with pytest.raises(AiError, match="Nessun modello scelto"):
        await svc.ai.answer("", "Mario", question="?")
    svc.settings.update({"ai_chat_model": "x", "groq_api_key": ""})
    with pytest.raises(AiError) as no_key:
        await svc.ai.answer("", "Mario", question="?")
    assert "Groq non è configurato: manca la chiave" in no_key.value.user_message
    svc.settings.update({"ai_chat_provider": "custom", "custom_api_key": "k"})
    with pytest.raises(AiError) as no_url:
        await svc.ai.answer("", "Mario", question="?")
    assert "Manca l'indirizzo" in no_url.value.user_message


async def test_custom_provider_uses_the_address_from_the_panel(world):
    svc = world(ai=lambda r: completion("Da Ollama."))
    svc.settings.update({"ai_chat_provider": "custom", "custom_base_url": "http://192.168.0.50:11434/v1/", "custom_api_key": "ollama", "ai_chat_model": "llama3"})
    assert await svc.ai.answer("", "Mario", question="?") == "Da Ollama."
    assert svc.calls == [("ai", "POST", "/v1/chat/completions")]


# --- Vocali --------------------------------------------------------------------


async def test_voice_is_transcribed_with_the_providers_whisper_then_answered(world):
    requests = []

    def ai(request):
        requests.append((request.url.path, request.content))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": GROQ_MODELS})
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "Come sta la glicemia?"})
        return completion("La glicemia è a posto.")

    svc = world(ai=ai)
    use_groq(svc)
    assert await svc.ai.answer("## Mario", "Mario", audio=b"OggS-voce", audio_mime="audio/ogg") == "La glicemia è a posto."
    paths = [p for p, _ in requests]
    assert paths == ["/openai/v1/models", "/openai/v1/audio/transcriptions", "/openai/v1/chat/completions"]  # elenco preso al volo
    assert b"whisper-large-v3-turbo" in requests[1][1]
    assert b"Come sta la glicemia?" in requests[2][1]  # la domanda trascritta arriva al modello


async def test_voice_with_a_cached_model_list_does_not_ask_again(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"text": "Domanda"}) if r.url.path.endswith("transcriptions") else completion("Ok."))
    use_groq(svc)
    svc.settings.update({"ai_models_cache": json.dumps({"groq": ["whisper-large-v3", "llama-3.3-70b-versatile"]})})
    await svc.ai.answer("", "Mario", audio=b"x", audio_mime="audio/ogg")
    assert not any(c[2].endswith("/models") for c in svc.calls)


async def test_provider_without_whisper_says_it_cannot_listen(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]}))
    svc.settings.update({"deepseek_api_key": "sk", "ai_chat_provider": "deepseek", "ai_chat_model": "deepseek-chat"})
    with pytest.raises(AiError) as exc:
        await svc.ai.answer("", "Mario", audio=b"x", audio_mime="audio/ogg")
    assert "Con DeepSeek non posso ascoltare i vocali: scrivimi la domanda." == exc.value.user_message


# --- Elenco modelli e scelta automatica ----------------------------------------


async def test_refresh_stores_the_list_and_reads_it_back(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": GROQ_MODELS}))
    assert svc.ai.cached_models("groq") == []
    ids = await svc.ai.refresh_models("groq")
    assert svc.ai.cached_models("groq") == ids and "llama-3.3-70b-versatile" in ids
    svc.settings.update({"ai_models_cache": "non è json"})
    assert svc.ai.cached_models("groq") == []  # una cache rovinata non rompe nulla


def models_or_ok(models):
    """Elenco dei modelli per GET /models, risposta valida per le richieste di chat e immagine."""
    return lambda r: httpx.Response(200, json={"data": models}) if r.url.path.endswith("/models") else completion("ok")


async def test_refresh_and_pick_chooses_a_model_for_each_role_by_itself(world):
    svc = world(ai=models_or_ok(GROQ_MODELS))
    svc.settings.update({"ai_docs_provider": "groq", "ai_chat_provider": "groq"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("docs") == "meta-llama/llama-4-scout-17b-16e-instruct"  # il modello con visione
    assert svc.ai.model("chat") == "llama-3.3-70b-versatile"
    assert all(ok for _, ok in messages) and any("scelto meta-llama/llama-4-scout" in m for m, _ in messages)
    assert sum(1 for c in svc.calls if c[2].endswith("/models")) == 1  # un solo elenco per provider


async def test_a_chosen_model_is_never_replaced_but_an_unlisted_one_is_flagged(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": GROQ_MODELS}))
    svc.settings.update({"ai_docs_provider": "groq", "ai_docs_model": "openai/gpt-oss-120b", "ai_chat_provider": "groq", "ai_chat_model": "nome-non-in-elenco"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("docs") == "openai/gpt-oss-120b" and svc.ai.model("chat") == "nome-non-in-elenco"
    assert (any(ok and "trovato tra i" in m for m, ok in messages)
            and any(not ok and "«nome-non-in-elenco» non compare" in m and "Prova" in m for m, ok in messages))


async def test_a_provider_that_fails_is_reported_and_leaves_the_model_alone(world):
    svc = world(ai=lambda r: httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    svc.settings.update({"ai_chat_provider": "groq", "ai_chat_model": "quello-che-avevo"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("chat") == "quello-che-avevo"
    assert any(not ok and "Groq: La chiave di Groq non è valida" in m for m, ok in messages)


async def test_gemini_models_are_listed_but_the_model_stays_the_one_from_the_api_keys_page(world):
    svc = world(gemini=lambda r: httpx.Response(200, json={"models": [
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]}))
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.cached_models("gemini") == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    assert svc.ai.model("docs") == "" and svc.ai.model("chat") == ""  # nessuna sostituzione silenziosa
    assert any("Gemini, modello gemini-3.8-flash (2 disponibili)" in m for m, _ in messages)


async def test_a_provider_with_no_suitable_model_asks_to_type_one(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}, {"id": "playai-tts"}]}))
    svc.settings.update({"ai_chat_provider": "groq"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("chat") == "" and any(not ok and "scrivi il nome a mano" in m for m, ok in messages)


# --- Prova ---------------------------------------------------------------------


async def test_probe_reports_role_provider_and_model(world):
    svc = world(ai=lambda r: completion("ok"))
    use_groq(svc)
    label, ok, detail = await svc.ai.probe("docs")
    assert label == "Lettura dei documenti · Groq · meta-llama/llama-4-scout-17b-16e-instruct" and ok and detail == "funziona"


async def test_probe_without_a_model_says_what_is_missing(world):
    svc = world()
    svc.settings.update({"ai_chat_provider": "groq"})
    label, ok, detail = await svc.ai.probe("chat")
    assert not ok and "Nessun modello scelto" in detail and "nessun modello" in label


async def test_probe_of_a_gemini_role_sends_an_image_for_documents(world):
    bodies = []
    svc = world(gemini=lambda r: (bodies.append(json.loads(r.content)), httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}))[1])
    label, ok, _ = await svc.ai.probe("docs")
    assert ok and any("inlineData" in p for p in bodies[0]["contents"][0]["parts"])


# --- Dal documento al database con Groq ----------------------------------------


async def test_a_photo_read_by_groq_ends_up_saved_like_any_other(world, root):
    svc = world(ai=lambda r: completion(appuntamento(date="2027-01-15", time="10:00").model_dump_json()))
    use_groq(svc)
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "«Visita cardiologica»" in outcome.text and "15/01/2027 alle 10:00" in outcome.text
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2027-01-15T10:00"
    assert all(c[0] == "ai" for c in svc.calls)  # Gemini non è stato disturbato


async def test_a_question_answered_by_groq_through_the_whole_consultant(world):
    svc = world(ai=lambda r: completion("Tutto nella norma."))
    use_groq(svc)
    assert await svc.consultant.ask(svc.mario, question="Come sto?") == "Tutto nella norma."


# --- Scelta del modello con visione: si prova davvero ---------------------------

# Elenco realistico di Groq oggi: i modelli con visione sono i Qwen, non i Llama 4 di prima.
GROQ_TODAY = [{"id": i} for i in (
    "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound",
    "groq/compound-mini", "qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "whisper-large-v3", "whisper-large-v3-turbo",
)]


def vision_only(*vision_ids):
    """Server finto: elenco dei modelli; le richieste con immagine riescono solo per i modelli indicati."""
    tried = []

    def respond(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": GROQ_TODAY})
        body = json.loads(request.content)
        has_image = "image_url" in json.dumps(body["messages"])
        if has_image:
            tried.append(body["model"])
        if has_image and body["model"] not in vision_ids:
            return httpx.Response(400, json={"error": {"message": "This model does not support image input"}})
        return completion("ok")

    respond.tried = tried
    return respond


async def test_documents_model_is_the_first_one_that_really_reads_images(world):
    respond = vision_only("qwen/qwen3.6-27b", "qwen/qwen3.8-27b")
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_provider": "groq"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("docs") == "qwen/qwen3.8-27b"  # la versione più alta, provata per prima
    assert respond.tried == ["qwen/qwen3.8-27b"]
    assert any(ok and "scelto qwen/qwen3.8-27b, che legge le immagini" in m for m, ok in messages)


async def test_a_candidate_that_fails_the_image_test_is_skipped(world):
    respond = vision_only("qwen/qwen3.6-27b")  # la 3.8 non accetta le immagini
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_provider": "groq"})
    await svc.ai.refresh_and_pick()
    assert svc.ai.model("docs") == "qwen/qwen3.6-27b" and respond.tried == ["qwen/qwen3.8-27b", "qwen/qwen3.6-27b"]


async def test_no_model_that_reads_images_says_so_and_never_picks_a_text_model(world):
    respond = vision_only()  # nessuno legge le immagini
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_provider": "groq"})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.model("docs") == ""
    text = next(m for m, ok in messages if not ok)
    assert "nessun modello di Groq che legga le immagini" in text and "usa Gemini per i documenti" in text
    assert "This model does not support image input" in text and len(respond.tried) == 6  # non si prova all'infinito
    assert "groq/compound" not in respond.tried  # i sistemi «agentici» e i modelli audio si escludono


async def test_repick_forgets_the_old_wrong_model_and_finds_a_working_one(world):
    respond = vision_only("qwen/qwen3.8-27b")
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_provider": "groq", "ai_docs_model": "llama-3.3-70b-versatile",  # scelto in passato, senza visione
                         "ai_chat_provider": "groq", "ai_chat_model": "llama-3.3-70b-versatile"})
    assert not (await svc.ai.probe("docs"))[1]  # la prova lo conferma: non legge immagini
    await svc.ai.repick()
    assert svc.ai.model("docs") == "qwen/qwen3.8-27b" and svc.ai.model("chat") == "llama-3.3-70b-versatile"
    assert (await svc.ai.probe("docs"))[1]


async def test_repick_leaves_gemini_roles_alone(world):
    svc = world(gemini=lambda r: httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    svc.settings.update({"ai_chat_model": "gemini-scelto-a-mano"})
    await svc.ai.repick()
    assert svc.ai.model("chat") == "gemini-scelto-a-mano"


async def test_probe_of_a_model_without_vision_tells_what_to_press(world):
    svc = world(ai=vision_only())
    svc.settings.update({"ai_docs_provider": "groq", "ai_docs_model": "llama-3.3-70b-versatile"})
    _, ok, detail = await svc.ai.probe("docs")
    assert not ok and "non sa leggere le immagini" in detail and "Scegli di nuovo in automatico" in detail
