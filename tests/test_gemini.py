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
        svc.settings.update({"gemini_api_key": "AIza-test", "gemini_model": "gemini-test-flash"})
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
        (429, "troppe richieste"),
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
