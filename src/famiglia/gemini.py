"""Gemini: lettura dei documenti (visione) con risposta JSON strutturata.

È Gemini a decidere di che documento si tratta: l'utente non scrive mai un prompt.
"""

from __future__ import annotations

import logging
from typing import Literal, Protocol

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

from . import clock
from .settings import Settings

log = logging.getLogger(__name__)

DocumentKind = Literal["appuntamento", "referto", "ricetta", "altro", "illeggibile"]


class GeminiError(Exception):
    """Errore di Gemini con un messaggio già pronto da mostrare all'utente."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message


class LabResult(BaseModel):
    name: str = Field(description="Nome dell'analisi o del parametro, ad esempio Glicemia")
    value: str = Field(description="Valore esattamente come scritto sul documento, ad esempio 95 oppure 5,4")
    unit: str = Field(description="Unità di misura, ad esempio mg/dL; vuoto se assente")
    reference: str = Field(description="Intervallo di riferimento come scritto, ad esempio 70-100; vuoto se assente")
    flag: str = Field(
        description='"basso" o "alto" solo se il valore è fuori dall\'intervallo di riferimento '
        "(o segnalato dal documento con asterisco, H o L); altrimenti stringa vuota"
    )


class AppointmentInfo(BaseModel):
    title: str = Field(description="Tipo di visita o esame, ad esempio Visita cardiologica")
    date: str = Field(description="Data della visita in formato AAAA-MM-GG; vuoto se non indicata")
    time: str = Field(description="Ora in formato HH:MM a 24 ore; vuoto se non indicata")
    place: str = Field(description="Struttura, reparto o indirizzo; vuoto se assente")
    notes: str = Field(description="Preparazione o indicazioni scritte (digiuno, documenti da portare); vuoto se assenti")


class Extraction(BaseModel):
    kind: DocumentKind = Field(description="Tipo di documento")
    patient_name: str = Field(description="Nome e cognome del paziente come scritto sul documento; vuoto se assente")
    document_date: str = Field(description="Data del documento o del referto in formato AAAA-MM-GG; vuoto se assente")
    summary: str = Field(description="Una o due frasi semplici in italiano su cosa contiene il documento")
    appointment: AppointmentInfo | None = Field(description="Solo se kind è appuntamento, altrimenti null")
    lab_results: list[LabResult] = Field(description="Solo se kind è referto con valori misurati, altrimenti lista vuota")


ANALYZE_INSTRUCTIONS = """\
Sei l'assistente di una famiglia italiana e leggi documenti medici fotografati o in PDF.
Guarda il documento e decidi tu di che tipo è:
- appuntamento: prenotazione, conferma o promemoria di una visita o di un esame ancora da fare
- referto: esito di analisi, esami o visite già svolti
- ricetta: ricetta medica o prescrizione
- altro: qualsiasi altro documento sanitario
- illeggibile: non riesci a leggerlo, oppure non è un documento medico

Regole:
- Riporta solo ciò che è scritto nel documento. Non inventare, non correggere, non interpretare i valori.
- Se un dato manca lascia il campo vuoto (o null / lista vuota).
- Date sempre nel formato AAAA-MM-GG, ore nel formato HH:MM. Le date italiane sono giorno/mese/anno.
- Per i referti elenca ogni parametro misurato come voce separata.
"""


class DocumentReader(Protocol):
    async def analyze_document(self, data: bytes, mime: str) -> Extraction: ...


class Gemini:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._client: genai.Client | None = None
        self._client_key = ""

    def _client_for_current_key(self) -> genai.Client:
        key = self._settings.get("gemini_api_key")
        if not key:
            raise GeminiError("Il bot non è ancora configurato (manca la chiave Gemini). Avvisa chi lo gestisce.")
        if self._client is None or key != self._client_key:
            options = types.HttpOptions(httpx_async_client=self._http_client) if self._http_client else None
            self._client = genai.Client(api_key=key, http_options=options)
            self._client_key = key
        return self._client

    async def _generate(self, contents: list, config: types.GenerateContentConfig) -> str:
        client = self._client_for_current_key()
        try:
            response = await client.aio.models.generate_content(
                model=self._settings.get("gemini_model"), contents=contents, config=config
            )
        except errors.APIError as exc:
            raise GeminiError(_friendly(exc), f"{exc.code}: {exc.message}") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise GeminiError("Non riesco a collegarmi a Gemini: controlla la connessione e riprova.", repr(exc)) from exc
        text = response.text
        if not text:
            raise GeminiError("Gemini non ha dato nessuna risposta per questo documento. Riprova con un'altra foto.")
        return text

    async def analyze_document(self, data: bytes, mime: str) -> Extraction:
        config = types.GenerateContentConfig(
            system_instruction=ANALYZE_INSTRUCTIONS,
            response_mime_type="application/json",
            response_schema=Extraction,
            temperature=0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        today = clock.now().strftime("%Y-%m-%d")
        text = await self._generate(
            [types.Part.from_bytes(data=data, mime_type=mime), f"Oggi è il {today}. Analizza il documento."], config
        )
        try:
            return Extraction.model_validate_json(text)
        except ValueError as exc:
            log.warning("Risposta Gemini non valida: %s", exc)
            raise GeminiError("Non sono riuscito a interpretare il documento. Riprova con una foto più nitida.") from exc


def _friendly(exc: errors.APIError) -> str:
    code = exc.code or 0
    if code == 429:
        return "Gemini ha ricevuto troppe richieste. Riprova tra un minuto."
    if code in (401, 403) or (code == 400 and "api key" in (exc.message or "").lower()):
        return "La chiave Gemini non è valida o non ha i permessi. Avvisa chi gestisce il bot."
    if code == 404:
        return "Il modello Gemini scelto non esiste. Avvisa chi gestisce il bot."
    if code >= 500:
        return "Gemini in questo momento non risponde. Riprova tra poco."
    return "Gemini ha rifiutato la richiesta. Riprova con un'altra foto."
