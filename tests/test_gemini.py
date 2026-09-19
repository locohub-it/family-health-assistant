import base64
import json

import httpx
import pytest

from famiglia.gemini import GeminiError
from famiglia.service import Service

from helpers import referto

JPEG = b"\xff\xd8\xff fake jpeg"


def candidate(text: str) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]}


@pytest.fixture
def gemini(tmp_path, root):
    """Gemini vero (SDK google-genai) collegato a un server finto: si controlla cosa parte e cosa torna."""

    def _make(respond):
        seen: list[httpx.Request] = []

        def transport(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return respond(request)

        svc = Service(
            tmp_path / "data", "chiave-di-test", root, gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        )
        svc.settings.update(
            {"gemini_api_key": "AIza-test", "gemini_model": "gemini-test-flash", "gemini_fallback_model": "gemini-test-lite"}
        )
        slept: list[float] = []

        async def no_wait(seconds):  # i test non aspettano davvero
            slept.append(seconds)

        svc.gemini._sleep = no_wait
        svc.gemini.slept, svc.gemini.service = slept, svc
        return svc.gemini, seen

    return _make


async def test_request_carries_image_schema_and_model_and_reply_is_parsed(gemini):
    expected = referto(patient="Mario Rossi")
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate(expected.model_dump_json())))

    result = await client.analyze_document(JPEG, "image/jpeg")

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
    assert "lab_results" in schema and "appuntamento" in schema and "illeggibile" in schema
    assert "referto" in json.dumps(body["systemInstruction"])
    assert request.headers["x-goog-api-key"] == "AIza-test"


@pytest.mark.parametrize(
    "status,fragment",
    [
        (429, "molto occupato"),
        (403, "chiave Gemini non è valida"),
        (404, "modello Gemini scelto non esiste"),
        (503, "non risponde"),
    ],
)
async def test_api_errors_become_friendly_messages(gemini, status, fragment):
    body = {"error": {"code": status, "message": "boom", "status": "ERR"}}
    client, _ = gemini(lambda r: httpx.Response(status, json=body))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert fragment in exc.value.user_message


async def test_network_failure_is_reported(gemini):
    def down(request):
        raise httpx.ConnectError("rete assente")

    client, _ = gemini(down)
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert "connessione" in exc.value.user_message


async def test_garbage_reply_is_reported(gemini):
    client, _ = gemini(lambda r: httpx.Response(200, json=candidate("non è json")))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert "interpretare" in exc.value.user_message


async def test_empty_reply_is_reported(gemini):
    client, _ = gemini(lambda r: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}))
    with pytest.raises(GeminiError):
        await client.analyze_document(JPEG, "image/jpeg")


async def test_missing_key_is_reported_without_calling_out(tmp_path, root):
    svc = Service(tmp_path / "data", "chiave-di-test", root)
    with pytest.raises(GeminiError) as exc:
        await svc.gemini.analyze_document(JPEG, "image/jpeg")
    assert "manca la chiave Gemini" in exc.value.user_message


async def test_changing_the_key_from_the_panel_takes_effect(gemini):
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate(referto().model_dump_json())))
    await client.analyze_document(JPEG, "image/jpeg")
    client._settings.update({"gemini_api_key": "AIza-nuova"})
    await client.analyze_document(JPEG, "image/jpeg")
    assert [r.headers["x-goog-api-key"] for r in seen] == ["AIza-test", "AIza-nuova"]


# --- Quota e modello di riserva ------------------------------------------------


def quota_body(quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", value="10", retry="2s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generate_content_free_tier_requests", "quotaId": quota_id, "quotaValue": value}]}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED", "details": details}}


def model_of(request: httpx.Request) -> str:
    return request.url.path.split("/models/")[1].split(":")[0]


def ok(text=None):
    return httpx.Response(200, json=candidate(text or referto().model_dump_json()))


async def test_per_minute_limit_waits_for_the_time_google_says_and_retries(gemini):
    answers = iter([httpx.Response(429, json=quota_body(retry="2s")), ok()])
    client, seen = gemini(lambda r: next(answers))
    result = await client.analyze_document(JPEG, "image/jpeg")
    assert result.kind == "referto"
    assert client.slept == [3.0]  # 2 secondi indicati + 1 di margine
    assert [model_of(r) for r in seen] == ["gemini-test-flash", "gemini-test-flash"]  # stesso modello, nessuna riserva


async def test_daily_limit_is_not_waited_for_and_switches_to_the_fallback_model(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20", "3600s")) if model_of(r) == "gemini-test-flash" else ok())
    result = await client.analyze_document(JPEG, "image/jpeg")
    assert result.kind == "referto" and client.slept == []
    assert [model_of(r) for r in seen] == ["gemini-test-flash", "gemini-test-lite"]
    assert any("non disponibile, ho risposto con gemini-test-lite" in r["detail"] for r in client.service.recent_activity(5))


async def test_daily_limit_on_every_model_gives_a_clear_message(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert "richieste giornaliere" in exc.value.user_message and "fatturazione" in exc.value.user_message
    assert len(seen) == 2 and client.slept == []
    assert "RESOURCE_EXHAUSTED" in str(exc.value)  # la causa vera resta nel dettaglio per il registro


async def test_zero_quota_says_the_model_is_not_available_on_this_plan(gemini):
    client, _ = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "0")))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert "non è disponibile con il piano" in exc.value.user_message


async def test_429_with_no_information_gets_one_quick_retry_then_tries_the_fallback(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json={"error": {"code": 429, "message": "boh", "status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert "molto occupato" in exc.value.user_message
    assert [model_of(r) for r in seen] == ["gemini-test-flash"] * 2 + ["gemini-test-lite"] * 2
    assert client.slept == [6.0, 6.0]


GENERIC_QUOTA = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota, please check your plan and billing details."}}


async def test_plain_quota_message_is_not_waited_for_and_says_the_free_requests_are_over(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=GENERIC_QUOTA))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
    assert client.slept == [] and len(seen) == 2  # niente attese: un modello, poi la riserva
    assert "richieste gratuite di Gemini sono esaurite" in exc.value.user_message and "fatturazione" in exc.value.user_message
    assert "exceeded your current quota" in str(exc.value)


async def test_the_registry_detail_names_the_quota_that_was_hit(gemini):
    body = quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20", "3600s")
    body["error"]["details"][0]["violations"][0]["quotaMetric"] = "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    body["error"]["details"][0]["violations"][0]["quotaDimensions"] = {"model": "gemini-test-flash"}
    client, _ = gemini(lambda r: httpx.Response(429, json=body))
    with pytest.raises(GeminiError) as exc:
        await client.analyze_document(JPEG, "image/jpeg")
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
    from famiglia.gemini import Quota, wait_before_retry

    assert wait_before_retry(Quota(kind, retry), attempt) == expected


async def test_a_wait_longer_than_the_limit_is_not_waited_for(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body(retry="300s")) if model_of(r) == "gemini-test-flash" else ok())
    await client.analyze_document(JPEG, "image/jpeg")
    assert client.slept == [] and len(seen) == 2


@pytest.mark.parametrize("status", [404, 503])
async def test_missing_or_down_model_falls_back(gemini, status):
    client, seen = gemini(lambda r: httpx.Response(status, json={"error": {"code": status, "message": "x", "status": "X"}}) if model_of(r) == "gemini-test-flash" else ok())
    assert (await client.analyze_document(JPEG, "image/jpeg")).kind == "referto"
    assert [model_of(r) for r in seen] == ["gemini-test-flash", "gemini-test-lite"]


async def test_no_fallback_configured_means_a_single_model(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=quota_body("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "20")))
    client._settings.update({"gemini_fallback_model": ""})
    with pytest.raises(GeminiError):
        await client.analyze_document(JPEG, "image/jpeg")
    assert len(seen) == 1


async def test_bad_key_does_not_try_the_fallback(gemini):
    client, seen = gemini(lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(GeminiError, match="403"):
        await client.analyze_document(JPEG, "image/jpeg")
    assert len(seen) == 1


async def test_no_more_than_two_calls_run_at_once(tmp_path, root):
    import asyncio

    running = peak = 0

    async def slow(request):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return ok()

    svc = Service(tmp_path / "data", "chiave-di-test", root, gemini_http=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    svc.settings.update({"gemini_api_key": "AIza-test"})
    await asyncio.gather(*(svc.gemini.analyze_document(JPEG, "image/jpeg") for _ in range(6)))
    assert peak == 2


# --- Domande: la ricerca web ha una quota sua ----------------------------------


def has_tools(request: httpx.Request) -> bool:
    return bool(json.loads(request.content).get("tools"))


async def test_answer_uses_web_search_when_it_works(gemini):
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate("Risposta con web.")))
    assert await client.answer("## Mario", "Mario", question="Ciao?") == "Risposta con web."
    assert has_tools(seen[0]) and len(seen) == 1


async def test_when_the_web_search_quota_is_over_the_answer_uses_the_saved_data_only(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=GENERIC_QUOTA) if has_tools(r) else httpx.Response(200, json=candidate("Dai dati salvati.")))
    answer = await client.answer("## Mario", "Mario", question="Come va?")
    assert answer.startswith("Dai dati salvati.") and "non riesco a cercare sul web" in answer
    assert "«in generale»" in answer and "meno aggiornate" in answer
    assert [has_tools(r) for r in seen] == [True, True, False]  # con web su entrambi i modelli, poi senza web
    assert any("Ricerca web non disponibile" in r["detail"] for r in client.service.recent_activity(10))


async def test_web_search_can_be_switched_off_from_the_panel(gemini):
    client, seen = gemini(lambda r: httpx.Response(200, json=candidate("Senza web.")))
    client._settings.update({"consult_web_search": "0"})
    assert await client.answer("## Mario", "Mario", question="Ciao?") == "Senza web."
    assert not has_tools(seen[0])


async def test_if_even_the_answer_without_web_search_is_over_quota_it_gives_up(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=GENERIC_QUOTA))
    with pytest.raises(GeminiError) as exc:
        await client.answer("## Mario", "Mario", question="Come va?")
    assert "richieste gratuite" in exc.value.user_message
    assert [has_tools(r) for r in seen] == [True, True, False, False]  # nessun ciclo infinito


async def test_a_bad_key_does_not_trigger_the_web_search_fallback(gemini):
    client, seen = gemini(lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(GeminiError, match="403"):
        await client.answer("## Mario", "Mario", question="Come va?")
    assert len(seen) == 1


# --- Prova dal pannello ---------------------------------------------------------


async def test_check_reports_each_model_and_the_web_search_separately(gemini):
    client, seen = gemini(lambda r: httpx.Response(429, json=GENERIC_QUOTA) if has_tools(r) else httpx.Response(200, json=candidate("ok")))
    results = await client.check()
    assert [(label, ok) for label, ok, _ in results] == [
        ("Modello principale (gemini-test-flash)", True),
        ("Modello di riserva (gemini-test-lite)", True),
        ("Ricerca web con gemini-test-flash", False),
    ]
    assert "exceeded your current quota" in results[2][2] and "429" in results[2][2]
    assert client.slept == []  # è una prova: nessuna attesa né nuovi tentativi


async def test_check_with_a_missing_model_shows_the_404(gemini):
    client, _ = gemini(lambda r: httpx.Response(404, json={"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}}))
    results = await client.check()
    assert all(not ok and "404" in detail for _, ok, detail in results)


async def test_check_without_a_key_reports_it(tmp_path, root):
    svc = Service(tmp_path / "data", "chiave-di-test", root)
    with pytest.raises(GeminiError, match="manca la chiave"):
        await svc.gemini.check()


# --- Modello per ruolo ed elenco modelli ---------------------------------------


async def test_a_model_chosen_for_the_role_overrides_the_default(gemini):
    client, seen = gemini(lambda r: ok())
    await client.analyze_document(JPEG, "image/jpeg", model="gemini-scelto-dal-ruolo")
    await client.answer("## Mario", "Mario", question="Ciao?", model="gemini-per-le-domande")
    assert [model_of(r) for r in seen] == ["gemini-scelto-dal-ruolo", "gemini-per-le-domande"]


async def test_list_models_returns_only_text_generators_without_the_prefix(gemini):
    def respond(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        ]})

    client, _ = gemini(respond)
    assert await client.list_models() == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


async def test_list_models_with_a_bad_key_says_so(gemini):
    client, _ = gemini(lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}))
    with pytest.raises(GeminiError, match="403") as exc:
        await client.list_models()
    assert "chiave Gemini non è valida" in exc.value.user_message


async def test_extraction_now_carries_details(gemini):
    from helpers import altro

    text = altro("ricetta", details="Occhio destro: sfera -2.50\nOcchio sinistro: sfera -3.00").model_dump_json()
    client, _ = gemini(lambda r: httpx.Response(200, json=candidate(text)))
    result = await client.analyze_document(JPEG, "image/jpeg")
    assert "sfera -2.50" in result.details
