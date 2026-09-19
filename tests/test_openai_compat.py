import base64
import json

import httpx
import pytest

from famiglia.ai import AiError, Endpoint
from famiglia.openai_compat import OpenAICompat, _seconds_in, _tiny_png, extract_pdf_text

from helpers import make_pdf, referto

JPEG = b"\xff\xd8\xff fake jpeg"
GROQ = Endpoint("groq", "Groq", "https://api.groq.com/openai/v1", "gsk_test", "llama-test")


def completion(text):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def error(status, message, headers=None):
    return httpx.Response(status, json={"error": {"message": message}}, headers=headers or {})


@pytest.fixture
def api():
    """Client compatibile OpenAI collegato a un server finto: registra le richieste e le attese."""

    def _make(respond):
        seen: list[httpx.Request] = []
        slept: list[float] = []

        def transport(request):
            seen.append(request)
            return respond(request)

        async def no_wait(seconds):
            slept.append(seconds)

        client = OpenAICompat(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        client._sleep = no_wait
        return client, seen, slept

    return _make


def body_of(request):
    return json.loads(request.content)


# --- Elenco modelli ------------------------------------------------------------


async def test_list_models_reads_the_provider_list(api):
    client, seen, _ = api(lambda r: httpx.Response(200, json={"object": "list", "data": [{"id": "llama-b"}, {"id": "llama-a"}, {"id": "llama-a"}]}))
    assert await client.list_models(GROQ) == ["llama-a", "llama-b"]
    assert str(seen[0].url) == "https://api.groq.com/openai/v1/models" and seen[0].headers["authorization"] == "Bearer gsk_test"


async def test_a_provider_without_a_model_list_asks_to_type_the_name(api):
    client, _, _ = api(lambda r: error(404, "not found"))
    with pytest.raises(AiError) as exc:
        await client.list_models(GROQ)
    assert "scrivi il nome del modello a mano" in exc.value.user_message


async def test_list_models_with_a_bad_key(api):
    client, _, _ = api(lambda r: error(401, "Invalid API Key"))
    with pytest.raises(AiError) as exc:
        await client.list_models(GROQ)
    assert "chiave di Groq non è valida" in exc.value.user_message and "401" in str(exc.value)


# --- Domande -------------------------------------------------------------------


async def test_answer_sends_the_saved_data_and_no_web_instructions(api):
    client, seen, _ = api(lambda r: completion("  Il colesterolo è alto.  "))
    assert await client.answer(GROQ, "## Mario\n- 25/10/2025: Colesterolo 240", "Mario Rossi", "Com'è il colesterolo?") == "Il colesterolo è alto."
    request = seen[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    body = body_of(request)
    assert body["model"] == "llama-test" and body["temperature"] == 0.3 and "tools" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and "Non hai accesso al web" in system["content"] and "ricerca web" not in system["content"]
    assert "Scrive Mario Rossi" in user["content"] and "Colesterolo 240" in user["content"] and "Com'è il colesterolo?" in user["content"]


async def test_visible_reasoning_is_removed_from_the_answer(api):
    client, _, _ = api(lambda r: completion("<think>fammi pensare...\ne ripensare</think>\nRisposta pulita."))
    assert await client.answer(GROQ, "", "Mario", "?") == "Risposta pulita."


async def test_empty_or_odd_replies_are_reported(api):
    client, _, _ = api(lambda r: completion(None))
    with pytest.raises(AiError) as empty:
        await client.answer(GROQ, "", "Mario", "?")
    assert "Groq non ha dato nessuna risposta" in empty.value.user_message
    client, _, _ = api(lambda r: httpx.Response(200, json={"strano": True}))
    with pytest.raises(AiError) as odd:
        await client.answer(GROQ, "", "Mario", "?")
    assert "formato inatteso" in odd.value.user_message and "strano" in str(odd.value)


# --- Documenti -----------------------------------------------------------------


async def test_image_is_sent_as_a_data_uri_and_json_is_parsed(api):
    expected = referto(patient="Mario Rossi", details="Conclusioni: nella norma")
    client, seen, _ = api(lambda r: completion("```json\n" + expected.model_dump_json() + "\n```"))
    result = await client.analyze_document(GROQ, JPEG, "image/jpeg")
    assert result == expected
    body = body_of(seen[0])
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0
    assert "Rispondi SOLO con un oggetto JSON" in body["messages"][0]["content"] and "details" in body["messages"][0]["content"]
    image = body["messages"][1]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,") and base64.b64decode(image.split(",", 1)[1]) == JPEG


async def test_models_that_omit_fields_or_use_numbers_are_tolerated(api):
    reply = json.dumps({
        "kind": "Referto", "patient_name": None, "document_date": "2025-10-25", "summary": "Esami",
        "lab_results": [{"name": "Glicemia", "value": 95, "unit": "mg/dL"}, {"name": "x"}, "spazzatura"],
        "details": ["Conclusioni: bene", "Controllo tra 12 mesi"],
    })
    client, _, _ = api(lambda r: completion(reply))
    result = await client.analyze_document(GROQ, JPEG, "image/png")
    assert result.kind == "referto" and result.patient_name == "" and result.appointment is None
    assert [(r.name, r.value, r.reference) for r in result.lab_results] == [("Glicemia", "95", ""), ("x", "", "")]
    assert result.details == "Conclusioni: bene\nControllo tra 12 mesi"


async def test_appointment_is_parsed(api):
    reply = json.dumps({"kind": "appuntamento", "appointment": {"title": "Visita", "date": "2027-01-15", "time": "10:00", "place": "Ospedale"}})
    client, _, _ = api(lambda r: completion(reply))
    result = await client.analyze_document(GROQ, JPEG, "image/jpeg")
    assert (result.appointment.title, result.appointment.time, result.appointment.notes) == ("Visita", "10:00", "")


@pytest.mark.parametrize("reply", ["non è json", "{\"kind\": \"sconosciuto\"}", "{ rotto", ""])
async def test_unusable_model_output_is_reported(api, reply):
    client, _, _ = api(lambda r: completion(reply or "x"))
    with pytest.raises(AiError) as exc:
        await client.analyze_document(GROQ, JPEG, "image/jpeg")
    assert "Non sono riuscito a interpretare il documento" in exc.value.user_message


async def test_json_mode_not_supported_retries_without_it(api):
    calls = []

    def respond(request):
        calls.append("response_format" in body_of(request))
        return error(400, "'response_format' is not supported with this model") if calls[-1] else completion(referto().model_dump_json())

    client, _, _ = api(respond)
    assert (await client.analyze_document(GROQ, JPEG, "image/jpeg")).kind == "referto"
    assert calls == [True, False]


async def test_pdf_with_text_is_sent_as_text_not_as_an_image(api):
    client, seen, _ = api(lambda r: completion(referto().model_dump_json()))
    await client.analyze_document(GROQ, make_pdf("Glicemia 95 mg/dL del 25/10/2025"), "application/pdf")
    user = body_of(seen[0])["messages"][1]["content"]
    assert isinstance(user, str) and "Glicemia 95 mg/dL" in user and "image_url" not in json.dumps(body_of(seen[0]))


async def test_scanned_pdf_and_unsupported_formats_are_refused_without_calling_out(api):
    client, seen, _ = api(lambda r: completion("{}"))
    with pytest.raises(AiError, match="è una scansione"):
        await client.analyze_document(GROQ, b"%PDF-1.4 rotto", "application/pdf")
    with pytest.raises(AiError, match="JPEG o PNG"):
        await client.analyze_document(GROQ, b"x", "image/heic")
    assert seen == []


def test_pdf_text_extraction():
    assert "Glicemia 95" in extract_pdf_text(make_pdf("Glicemia 95 mg/dL"))
    assert extract_pdf_text(b"non un pdf") == ""


# --- Limiti e errori -----------------------------------------------------------


async def test_429_with_a_short_retry_after_waits_and_retries(api):
    answers = iter([error(429, "Rate limit reached", {"retry-after": "3"}), completion("Ecco.")])
    client, seen, slept = api(lambda r: next(answers))
    assert await client.answer(GROQ, "", "Mario", "?") == "Ecco." and slept == [4.0] and len(seen) == 2


async def test_429_wait_is_read_from_the_message_when_there_is_no_header(api):
    answers = iter([error(429, "Rate limit reached. Please try again in 7.5s."), completion("Ecco.")])
    client, _, slept = api(lambda r: next(answers))
    assert await client.answer(GROQ, "", "Mario", "?") == "Ecco."
    assert slept == [8.5]


async def test_a_wait_of_over_45_seconds_read_from_the_message_is_not_waited_for(api):
    client, seen, slept = api(lambda r: error(429, "Rate limit reached. Please try again in 1m3.5s."))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert slept == [] and len(seen) == 1 and exc.value.quota


async def test_429_with_a_long_wait_says_when_to_retry_and_flags_quota(api):
    client, seen, slept = api(lambda r: error(429, "Rate limit reached for requests per day. Please try again in 14m8.3s."))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert exc.value.quota and "raggiunto il limite di richieste" in exc.value.user_message and "circa 14 minuti" in exc.value.user_message
    assert slept == [] and len(seen) == 1 and "429" in str(exc.value)


async def test_429_gives_up_after_the_retries(api):
    client, seen, slept = api(lambda r: error(429, "slow down", {"retry-after": "2"}))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert exc.value.quota and len(seen) == 3 and slept == [3.0, 3.0]


@pytest.mark.parametrize(
    "status,message,fragment",
    [
        (401, "Invalid API Key", "chiave di Groq non è valida"),
        (403, "forbidden", "non ha i permessi"),
        (404, "model not found", "modello scelto per Groq non esiste"),
        (400, "This model does not support image input", "non sa leggere le immagini"),
        (413, "too large", "troppo grande"),
        (503, "overloaded", "non risponde"),
        (418, "boh", "ha rifiutato la richiesta"),
    ],
)
async def test_http_errors_become_friendly_messages(api, status, message, fragment):
    client, _, _ = api(lambda r: error(status, message))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert fragment in exc.value.user_message and str(status) in str(exc.value)


async def test_network_failure_is_reported(api):
    def down(request):
        raise httpx.ConnectError("rete assente")

    client, _, _ = api(down)
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert "Non riesco a collegarmi a Groq" in exc.value.user_message and "rete assente" in str(exc.value)


@pytest.mark.parametrize("text,seconds", [("try again in 7.2s", 7.2), ("try again in 1m3.5s", 63.5), ("try again in 850ms", 0.85),
                                            ("try again in 2m", 120.0), ("try again in 1h2m", 3720.0), ("nessun tempo", None)])
def test_wait_time_parsing(text, seconds):
    assert _seconds_in(text) == pytest.approx(seconds) if seconds is not None else _seconds_in(text) is None


# --- Vocali e prova ------------------------------------------------------------


async def test_transcribe_sends_the_audio_to_the_whisper_model(api):
    client, seen, _ = api(lambda r: httpx.Response(200, json={"text": " Come sta la glicemia? "}))
    assert await client.transcribe(GROQ, "whisper-large-v3-turbo", b"OggS-voce", "audio/ogg") == "Come sta la glicemia?"
    request = seen[0]
    assert str(request.url).endswith("/audio/transcriptions") and request.headers["content-type"].startswith("multipart/form-data")
    assert b"whisper-large-v3-turbo" in request.content and b"OggS-voce" in request.content


async def test_empty_transcription_is_reported(api):
    client, _, _ = api(lambda r: httpx.Response(200, json={"text": ""}))
    with pytest.raises(AiError, match="capire il messaggio vocale"):
        await client.transcribe(GROQ, "whisper", b"x", "audio/ogg")


async def test_probe_chat_and_docs(api):
    client, seen, _ = api(lambda r: completion("ok"))
    assert await client.probe(GROQ, "chat") == (True, "funziona")
    assert await client.probe(GROQ, "docs") == (True, "funziona")
    assert isinstance(body_of(seen[0])["messages"][0]["content"], str)
    parts = body_of(seen[1])["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body_of(seen[1])["max_tokens"] == 16


async def test_probe_says_why_a_model_cannot_read_images(api):
    client, _, _ = api(lambda r: error(400, "this model does not support image input"))
    ok, detail = await client.probe(GROQ, "docs")
    assert not ok and "non sa leggere le immagini" in detail and "400" in detail


def test_the_test_image_is_a_valid_png():
    png = _tiny_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")


# --- Richieste troppo grandi per il limite di token ------------------------------


async def test_413_for_tokens_says_the_request_is_too_big_for_the_plan_not_the_photo(api):
    client, seen, slept = api(lambda r: error(413, "Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 9500"))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert "troppo grande per il limite di token al minuto" in exc.value.user_message and "Modelli IA" in exc.value.user_message
    assert exc.value.quota and slept == [] and len(seen) == 1


async def test_a_plain_413_is_still_about_the_file(api):
    client, _, _ = api(lambda r: error(413, "payload too large"))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert "foto più leggera" in exc.value.user_message and not exc.value.quota


async def test_429_whose_request_alone_exceeds_the_limit_is_not_retried(api):
    client, seen, slept = api(lambda r: error(429, "Rate limit reached on tokens per minute (TPM): Limit 8000, Used 0, Requested 9500. Please try again in 1s.", {"retry-after": "1"}))
    with pytest.raises(AiError) as exc:
        await client.answer(GROQ, "", "Mario", "?")
    assert "troppo grande" in exc.value.user_message and slept == [] and len(seen) == 1


async def test_429_within_the_limit_is_an_ordinary_wait(api):
    answers = iter([error(429, "Rate limit reached on TPM: Limit 8000, Used 7600, Requested 900. Please try again in 3s.", {"retry-after": "3"}), completion("Ecco.")])
    client, _, slept = api(lambda r: next(answers))
    assert await client.answer(GROQ, "", "Mario", "?") == "Ecco." and slept == [4.0]


async def test_the_model_list_has_a_short_timeout_but_normal_requests_do_not(api):
    client, seen, _ = api(lambda r: httpx.Response(200, json={"data": [{"id": "a"}]}) if r.url.path.endswith("/models") else completion("ok"))
    await client.list_models(GROQ)
    await client.answer(GROQ, "", "Mario", "?")
    assert seen[0].extensions["timeout"]["read"] == 20
    assert seen[1].extensions["timeout"]["read"] != 20  # le richieste vere usano il tempo normale
