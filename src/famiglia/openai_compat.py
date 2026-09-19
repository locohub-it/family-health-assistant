"""Provider compatibili OpenAI (Groq, DeepSeek, OpenRouter, Ollama...): documenti, domande, vocali, elenco modelli."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from typing import Callable

import httpx
from pydantic import ValidationError

from . import clock
from .ai import AiError, Endpoint
from .ai import tiny_png as _tiny_png
from .gemini import ANALYZE_INSTRUCTIONS, CONSULT_INSTRUCTIONS, Extraction

MAX_PARALLEL_CALLS = 2
MAX_RETRIES = 2
MAX_WAIT_SECONDS = 45
REQUEST_TIMEOUT = 90
LIST_TIMEOUT = 20
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

JSON_RULES = (
    "\nRispondi SOLO con un oggetto JSON valido, senza testo prima o dopo e senza blocchi di codice, con esattamente "
    "questi campi. kind è uno tra appuntamento, referto, ricetta, altro, illeggibile. appointment è null oppure "
    '{"title": "", "date": "AAAA-MM-GG", "time": "HH:MM", "place": "", "notes": ""}. lab_results è una lista, anche vuota.\n'
    '{"kind": "referto", "patient_name": "", "document_date": "AAAA-MM-GG", "summary": "", "details": "", '
    '"appointment": null, "lab_results": [{"name": "", "value": "", "unit": "", "reference": "", "flag": ""}]}'
)


class _FormatRejected(AiError):
    """Il provider non accetta response_format: si riprova senza."""


def extract_pdf_text(data: bytes, max_chars: int = 20000) -> str:
    """Testo di un PDF; vuoto se è una scansione (solo immagini) o non si legge."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:15])
    except Exception:  # noqa: BLE001 - PDF rovinato, protetto o con struttura strana
        return ""
    return text.strip()[:max_chars]


def _message_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:400]
    return str(error or body)[:400]


def _seconds_in(text: str) -> float | None:
    """«Please try again in 1m3.5s» / «in 850ms» / «in 7.2s» → secondi."""
    found = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)(ms|s))?", text)
    if not found or not any(found.groups()[:3]):
        return None
    hours, minutes, amount, unit = found.groups()
    seconds = int(hours or 0) * 3600 + int(minutes or 0) * 60
    if amount:
        seconds += float(amount) / (1000 if unit == "ms" else 1)
    return float(seconds)


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after", "")
    try:
        return float(header)
    except ValueError:
        return _seconds_in(_message_of(response))


def _error_for(endpoint: Endpoint, response: httpx.Response) -> AiError:
    status, message = response.status_code, _message_of(response)
    detail = f"{status}: {message}"
    label = endpoint.label
    if status in (401, 403):
        return AiError(f"La chiave di {label} non è valida o non ha i permessi. Avvisa chi gestisce il bot.", detail)
    if status == 404:
        return AiError(f"Il modello scelto per {label} non esiste. Avvisa chi gestisce il bot.", detail)
    if status in (400, 422):
        if re.search(r"response_format|json_object|json mode", message, re.I):
            return _FormatRejected(f"{label} non accetta il formato di risposta richiesto.", detail)
        if re.search(r"image|vision|multimodal|modalit", message, re.I):
            return AiError(
                f"Il modello scelto per {label} non sa leggere le immagini: chi gestisce il bot deve sceglierne uno con visione.",
                detail,
            )
    if status == 413:
        if re.search(r"token|tpm|too large for model", message, re.I):
            return AiError(_too_large_message(endpoint), detail, quota=True)
        return AiError("Il file è troppo grande per questo modello. Prova con una foto più leggera.", detail)
    if status >= 500:
        return AiError(f"{label} in questo momento non risponde. Riprova tra poco.", detail)
    return AiError(f"{label} ha rifiutato la richiesta. Riprova con un'altra foto.", detail)


def _too_large_message(endpoint: Endpoint) -> str:
    return (
        f"La richiesta è troppo grande per il limite di token al minuto del piano di {endpoint.label}. "
        "Chi gestisce il bot può ridurre i dati inviati (Modelli IA) o passare a un piano superiore."
    )


def _exceeds_limit(message: str) -> bool:
    """«Limit 8000, Requested 9500»: la richiesta da sola supera il limite, aspettare non serve a niente."""
    found = re.search(r"Limit (\d+),?\s*(?:Used \d+,?\s*)?Requested (\d+)", message)
    return bool(found) and int(found.group(2)) > int(found.group(1))


def _quota_message(endpoint: Endpoint, wait: float | None) -> str:
    when = f" Riprova tra circa {max(1, round(wait / 60))} minuti." if wait and wait > MAX_WAIT_SECONDS else " Riprova tra poco."
    return (
        f"{endpoint.label} ha raggiunto il limite di richieste.{when} Se succede spesso, chi gestisce il bot "
        "può cambiare modello o piano."
    )


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _normalize(obj: dict) -> dict:
    """Modelli diversi omettono campi o usano numeri al posto di testo: si riportano allo schema atteso."""
    appointment = obj.get("appointment")
    results = obj.get("lab_results")
    return {
        "kind": _text(obj.get("kind")).lower(),
        "patient_name": _text(obj.get("patient_name")),
        "document_date": _text(obj.get("document_date")),
        "summary": _text(obj.get("summary")),
        "details": "\n".join(map(_text, obj["details"])) if isinstance(obj.get("details"), list) else _text(obj.get("details")),
        "appointment": (
            {k: _text(appointment.get(k)) for k in ("title", "date", "time", "place", "notes")}
            if isinstance(appointment, dict)
            else None
        ),
        "lab_results": [
            {k: _text(item.get(k)) for k in ("name", "value", "unit", "reference", "flag")}
            for item in (results if isinstance(results, list) else [])
            if isinstance(item, dict)
        ],
    }


def parse_extraction(text: str, endpoint: Endpoint) -> Extraction:
    start, end = text.find("{"), text.rfind("}")
    try:
        if start < 0 or end < start:
            raise ValueError("nessun oggetto JSON nella risposta")
        return Extraction.model_validate(_normalize(json.loads(text[start : end + 1])))
    except (ValueError, ValidationError) as exc:
        raise AiError(
            "Non sono riuscito a interpretare il documento con questo modello. Riprova con una foto più nitida "
            "o chiedi a chi gestisce il bot di scegliere un altro modello.",
            f"{endpoint.label}: risposta non valida ({exc}): {text[:200]!r}",
        ) from exc


class OpenAICompat:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._own: httpx.AsyncClient | None = None
        self._slots = asyncio.Semaphore(MAX_PARALLEL_CALLS)
        self._sleep: Callable = asyncio.sleep  # sostituibile nei test

    def _client(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        if self._own is None:
            self._own = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self._own

    async def _send(
        self, endpoint: Endpoint, method: str, path: str, timeout: float | None = None, **kwargs
    ) -> httpx.Response:
        url = endpoint.base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}  # un server locale può non avere chiave
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with self._slots:
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    response = await self._client().request(method, url, headers=headers, **kwargs)
            except (httpx.HTTPError, OSError) as exc:
                raise AiError(
                    f"Non riesco a collegarmi a {endpoint.label}: controlla la connessione e riprova.", repr(exc)
                ) from exc
            if response.status_code == 429:
                if _exceeds_limit(_message_of(response)):
                    raise AiError(_too_large_message(endpoint), f"429: {_message_of(response)}", quota=True)
                wait = _retry_after(response)
                if attempt < MAX_RETRIES and wait is not None and wait <= MAX_WAIT_SECONDS:
                    await self._sleep(wait + 1)  # limite al minuto: basta aspettare
                    continue
                raise AiError(_quota_message(endpoint, wait), f"429: {_message_of(response)}", quota=True)
            if response.status_code >= 400:
                raise _error_for(endpoint, response)
            return response
        raise AssertionError("il ciclo termina sempre con un ritorno o un errore")  # pragma: no cover

    async def list_models(self, endpoint: Endpoint) -> list[str]:
        try:
            # L'elenco è una richiesta leggera: se il servizio non risponde in poco tempo, meglio dirlo che far aspettare.
            body = (await self._send(endpoint, "GET", "/models", timeout=LIST_TIMEOUT)).json()
        except AiError as exc:
            if "non esiste" in exc.user_message:  # 404: il servizio non pubblica l'elenco
                raise AiError(
                    f"{endpoint.label} non pubblica l'elenco dei modelli: scrivi il nome del modello a mano.", str(exc)
                ) from exc
            raise
        items = body.get("data") or body.get("models") or [] if isinstance(body, dict) else []
        return sorted({str(item.get("id") or item.get("name")) for item in items if isinstance(item, dict) and (item.get("id") or item.get("name"))})

    async def _chat(
        self, endpoint: Endpoint, messages: list[dict], *, temperature: float, json_mode: bool = False, max_tokens: int | None = None
    ) -> str:
        payload: dict = {"model": endpoint.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            body = (await self._send(endpoint, "POST", "/chat/completions", json=payload)).json()
        except _FormatRejected:
            payload.pop("response_format", None)  # alcuni servizi non supportano la modalità JSON: il prompt basta
            body = (await self._send(endpoint, "POST", "/chat/completions", json=payload)).json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AiError(f"{endpoint.label} ha risposto in un formato inatteso.", str(body)[:300]) from exc
        text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()  # modelli che «ragionano» in vista
        if not text:
            raise AiError(f"{endpoint.label} non ha dato nessuna risposta. Riprova.")
        return text

    async def analyze_document(self, endpoint: Endpoint, data: bytes, mime: str) -> Extraction:
        today = clock.now().strftime("%Y-%m-%d")
        if mime == "application/pdf":
            text = extract_pdf_text(data)
            if not text:
                raise AiError(
                    f"Questo PDF è una scansione e {endpoint.label} non riesce a leggerlo: manda una foto del documento."
                )
            user: str | list = f"Oggi è il {today}. Testo del documento:\n\n{text}"
        elif mime in IMAGE_MIMES:
            image = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            user = [
                {"type": "text", "text": f"Oggi è il {today}. Analizza il documento."},
                {"type": "image_url", "image_url": {"url": image}},
            ]
        else:
            raise AiError(f"{endpoint.label} non legge questo formato di file: manda una foto in formato JPEG o PNG.")
        messages = [{"role": "system", "content": ANALYZE_INSTRUCTIONS + JSON_RULES}, {"role": "user", "content": user}]
        return parse_extraction(await self._chat(endpoint, messages, temperature=0, json_mode=True), endpoint)

    async def answer(self, endpoint: Endpoint, context: str, sender_name: str, question: str) -> str:
        intro = f"Oggi è il {clock.now().strftime('%Y-%m-%d')}. Scrive {sender_name}.\n\nDATI DELLA FAMIGLIA:\n{context}\n\n"
        messages = [
            {"role": "system", "content": CONSULT_INSTRUCTIONS},
            {"role": "user", "content": intro + f"Domanda: {question}"},
        ]
        return await self._chat(endpoint, messages, temperature=0.3)

    async def transcribe(self, endpoint: Endpoint, model: str, audio: bytes, mime: str) -> str:
        response = await self._send(
            endpoint,
            "POST",
            "/audio/transcriptions",
            files={"file": ("voce.ogg", audio, mime or "audio/ogg")},
            data={"model": model, "language": "it"},
        )
        text = _text(response.json().get("text") if isinstance(response.json(), dict) else "")
        if not text:
            raise AiError("Non sono riuscito a capire il messaggio vocale. Riprova o scrivimi la domanda.")
        return text

    async def probe(self, endpoint: Endpoint, role: str) -> tuple[bool, str]:
        """Prova reale e minima: il modello risponde? E, per i documenti, accetta le immagini?"""
        try:
            if role == "docs":
                image = f"data:image/png;base64,{base64.b64encode(_tiny_png()).decode()}"
                content: str | list = [
                    {"type": "text", "text": "Rispondi solo con la parola: ok"},
                    {"type": "image_url", "image_url": {"url": image}},
                ]
            else:
                content = "Rispondi solo con la parola: ok"
            await self._chat(endpoint, [{"role": "user", "content": content}], temperature=0, max_tokens=16)
        except AiError as exc:
            return False, exc.user_message if str(exc) == exc.user_message else f"{exc.user_message} ({exc})"
        return True, "funziona"
