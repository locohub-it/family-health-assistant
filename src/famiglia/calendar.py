"""Google Calendar tramite Composio: un collegamento Google per ogni familiare.

L'SDK di Composio è sincrono: ogni chiamata gira in un thread per non bloccare il bot.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from . import clock
from .settings import Settings
from .users import User

log = logging.getLogger(__name__)

TOOLKIT = "googlecalendar"
CREATE_EVENT = "GOOGLECALENDAR_CREATE_EVENT"
DELETE_EVENT = "GOOGLECALENDAR_DELETE_EVENT"
LIST_CALENDARS = "GOOGLECALENDAR_LIST_CALENDARS"

DEFAULT_MINUTES = 60
UNKNOWN_TIME = "09:00"  # se sul documento manca l'ora: l'evento è segnato «orario da confermare»


WHERE_IS_THE_KEY = (
    "si trova su dashboard.composio.dev: scegli «Platform» (non «For You»), apri il tuo progetto, poi "
    "Settings → API Keys, e copia o crea la chiave che inizia con ak_"
)
BAD_KEY_MESSAGE = f"La chiave Composio non è valida o non ha i permessi. Serve la Project API key: {WHERE_IS_THE_KEY}."
CONSUMER_KEY_MESSAGE = (
    "La chiave Composio inserita inizia con ck_: è una chiave «consumer», quella della pagina Sessions / AI Client, "
    f"che serve per MCP e non funziona con il calendario. Serve la Project API key: {WHERE_IS_THE_KEY}."
)


class CalendarError(Exception):
    """Errore con un messaggio già pronto da mostrare all'utente."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message


class PastDateError(CalendarError):
    """La visita è già passata: inutile provarci su un altro calendario."""


@dataclass(frozen=True)
class CalendarInfo:
    id: str
    name: str
    primary: bool


def _default_factory(api_key: str) -> Any:
    from composio import Composio

    return Composio(api_key=api_key)


def _payload(data: dict) -> dict:
    """Composio mette a volte la risposta di Google sotto `response_data`."""
    inner = data.get("response_data")
    return inner if isinstance(inner, dict) else data


class Calendar:
    def __init__(self, settings: Settings, factory: Callable[[str], Any] | None = None) -> None:
        self._settings = settings
        self._factory = factory or _default_factory
        self._client: Any = None
        self._client_key = ""

    @property
    def enabled(self) -> bool:
        return self._settings.is_set("composio_api_key")

    def _composio(self) -> Any:
        key = self._settings.get("composio_api_key")
        if not key:
            raise CalendarError("Google Calendar non è configurato: manca la chiave Composio.")
        if key.startswith("ck_"):  # salvata prima che il pannello la rifiutasse: inutile chiedere a Composio
            raise CalendarError(CONSUMER_KEY_MESSAGE)
        if self._client is None or key != self._client_key:
            self._client, self._client_key = self._factory(key), key
        return self._client

    async def _run(self, fn: Callable[[], Any]) -> Any:
        try:
            return await asyncio.to_thread(fn)
        except CalendarError:
            raise
        except Exception as exc:  # noqa: BLE001 - qualunque errore dell'SDK diventa un messaggio gentile
            if getattr(exc, "status_code", None) in (401, 403) or "invalid api key" in str(exc).lower():
                raise CalendarError(BAD_KEY_MESSAGE, f"{type(exc).__name__}: {exc}") from exc
            raise CalendarError(
                "Non riesco a usare Google Calendar in questo momento.", f"{type(exc).__name__}: {exc}"
            ) from exc

    def _execute(self, slug: str, user: User, arguments: dict) -> dict:
        response = self._composio().tools.execute(
            slug,
            arguments,
            user_id=user.composio_user_id,
            # Le versioni dei toolkit cambiano di frequente: gli argomenti usati qui sono quelli documentati.
            dangerously_skip_version_check=True,
        )
        if not response.get("successful"):
            raise CalendarError("Google Calendar ha rifiutato la richiesta.", str(response.get("error")))
        return response.get("data") or {}

    # --- Collegamento dell'account Google -----------------------------------------

    async def connect_link(self, user: User) -> str:
        """Indirizzo su cui la persona autorizza il proprio Google Calendar."""

        def call() -> str:
            request = self._composio().toolkits.authorize(user_id=user.composio_user_id, toolkit=TOOLKIT)
            if not request.redirect_url:
                raise CalendarError("Composio non ha dato il link di collegamento.")
            return request.redirect_url

        return await self._run(call)

    async def connected_user_ids(self, users: list[User]) -> set[str]:
        """Quali utenti hanno un Google Calendar collegato e attivo."""
        if not users:
            return set()

        def call() -> set[str]:
            found = self._composio().connected_accounts.list(
                user_ids=[u.composio_user_id for u in users], toolkit_slugs=[TOOLKIT]
            )
            # Lo stato si filtra qui: un collegamento scaduto o revocato non conta come collegato.
            return {item.user_id for item in (found.items or []) if str(item.status).upper() == "ACTIVE"}

        return await self._run(call)

    async def is_connected(self, user: User) -> bool:
        return user.composio_user_id in await self.connected_user_ids([user])

    # --- Calendari ----------------------------------------------------------------

    async def list_calendars(self, user: User) -> list[CalendarInfo]:
        data = _payload(await self._run(lambda: self._execute(LIST_CALENDARS, user, {"min_access_role": "writer"})))
        return [
            CalendarInfo(item["id"], item.get("summary") or item["id"], bool(item.get("primary")))
            for item in data.get("items", [])
            if item.get("id")
        ]

    async def create_event(
        self, user: User, starts_at: str, title: str, place: str = "", notes: str = "", now: datetime | None = None
    ) -> str:
        """Crea la visita sul calendario dell'utente e restituisce l'id dell'evento.

        `starts_at` è `AAAA-MM-GGTHH:MM` oppure solo `AAAA-MM-GG` se l'ora non si legge.
        """
        day, _, hour = starts_at.partition("T")
        summary = title if hour else f"{title} (orario da confermare)"
        start = datetime.fromisoformat(f"{day}T{hour or UNKNOWN_TIME}").replace(tzinfo=clock.TIMEZONE)
        current = now or clock.now()
        # Senza ora conta il giorno: una visita di oggi con l'ora da confermare non è «passata».
        if (start < current) if hour else (start.date() < current.date()):
            raise PastDateError("La data è già passata: non l'ho messa sul calendario.")
        if not await self.is_connected(user):
            raise CalendarError("Il Google Calendar di questa persona non è ancora collegato.")

        arguments = {
            "calendar_id": user.calendar_id,
            "summary": summary,
            "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": clock.TIMEZONE.key,
            "event_duration_hour": DEFAULT_MINUTES // 60,
            "event_duration_minutes": DEFAULT_MINUTES % 60,
            **({"location": place} if place else {}),
            **({"description": notes} if notes else {}),
            "create_meeting_room": False,  # di default Composio aggiunge un link Google Meet: qui non ha senso
            "send_updates": "none",
        }
        data = _payload(await self._run(lambda: self._execute(CREATE_EVENT, user, arguments)))
        event_id = data.get("id")
        if not event_id:
            raise CalendarError("Google Calendar non ha confermato la creazione della visita.", str(data))
        return str(event_id)

    async def delete_event(self, user: User, event_id: str) -> None:
        await self._run(
            lambda: self._execute(DELETE_EVENT, user, {"event_id": event_id, "calendar_id": user.calendar_id, "send_updates": "none"})
        )
