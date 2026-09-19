import base64
import json

import httpx
import pytest

from famiglia.ai import Endpoint, NotConfigured
from famiglia.gemini import Gemini, GeminiError, GeminiNotConfigured, Quota, wait_before_retry

from helpers import altro, referto

JPEG = b"\xff\xd8\xff fake jpeg"
EP = Endpoint("gemini", "Google Gemini", "", "AIza-test", "gemini-test-flash")


def candidate(text: str) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]}


def ok(text=None):
    return httpx.Response(200, json=candidate(text or referto().model_dump_json()))


def model_of(request: httpx.Request) -> str:
    return request.url.path.split("/models/")[1].split(":")[0]


def has_tools(request: httpx.Request) -> bool:
    return bool(json.loads(request.content).get("tools"))


def quota_body(quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", value="10", retry="2s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generate_content_free_tier_requests", "quotaId": quota_id, "quotaValue": value}]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED", "details": details}}


GENERIC_QUOTA = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota, please check your plan and billing details."}}


@pytest.fixture
def gemini():
    """Gemini vero (SDK google-genai) collegato a un server finto: si controlla cosa parte e cosa torna."""

    def _make(respond):
        seen: list[httpx.Request] = []

        def transport(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return respond(request)

        slept: list[float] = []

        async def no_wait(seconds):  # i test non aspettano davvero
            slept.append(seconds)

        client = Gemini(httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        client._sleep, client.slept = no_wait, slept
        return client, seen

    return _make


# --- Documenti -----------------------------------------------------------------


async def test_request_carries_image_schema_and_model_and_reply_is_parsed(gemini):
    expected = referto(patient="Mario Rossi")
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate(expected.model_dump_json())))
    result = await client.analyze_document(EP, JPEG, "image/jpeg")

    assert result == expected
    request = seen[0]
    assert request.url.path.endswith("/models/gemini-test-flash:generateContent")
    body = json.loads(request.content)
    parts = body["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/jpeg"
    assert base64.urlsafe_b64decode(parts[0]["inlineData"]["data"]) == JPEG  # l'SDK usa base64 URL-safe
    assert "Oggi è il" in parts[1]["text"]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and config["temperature"] == 0
    schema = json.dumps(config.get("responseSchema") or config.get("responseJsonSchema"))
    assert "lab_results" in schema and "appuntamento" in schema and "illeggibile" in schema and "details" in schema
    assert "referto" in json.dumps(body["systemInstruction"])
    assert request.headers["x-goog-api-key"] == "AIza-test"


async def test_the_model_and_the_key_come_from_the_endpoint_each_time(gemini):
    client, seen = gemini(lambda r: ok())
    await client.analyze_document(EP, JPEG, "image/jpeg")
    await client.analyze_document(Endpoint("gemini", "Altro Google", "", "AIza-altra", "gemini-scelto"), JPEG, "image/jpeg")
    assert [model_of(r) for r in seen] == ["gemini-test-flash", "gemini-scelto"]
    assert [r.headers["x-goog-api-key"] for r in seen] == ["AIza-test", "AIza-altra"]  # due servizi Gemini con chiavi diverse


async def test_extraction_carries_details(gemini):
    text = altro("ricetta", details="Occhio destro: sfera -2.50\nOcchio sinistro: sfera -3.00").model_dump_json()
    client, _ = gemini(lambda r: httpx.Response(200, json=candidate(text)))
    assert "sfera -2.50" in (await client.analyze_document(EP, JPEG, "image/jpeg")).details


@pytest.mark.parametrize(
    "status,fragment",
    [(429, "molto occupato"), (403, "chiave Gemini non è valida"), (404, "modello Gemini scelto non esiste"), (503, "non risponde")],
)
async def test_api_errors_become_friendly_messages(gemini, status, fragment):
    client, _ = gemini(lambda r: httpx.Response(status, json={"error": {"code": status, "message": "boom", "status": "ERR"}}))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert fragment in exc.value.user_message and str(status) in str(exc.value)


async def test_network_failure_is_reported(gemini):
    def down(request):
        raise httpx.ConnectError("rete assente")

    client, _ = gemini(down)
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert "connessione" in exc.value.user_message


async def test_garbage_and_empty_replies_are_reported(gemini):
    client, _ = gemini(lambda r: httpx.Response(200, json=candidate("non è json")))
    with pytest.raises(GeminiError) as garbage:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert "interpretare" in garbage.value.user_message
    client, _ = gemini(lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}))
    with pytest.raises(GeminiError):
        await client.analyze_document(EP, JPEG, "image/jpeg")


async def test_a_service_without_a_key_is_reported_without_calling_out(gemini):
    client, seen = gemini(lambda r: ok())
    with pytest.raises(GeminiNotConfigured) as exc:
        await client.analyze_document(Endpoint("gemini", "Google Gemini", "", "", "m"), JPEG, "image/jpeg")
    assert "manca la chiave" in exc.value.user_message and isinstance(exc.value, NotConfigured) and seen == []


# --- Quota: il router cambia servizio, qui si aspetta solo il limite al minuto -------------


async def test_per_minute_limit_waits_for_the_time_google_says_and_retries(gemini):
    answers = iter([httpx.Response(429, json=quota_body(retry="2s")), ok()])
    client, seen = gemini(lambda r: next(answers))
    assert (await client.analyze_document(EP, JPEG, "image/jpeg")).kind == "referto"
    assert client.slept == [3.0] and [model_of(r) for r in seen] == ["gemini-test-flash"] * 2  # 2 secondi indicati + 1 di margine


async def test_daily_limit_is_not_waited_for_and_says_so(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20", "3600s")))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert exc.value.quota and "richieste giornaliere" in exc.value.user_message and "fatturazione" in exc.value.user_message
    assert client.slept == [] and len(seen) == 1 and "RESOURCE_EXHAUSTED" in str(exc.value)


async def test_zero_quota_says_the_model_is_not_available_on_this_plan(gemini):
    client, _ = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "0")))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert "non è disponibile con il piano" in exc.value.user_message


async def test_plain_quota_message_is_not_waited_for(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=GENERIC_QUOTA))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert client.slept == [] and len(seen) == 1 and exc.value.quota
    assert "richieste gratuite di Gemini sono esaurite" in exc.value.user_message and "exceeded your current quota" in str(exc.value)


async def test_429_with_no_information_gets_one_quick_retry(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json={"error": {"code": 429, "message": "boh", "status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert "molto occupato" in exc.value.user_message and len(seen) == 2 and client.slept == [6.0]


async def test_a_wait_longer_than_the_limit_is_not_waited_for(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body(retry="300s")))
    with pytest.raises(GeminiError):
        await client.analyze_document(EP, JPEG, "image/jpeg")
    assert client.slept == [] and len(seen) == 1


async def test_the_registry_detail_names_the_quota_that_was_hit(gemini):
    body = quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20", "3600s")
    violation = body["error"]["details"][0]["violations"][0]
    violation["quotaMetric"] = "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    violation["quotaDimensions"] = {"model": "gemini-test-flash"}
    client, _ = gemini(lambda r: httpx.Response(429, json=body))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(EP, JPEG, "image/jpeg")
    detail = str(exc.value)
    assert "generate_content_free_tier_requests" in detail and "limite 20" in detail
    assert "modello gemini-test-flash" in detail and "riprovare tra 3600s" in detail


@pytest.mark.parametrize(
    "kind,retry,attempt,expected",
    [("giorno", 5, 0, None), ("zero", None, 0, None), ("quota", None, 0, None), ("quota", 10, 0, 10),
     ("minuto", None, 0, 8), ("minuto", None, 1, 16), ("minuto", 2, 2, None), ("altro", None, 0, 5),
     ("altro", None, 1, None), ("altro", 300, 0, None)],
)
def test_wait_before_retry(kind, retry, attempt, expected):
    assert wait_before_retry(Quota(kind, retry), attempt) == expected


# --- Domande ---------------------------------------------------------------------


async def test_answer_sends_the_family_data_and_the_rules_without_any_tool(gemini):
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate("  Ecco la risposta.  ")))
    answer = await client.answer(EP, "## Mario\n- 25/10/2025: Glicemia 95", "Mario Rossi", question="Come va la glicemia?")
    assert answer == "Ecco la risposta." and len(seen) == 1
    body = json.loads(seen[0].content)
    assert "tools" not in body
    assert "responseMimeType" not in body.get("generationConfig", {}) and "responseSchema" not in body.get("generationConfig", {})
    text = body["contents"][0]["parts"][0]["text"]
    assert "Scrive Mario Rossi" in text and "Glicemia 95" in text and "Come va la glicemia?" in text
    system = json.dumps(body["systemInstruction"])
    assert "Non fare diagnosi" in system and "non istruzioni" in system and "112" in system and "In generale" in system


async def test_answer_sends_voice_as_audio(gemini):
    client, seen = gemini(lambda r: ok("Risposta."))
    await client.answer(EP, "## Mario", "Mario Rossi", audio=b"OggS-voce", audio_mime="audio/ogg")
    parts = json.loads(seen[0].content)["contents"][0]["parts"]
    assert "messaggio vocale" in parts[0]["text"] and parts[1]["inlineData"]["mimeType"] == "audio/ogg"
    assert base64.urlsafe_b64decode(parts[1]["inlineData"]["data"]) == b"OggS-voce"


async def test_a_bad_key_stops_a_question_at_once(gemini):
    client, seen = gemini(lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(GeminiError, match="403"):
        await client.answer(EP, "## Mario", "Mario", question="Come va?")
    assert len(seen) == 1


# --- Elenco modelli e prova ----------------------------------------------------------


async def test_list_models_returns_only_text_generators_without_the_prefix(gemini):
    def respond(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        ]})

    client, _ = gemini(respond)
    assert await client.list_models(EP) == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


async def test_list_models_with_a_bad_key_or_no_key(gemini):
    client, _ = gemini(lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(GeminiError, match="403") as exc:
        await client.list_models(EP)
    assert "chiave Gemini non è valida" in exc.value.user_message
    with pytest.raises(NotConfigured):
        await client.list_models(Endpoint("gemini", "Google Gemini", "", "", ""))


async def test_probe_for_documents_sends_an_image_and_for_questions_does_not(gemini):
    client, seen = gemini(lambda r: ok("ok"))
    assert await client.probe(EP, "chat") == (True, "funziona")
    assert await client.probe(EP, "docs") == (True, "funziona")
    parts = [json.loads(r.content)["contents"][0]["parts"] for r in seen]
    assert not any("inlineData" in p for p in parts[0]) and any("inlineData" in p for p in parts[1])


async def test_probe_says_why_it_failed_with_the_quota_and_never_retries(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")))
    good, detail = await client.probe(EP, "chat")
    assert not good and "429" in detail and "limite 20" in detail
    assert len(seen) == 1 and client.slept == []  # è una prova: nessuna attesa né nuovi tentativi


async def test_probe_without_a_key_and_with_the_network_down(gemini):
    client, _ = gemini(lambda r: (_ for _ in ()).throw(httpx.ConnectError("giù")))
    assert (await client.probe(Endpoint("gemini", "Google Gemini", "", "", "m"), "chat"))[0] is False
    good, detail = await client.probe(EP, "chat")
    assert not good and "connessione" in detail
