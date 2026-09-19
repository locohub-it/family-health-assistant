import json

import httpx
import pytest

from famiglia.ai import AiError, NotConfigured
from famiglia.service import Service

from helpers import altro, appuntamento, referto

JPEG = b"\xff\xd8\xff fake jpeg"
GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_MODELS = [{"id": i} for i in ("llama-3.3-70b-versatile", "meta-llama/llama-4-scout-17b-16e-instruct", "whisper-large-v3", "whisper-large-v3-turbo", "openai/gpt-oss-120b")]
# Elenco realistico di Groq oggi: i modelli con visione sono i Qwen, non i Llama 4 di prima.
GROQ_TODAY = [{"id": i} for i in (
    "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound",
    "groq/compound-mini", "qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "whisper-large-v3", "whisper-large-v3-turbo",
)]
QUOTA = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota, please check your plan and billing details."}}


def completion(text):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def gemini_reply(text="Risposta di Gemini."):
    return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]})


def groq_error(status, message="errore"):
    return httpx.Response(status, json={"error": {"message": message}})


@pytest.fixture
def world(tmp_path, root):
    """Servizio con Gemini e un servizio compatibile OpenAI, entrambi finti; registra chi viene chiamato."""

    def _make(ai=None, gemini=None):
        calls: list[tuple[str, str, str]] = []
        headers: list[dict] = []

        def ai_transport(request):
            calls.append(("ai", request.method, request.url.path))
            headers.append(dict(request.headers))
            return ai(request) if ai else httpx.Response(500)

        def gemini_transport(request):
            calls.append(("gemini", request.method, request.url.path))
            return gemini(request) if gemini else gemini_reply()

        svc = Service(
            tmp_path / "data", "chiave-di-test", root,
            gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(gemini_transport)),
            ai_http=httpx.AsyncClient(transport=httpx.MockTransport(ai_transport)),
        )
        svc.settings.update({"documents_dir": "Documenti"})
        svc.mario = svc.users.add(111, "Mario Rossi")
        svc.calls, svc.headers = calls, headers
        return svc

    return _make


def add_groq(svc, key="gsk_test", name="Groq"):
    return svc.ai_services.add(name, "openai", GROQ_URL, key)


def add_gemini(svc, key="AIza-test", name="Gemini"):
    return svc.ai_services.add(name, "gemini", "", key)


def assign(svc, role, service, model, backup=False):
    prefix = svc.ai.prefix(role, backup)
    svc.settings.update({f"{prefix}_service": str(service.id), f"{prefix}_model": model})


def errors_logged(svc):
    return [r["detail"] for r in svc.recent_activity(20) if r["kind"] == "errore"]


# --- Instradamento -------------------------------------------------------------


async def test_a_function_uses_the_service_and_model_chosen_for_it(world):
    svc = world(ai=lambda r: completion("Risposta di Groq."))
    assign(svc, "chat", add_groq(svc), "llama-3.3-70b-versatile")
    assert await svc.ai.answer("## Mario", "Mario", question="Ciao?") == "Risposta di Groq."
    assert svc.calls == [("ai", "POST", "/openai/v1/chat/completions")]


async def test_each_function_can_use_a_different_service(world):
    seen = []

    def ai(request):
        seen.append(json.loads(request.content)["model"])
        return completion(referto(patient="Mario Rossi").model_dump_json())

    svc = world(ai=ai)
    assign(svc, "docs", add_groq(svc), "qwen/qwen3.8-27b")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash")
    assert (await svc.ai.analyze_document(JPEG, "image/jpeg")).kind == "referto" and seen == ["qwen/qwen3.8-27b"]
    assert await svc.ai.answer("", "Mario", question="Ciao?") == "Risposta di Gemini."
    assert [c[0] for c in svc.calls] == ["ai", "gemini"]


async def test_two_gemini_services_with_different_keys_are_kept_apart(world):
    keys = []

    def gemini(request):
        keys.append(request.headers["x-goog-api-key"])
        return httpx.Response(429, json=QUOTA) if keys[-1] == "AIza-uno" else gemini_reply()

    svc = world(gemini=gemini)
    assign(svc, "chat", add_gemini(svc, "AIza-uno", "Gemini personale"), "gemini-a")  # ha finito la quota
    assign(svc, "chat", add_gemini(svc, "AIza-due", "Gemini famiglia"), "gemini-b", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Risposta di Gemini."
    assert keys == ["AIza-uno", "AIza-due"]  # ognuno con la propria chiave


async def test_a_service_without_a_key_sends_no_authorization_header(world):
    svc = world(ai=lambda r: completion("Da Ollama."))
    ollama = svc.ai_services.add("Ollama", "openai", "http://192.168.0.50:11434/v1", "")
    assign(svc, "chat", ollama, "llama3")
    assert await svc.ai.answer("", "Mario", question="?") == "Da Ollama."
    assert svc.calls == [("ai", "POST", "/v1/chat/completions")] and "authorization" not in svc.headers[0]


# --- Riserva: anche di un altro servizio ---------------------------------------------


async def test_the_backup_is_never_touched_while_the_primary_answers(world):
    svc = world(ai=lambda r: completion("Dalla principale."))
    assign(svc, "chat", add_groq(svc), "llama-3.3-70b-versatile")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Dalla principale."
    assert [c[0] for c in svc.calls] == ["ai"] and errors_logged(svc) == []


async def test_when_the_primary_hits_its_limit_a_backup_on_another_service_answers(world):
    svc = world(ai=lambda r: groq_error(429, "Rate limit reached on requests per day: Limit 1000, Used 1000, Requested 1. Please try again in 14m8s."))
    assign(svc, "chat", add_groq(svc), "openai/gpt-oss-120b")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Risposta di Gemini."
    assert [c[0] for c in svc.calls] == ["ai", "gemini"]
    assert any("la principale non ha risposto" in e and "ha risposto la riserva (Gemini · gemini-3.8-flash)" in e for e in errors_logged(svc))


async def test_the_backup_can_be_another_model_of_the_same_service(world):
    svc = world(ai=lambda r: groq_error(429, "quota") if json.loads(r.content)["model"] == "openai/gpt-oss-120b" else completion("Dal modello leggero."))
    groq = add_groq(svc)
    assign(svc, "chat", groq, "openai/gpt-oss-120b")
    assign(svc, "chat", groq, "llama-3.1-8b-instant", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Dal modello leggero."


@pytest.mark.parametrize("failure", [groq_error(401, "Invalid API Key"), groq_error(404, "model not found"), groq_error(503, "overloaded")])
async def test_any_failure_of_the_primary_falls_back(world, failure):
    svc = world(ai=lambda r: failure)
    assign(svc, "chat", add_groq(svc), "x")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Risposta di Gemini."


async def test_a_primary_with_no_model_chosen_falls_back(world):
    svc = world()
    assign(svc, "chat", svc.ai_services.add("Vuoto", "openai", GROQ_URL, ""), "")  # servizio scelto ma nessun modello
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert await svc.ai.answer("", "Mario", question="?") == "Risposta di Gemini."
    assert [c[0] for c in svc.calls] == ["gemini"]  # la principale non è nemmeno stata chiamata


async def test_a_removed_primary_service_falls_back_to_the_backup(world):
    svc = world()
    doomed = add_groq(svc)
    assign(svc, "chat", doomed, "x")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    svc.ai_services.remove(doomed.id)  # svuota la principale; la riserva resta
    assert svc.ai.slot("chat")[0] is None and await svc.ai.answer("", "Mario", question="?") == "Risposta di Gemini."


async def test_when_both_fail_the_error_of_the_primary_is_shown_and_both_are_logged(world):
    svc = world(ai=lambda r: groq_error(429, "Rate limit reached: Limit 8000, Used 0, Requested 9500."), gemini=lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    assign(svc, "chat", add_groq(svc), "x")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    with pytest.raises(AiError) as exc:
        await svc.ai.answer("", "Mario", question="?")
    assert "troppo grande" in exc.value.user_message  # il messaggio della principale, non quello della riserva
    assert any("anche la riserva non ha risposto" in e and "403" in e for e in errors_logged(svc))


async def test_without_a_backup_the_primary_error_is_raised_as_it_is(world):
    svc = world(ai=lambda r: groq_error(401, "Invalid API Key"))
    assign(svc, "chat", add_groq(svc), "x")
    with pytest.raises(AiError, match="401"):
        await svc.ai.answer("", "Mario", question="?")
    assert len(svc.calls) == 1


async def test_a_backup_without_a_model_is_a_failure_not_a_crash(world):
    svc = world(ai=lambda r: groq_error(401, "Invalid API Key"))
    groq = add_groq(svc)
    assign(svc, "chat", groq, "x")
    svc.settings.update({"ai_chat_backup_service": str(groq.id)})  # riserva con servizio ma senza modello
    with pytest.raises(AiError, match="401"):
        await svc.ai.answer("", "Mario", question="?")


async def test_documents_fall_back_to_a_backup_that_reads_images(world):
    svc = world(ai=lambda r: groq_error(400, "This model does not support image input"), gemini=lambda r: gemini_reply(referto(patient="Mario Rossi").model_dump_json()))
    assign(svc, "docs", add_groq(svc), "llama-3.3-70b-versatile")  # senza visione
    assign(svc, "docs", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert (await svc.ai.analyze_document(JPEG, "image/jpeg")).kind == "referto"


# --- Configurazione mancante ----------------------------------------------------------


async def test_missing_service_model_or_key_give_clear_errors(world):
    svc = world()
    with pytest.raises(NotConfigured, match="Nessun servizio scelto per «Domande e risposte»"):
        await svc.ai.answer("", "Mario", question="?")
    groq = add_groq(svc)
    svc.settings.update({"ai_chat_service": str(groq.id)})
    with pytest.raises(NotConfigured, match="Nessun modello scelto per «Domande e risposte» \\(Groq\\)"):
        await svc.ai.answer("", "Mario", question="?")
    gemini = svc.ai_services.add("Vuoto", "openai", GROQ_URL, "")
    assign(svc, "docs", gemini, "m")
    svc.settings.update({"ai_chat_service": ""})  # nessun servizio scelto e nessuna riserva: errore della principale
    with pytest.raises(AiError, match="Nessun servizio"):
        await svc.ai.answer("", "Mario", question="?")


# --- Vocali --------------------------------------------------------------------


async def test_voice_is_transcribed_with_the_services_whisper_then_answered(world):
    requests = []

    def ai(request):
        requests.append((request.url.path, request.content))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": GROQ_MODELS})
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "Come sta la glicemia?"})
        return completion("La glicemia è a posto.")

    svc = world(ai=ai)
    assign(svc, "chat", add_groq(svc), "llama-3.3-70b-versatile")
    assert await svc.ai.answer("## Mario", "Mario", audio=b"OggS-voce", audio_mime="audio/ogg") == "La glicemia è a posto."
    assert [p for p, _ in requests] == ["/openai/v1/models", "/openai/v1/audio/transcriptions", "/openai/v1/chat/completions"]  # elenco preso al volo
    assert b"whisper-large-v3-turbo" in requests[1][1] and b"Come sta la glicemia?" in requests[2][1]


async def test_voice_with_a_known_model_list_does_not_ask_again(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"text": "Domanda"}) if r.url.path.endswith("transcriptions") else completion("Ok."))
    groq = add_groq(svc)
    svc.ai_services.set_models(groq.id, ["whisper-large-v3", "llama-3.3-70b-versatile"])
    assign(svc, "chat", groq, "llama-3.3-70b-versatile")
    await svc.ai.answer("", "Mario", audio=b"x", audio_mime="audio/ogg")
    assert not any(c[2].endswith("/models") for c in svc.calls)


async def test_a_service_without_whisper_cannot_listen_but_a_gemini_backup_can(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": [{"id": "deepseek-chat"}]}))
    deepseek = svc.ai_services.add("DeepSeek", "openai", "https://api.deepseek.com", "sk")
    assign(svc, "chat", deepseek, "deepseek-chat")
    with pytest.raises(AiError) as exc:
        await svc.ai.answer("", "Mario", audio=b"x", audio_mime="audio/ogg")
    assert exc.value.user_message == "Con DeepSeek non posso ascoltare i vocali: scrivimi la domanda."
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)  # con una riserva che ascolta, il vocale funziona
    assert await svc.ai.answer("", "Mario", audio=b"OggS", audio_mime="audio/ogg") == "Risposta di Gemini."


# --- Elenco dei modelli e scelta automatica -------------------------------------------


async def test_refresh_stores_the_list_on_the_service(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": GROQ_MODELS}))
    groq = add_groq(svc)
    assert groq.models == ()
    ids = await svc.ai.refresh_models(groq.id)
    assert svc.ai_services.get(groq.id).models == tuple(ids) and "llama-3.3-70b-versatile" in ids
    with pytest.raises(AiError, match="non trovato"):
        await svc.ai.refresh_models(999)


def models_or_ok(models):
    """Elenco dei modelli per GET /models, risposta valida per le richieste di chat e immagine."""
    return lambda r: httpx.Response(200, json={"data": models}) if r.url.path.endswith("/models") else completion("ok")


async def test_refresh_and_pick_chooses_a_model_for_every_slot_by_itself(world):
    svc = world(ai=models_or_ok(GROQ_MODELS), gemini=lambda r: httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    groq, gemini = add_groq(svc), add_gemini(svc)
    for role in ("docs", "chat"):
        svc.settings.update({f"ai_{role}_service": str(groq.id), f"ai_{role}_backup_service": str(gemini.id)})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == "meta-llama/llama-4-scout-17b-16e-instruct"  # il modello con visione
    assert svc.ai.slot("chat")[1] == "llama-3.3-70b-versatile"
    assert svc.ai.slot("docs", True)[1] == "gemini-3.8-flash" and svc.ai.slot("chat", True)[1] == "gemini-3.8-flash"
    assert all(ok for _, ok in messages) and any("Lettura dei documenti · riserva: scelto gemini-3.8-flash" in m for m, _ in messages)
    assert sum(1 for c in svc.calls if c[0] == "ai" and c[2].endswith("/models")) == 1  # un solo elenco per servizio
    assert sum(1 for c in svc.calls if c[0] == "gemini" and c[2].endswith("/models")) == 1


async def test_a_chosen_model_is_never_replaced_but_an_unlisted_one_is_flagged(world):
    svc = world(ai=models_or_ok(GROQ_MODELS))
    groq = add_groq(svc)
    assign(svc, "docs", groq, "openai/gpt-oss-120b")
    assign(svc, "chat", groq, "nome-non-in-elenco")
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == "openai/gpt-oss-120b" and svc.ai.slot("chat")[1] == "nome-non-in-elenco"
    assert any(ok and "trovato tra i" in m for m, ok in messages)
    assert any(ok is False and "«nome-non-in-elenco» non compare" in m and "Prova" in m for m, ok in messages)


async def test_a_service_that_fails_is_reported_and_leaves_the_model_alone(world):
    svc = world(ai=lambda r: groq_error(401, "Invalid API Key"))
    assign(svc, "chat", add_groq(svc), "quello-che-avevo")
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("chat")[1] == "quello-che-avevo"
    assert any(ok is False and "Groq: La chiave di Groq non è valida" in m for m, ok in messages)


async def test_a_key_not_yet_entered_is_a_neutral_note_shown_once_per_service(world):
    svc = world()
    gemini = svc.ai_services.add("Gemini", "gemini", "", "AIza")
    svc.ai_services.update(gemini.id, "Gemini", "gemini", "", "AIza")
    svc.db.execute("UPDATE ai_services SET api_key = '' WHERE id = ?", (gemini.id,))  # chiave non più leggibile
    for role in ("docs", "chat"):
        svc.settings.update({f"ai_{role}_service": str(gemini.id)})
    messages = await svc.ai.refresh_and_pick()
    assert messages == [("Gemini: chiave non impostata, elenco dei modelli non caricato.", None)]
    assert svc.calls == []  # senza chiave non si chiama nessuno


async def test_a_note_does_not_hide_a_real_failure_of_another_service(world):
    svc = world(ai=lambda r: groq_error(401, "Invalid API Key"))
    keyless = svc.ai_services.add("Gemini", "gemini", "", "AIza")
    svc.db.execute("UPDATE ai_services SET api_key = '' WHERE id = ?", (keyless.id,))
    assign(svc, "docs", keyless, "")
    assign(svc, "chat", add_groq(svc), "x")
    messages = await svc.ai.refresh_and_pick()
    assert any(ok is None and "chiave non impostata" in m for m, ok in messages)
    assert any(ok is False and "La chiave di Groq non è valida" in m for m, ok in messages)


async def test_no_suitable_model_asks_to_type_one(world):
    svc = world(ai=lambda r: httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}, {"id": "playai-tts"}]}))
    svc.settings.update({"ai_chat_service": str(add_groq(svc).id)})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("chat")[1] == "" and any(ok is False and "scrivi il nome a mano" in m for m, ok in messages)


# --- Scelta del modello con visione: si prova davvero ---------------------------

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
    svc.settings.update({"ai_docs_service": str(add_groq(svc).id)})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == "qwen/qwen3.8-27b" and respond.tried == ["qwen/qwen3.8-27b"]  # la più alta, per prima
    assert any(ok and "scelto qwen/qwen3.8-27b, che legge le immagini" in m for m, ok in messages)


async def test_a_candidate_that_fails_the_image_test_is_skipped(world):
    respond = vision_only("qwen/qwen3.6-27b")
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_service": str(add_groq(svc).id)})
    await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == "qwen/qwen3.6-27b" and respond.tried == ["qwen/qwen3.8-27b", "qwen/qwen3.6-27b"]


async def test_no_model_that_reads_images_says_so_and_never_picks_a_text_model(world):
    respond = vision_only()
    svc = world(ai=respond)
    svc.settings.update({"ai_docs_service": str(add_groq(svc).id)})
    messages = await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == ""
    text = next(m for m, ok in messages if ok is False)
    assert "nessun modello di Groq che legga le immagini" in text and "scegli un altro servizio" in text
    assert "This model does not support image input" in text and len(respond.tried) == 6  # non si prova all'infinito
    assert "groq/compound" not in respond.tried


async def test_gemini_documents_need_no_probing(world):
    svc = world(gemini=lambda r: httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    svc.settings.update({"ai_docs_service": str(add_gemini(svc).id)})
    await svc.ai.refresh_and_pick()
    assert svc.ai.slot("docs")[1] == "gemini-3.8-flash" and [c[2].endswith("/models") for c in svc.calls] == [True]


async def test_repick_forgets_every_model_and_finds_working_ones(world):
    respond = vision_only("qwen/qwen3.8-27b")
    svc = world(ai=respond)
    groq = add_groq(svc)
    for backup in (False, True):
        assign(svc, "docs", groq, "llama-3.3-70b-versatile", backup)  # scelto in passato, senza visione
        assign(svc, "chat", groq, "llama-3.3-70b-versatile", backup)
    assert not (await svc.ai.probe("docs"))[0][1]  # la prova lo conferma: non legge immagini
    await svc.ai.repick()
    assert svc.ai.slot("docs")[1] == "qwen/qwen3.8-27b" and svc.ai.slot("docs", True)[1] == "qwen/qwen3.8-27b"
    assert svc.ai.slot("chat")[1] == "llama-3.3-70b-versatile" and svc.ai.slot("chat", True)[1] == "llama-3.3-70b-versatile"
    assert all(ok for _, ok, _ in await svc.ai.probe("docs"))


# --- Prova ---------------------------------------------------------------------


async def test_probe_reports_primary_and_backup_with_service_and_model(world):
    svc = world(ai=lambda r: completion("ok"))
    assign(svc, "docs", add_groq(svc), "qwen/qwen3.8-27b")
    assign(svc, "docs", add_gemini(svc), "gemini-3.8-flash", backup=True)
    results = await svc.ai.probe("docs")
    assert [(label, ok, detail) for label, ok, detail in results] == [
        ("Lettura dei documenti · principale · Groq · qwen/qwen3.8-27b", True, "funziona"),
        ("Lettura dei documenti · riserva · Gemini · gemini-3.8-flash", True, "funziona"),
    ]


async def test_probe_says_what_is_missing(world):
    svc = world()
    assert (await svc.ai.probe("chat")) == [("Domande e risposte · principale", False, "Nessun servizio scelto: sceglilo in «Modelli da usare».")]
    svc.settings.update({"ai_chat_service": str(add_groq(svc).id)})
    (label, ok, detail), = await svc.ai.probe("chat")
    assert not ok and "nessun modello" in label and detail == "Nessun modello scelto."


async def test_probe_of_a_model_without_vision_tells_what_to_press(world):
    svc = world(ai=vision_only())
    assign(svc, "docs", add_groq(svc), "llama-3.3-70b-versatile")
    (_, ok, detail), = await svc.ai.probe("docs")
    assert not ok and "non sa leggere le immagini" in detail and "Scegli di nuovo in automatico" in detail


async def test_probe_shows_the_real_quota_reason_of_a_gemini_model(world):
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generate_content_free_tier_requests", "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}]}}
    svc = world(gemini=lambda r: httpx.Response(429, json=body))
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash")
    (_, ok, detail), = await svc.ai.probe("chat")
    assert not ok and "429" in detail and "limite 20" in detail


# --- Tetto dei dati, riepilogo e chiavi richieste --------------------------------------


async def test_context_budget_is_the_smallest_among_the_services_that_could_answer(world):
    svc = world()
    assert svc.ai.context_budget() == 9000  # nessun servizio: il valore impostato
    gemini, groq = add_gemini(svc), add_groq(svc)
    assign(svc, "chat", gemini, "g")
    assert svc.ai.context_budget() == 60000
    assign(svc, "chat", groq, "m", backup=True)
    assert svc.ai.context_budget() == 9000  # la riserva ha limiti più bassi: si sta nel più piccolo
    svc.settings.update({"ai_context_chars": "20000"})
    assert svc.ai.context_budget() == 20000
    svc.settings.update({"ai_context_chars": "50"})
    assert svc.ai.context_budget() == 2000
    svc.settings.update({"ai_context_chars": "boh"})
    assert svc.ai.context_budget() == 9000


async def test_summary_lists_primary_and_backup_in_words(world):
    svc = world()
    assert svc.ai.summary() == [("Lettura dei documenti", "servizio da scegliere", ""), ("Domande e risposte", "servizio da scegliere", "")]
    assign(svc, "chat", add_groq(svc), "openai/gpt-oss-120b")
    assign(svc, "chat", add_gemini(svc), "gemini-3.8-flash", backup=True)
    assert svc.ai.summary()[1] == ("Domande e risposte", "Groq · openai/gpt-oss-120b", "Gemini · gemini-3.8-flash")


async def test_missing_for_run_follows_the_services_and_models_chosen(world):
    svc = world()
    assert svc.missing_for_run() == ["Token del bot Telegram", "Servizio AI per «Lettura dei documenti»", "Servizio AI per «Domande e risposte»"]
    groq = add_groq(svc)
    svc.settings.update({"ai_docs_service": str(groq.id), "ai_chat_service": str(groq.id), "bot_token": "1:abc"})
    assert svc.missing_for_run() == ["Modello per «Lettura dei documenti»", "Modello per «Domande e risposte»"]
    assign(svc, "docs", groq, "qwen/qwen3.8-27b")
    gemini = add_gemini(svc)
    svc.db.execute("UPDATE ai_services SET api_key = '' WHERE id = ?", (gemini.id,))
    assign(svc, "chat", svc.ai_services.get(gemini.id), "m")
    assert svc.missing_for_run() == ["Chiave di Gemini"]
    assign(svc, "chat", groq, "llama-3.3-70b-versatile")
    assert svc.missing_for_run() == []


# --- Dal documento al database, e dalla domanda alla risposta -------------------------


async def test_a_photo_read_by_groq_ends_up_saved_like_any_other(world):
    svc = world(ai=lambda r: completion(appuntamento(date="2027-01-15", time="10:00").model_dump_json()))
    assign(svc, "docs", add_groq(svc), "qwen/qwen3.8-27b")
    outcome = await svc.documents.process(svc.mario, JPEG, "image/jpeg")
    assert "«Visita cardiologica»" in outcome.text and "15/01/2027 alle 10:00" in outcome.text
    assert svc.db.execute("SELECT starts_at FROM appointments")[0]["starts_at"] == "2027-01-15T10:00"
    assert all(c[0] == "ai" for c in svc.calls)


async def test_a_question_answered_through_the_whole_consultant_carries_only_what_fits_the_budget(world):
    sent = []

    def ai(request):
        sent.append(json.loads(request.content)["messages"][1]["content"])
        return completion("Tutto nella norma.")

    svc = world(ai=ai)
    assign(svc, "chat", add_groq(svc), "llama-3.3-70b-versatile")
    for n in range(1, 16):
        svc.records.add_document(svc.mario.id, 111, "referto", "", f"2025-01-{n:02d}", "", "")
        doc = svc.db.execute("SELECT max(id) AS id FROM documents")[0]["id"]
        svc.db.execute("INSERT INTO lab_results (document_id, user_id, result_date, name, value) VALUES (?, ?, '', ?, '1')", (doc, svc.mario.id, "Analisi" + "x" * 400 + str(n)))
    assert await svc.consultant.ask(svc.mario, question="Come sto?") == "Tutto nella norma."
    assert len(sent[0]) < 9000 + 1500  # dati (entro il tetto) + intestazione e domanda
