"""Cosa fa il bot quando riceve un documento: legge, decide, salva e risponde.

Nessun codice Telegram qui: ricevo i byte del file e restituisco il testo da mandare.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from . import clock
from .calendar import UNKNOWN_TIME, Calendar, CalendarError
from .gemini import DocumentReader, Extraction
from .records import Records
from .settings import Settings
from .storage import Storage, StorageError
from .users import User, UserStore

SUPPORTED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}
MAX_BYTES = 15 * 1024 * 1024

DEFAULT_DOCUMENTS_DIR = "Documenti"  # dentro la radice: si usa se non ne è stata scelta un'altra
FALLBACK_PREFIX = "@volume/"  # ultima spiaggia: la cartella nel volume dei dati, sempre scrivibile
FOLDERS = {"appuntamento": "Appuntamenti", "referto": "Referti", "ricetta": "Ricette", "altro": "Altro"}
TITLES = re.compile(r"\b(sig\.ra|dott\.ssa|signora|signor|sigg|sig|dott|dr|prof|paziente)\b\.?", re.IGNORECASE)


@dataclass
class Outcome:
    text: str
    document_id: int | None = None  # se valorizzato il bot mostra il pulsante «Annulla»


def _tokens(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    return {t for t in re.split(r"[^a-z]+", TITLES.sub(" ", plain)) if len(t) > 1}


def match_patient(patient_name: str, users: list[User]) -> list[User]:
    """Familiari il cui nome compare nel nome scritto sul documento (i più somiglianti)."""
    wanted = _tokens(patient_name)
    if not wanted:
        return []
    scored = [(len(wanted & _tokens(u.name)), u) for u in users]
    scored = [(s, u) for s, u in scored if s and (_tokens(u.name) <= wanted or wanted <= _tokens(u.name))]
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [u for s, u in scored if s == best]


def _valid_date(value: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return ""


def _valid_time(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return ""


def format_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return iso


class DocumentService:
    def __init__(
        self,
        settings: Settings,
        users: UserStore,
        storage: Storage,
        records: Records,
        gemini: DocumentReader,
        log: Callable[[str, str], None],
        calendar: Calendar | None = None,
        fallback: Storage | None = None,
    ) -> None:
        self._fallback = fallback
        self._settings = settings
        self._users = users
        self._storage = storage
        self._records = records
        self._gemini = gemini
        self._log = log
        self._calendar = calendar

    async def process(self, sender: User, data: bytes, mime: str) -> Outcome:
        if mime not in SUPPORTED_MIME:
            return Outcome("Questo tipo di file non lo so leggere. Mandami una foto o un PDF del documento.")
        if len(data) > MAX_BYTES:
            return Outcome("Il file è troppo grande. Prova con una foto più leggera.")
        extraction = await self._gemini.analyze_document(data, mime)
        if extraction.kind == "illeggibile":
            self._log("documento", f"{sender.name}: illeggibile")
            return Outcome(
                "Non riesco a leggere bene il documento. Puoi rifare la foto con più luce, "
                "tenendo il foglio dritto e intero?"
            )

        patient, note = self._resolve_patient(extraction.patient_name, sender)
        doc_date = _valid_date(extraction.document_date)
        kind = extraction.kind
        filename = f"{doc_date or clock.now().strftime('%Y-%m-%d')}_{kind}{SUPPORTED_MIME[mime]}"
        saved_path = await self._save_file([patient.name, FOLDERS[kind]], filename, data)
        document_id = self._records.add_document(
            patient.id, sender.telegram_id, kind, saved_path, doc_date, extraction.summary, extraction.model_dump_json()
        )
        text = await self._save_details(document_id, patient, extraction, doc_date)
        self._log("documento", f"{patient.name}: {kind}")
        return Outcome(note + text, document_id)

    async def _save_file(self, parts: list[str], filename: str, data: bytes) -> str:
        """Salva il file: cartella scelta, poi quella predefinita, poi il volume dei dati.

        Un percorso sbagliato non deve arrivare in chat: si prova la successiva e l'errore va nel registro.
        """
        chosen = self._settings.get("documents_dir") or DEFAULT_DOCUMENTS_DIR
        attempts: list[tuple[Storage, str, str]] = [(self._storage, chosen, "")]
        if chosen != DEFAULT_DOCUMENTS_DIR:
            attempts.append((self._storage, DEFAULT_DOCUMENTS_DIR, ""))
        if self._fallback is not None:
            attempts.append((self._fallback, ".", FALLBACK_PREFIX))
        failures: list[str] = []
        for storage, folder, prefix in attempts:
            try:
                saved = await asyncio.to_thread(storage.save, folder, parts, filename, data)
            except StorageError as exc:
                failures.append(f"«{folder if not prefix else 'volume dei dati'}»: {exc}")
                continue
            if failures:
                self._log("errore", f"Cartella non utilizzabile ({'; '.join(failures)}). Salvato in {prefix + saved}")
            return prefix + saved
        raise StorageError("; ".join(failures))

    def _delete_file(self, path: str) -> None:
        if path.startswith(FALLBACK_PREFIX) and self._fallback is not None:
            self._fallback.delete(path.removeprefix(FALLBACK_PREFIX))
        else:
            self._storage.delete(path)

    def _resolve_patient(self, patient_name: str, sender: User) -> tuple[User, str]:
        """Di chi è il documento: se porta il nome di un altro familiare, è suo."""
        candidates = match_patient(patient_name, self._users.all())
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1 and sender in candidates:
            return sender, ""
        if len(candidates) > 1:
            names = " o ".join(u.name for u in candidates)
            return sender, f"Sul documento c'è un nome che potrebbe essere {names}: l'ho messo a nome tuo.\n\n"
        if patient_name.strip():
            return sender, f"Il documento è intestato a «{patient_name.strip()}», che non è tra i familiari: l'ho messo a nome tuo.\n\n"
        return sender, ""

    async def _save_details(self, document_id: int, patient: User, extraction: Extraction, doc_date: str) -> str:
        who = patient.name
        if extraction.kind == "referto":
            return self._save_lab_report(document_id, patient, extraction, doc_date)
        if extraction.kind == "appuntamento":
            return await self._save_appointment(document_id, patient, extraction)
        label = "la ricetta" if extraction.kind == "ricetta" else "il documento"
        return f"Ho salvato {label} di {who}.\n{extraction.summary}"

    def _save_lab_report(self, document_id: int, patient: User, extraction: Extraction, doc_date: str) -> str:
        results = [r for r in extraction.lab_results if r.name.strip() and r.value.strip()]
        for r in results:
            r.flag = r.flag.strip().lower() if r.flag.strip().lower() in {"alto", "basso"} else ""
        self._records.add_lab_results(document_id, patient.id, doc_date, results)
        when = f" del {format_date(doc_date)}" if doc_date else ""
        lines = [f"Ho salvato il referto di {patient.name}{when}: {len(results)} valori."]
        if extraction.summary:
            lines.append(extraction.summary)
        flagged = [r for r in results if r.flag]
        if flagged:
            lines.append("\nFuori dai valori di riferimento:")
            lines += [f"• {r.name}: {r.value} {r.unit} ({r.flag})".replace("  ", " ") for r in flagged]
        return "\n".join(lines)

    async def _save_appointment(self, document_id: int, patient: User, extraction: Extraction) -> str:
        info = extraction.appointment
        day = _valid_date(info.date) if info else ""
        if not info or not day:
            return (
                f"Ho salvato il documento di {patient.name}, ma non trovo la data della visita: "
                "non l'ho messa sul calendario. Riprova con una foto in cui si legge bene la data."
            )
        hour = _valid_time(info.time)
        starts_at = f"{day}T{hour}" if hour else day
        title = info.title.strip() or "Visita medica"
        place, notes = info.place.strip(), info.notes.strip()
        appointment_id = self._records.add_appointment(document_id, patient.id, starts_at, title, place, notes)
        when = f"il {format_date(day)}" + (f" alle {hour}" if hour else "")
        lines = [f"Ho salvato la visita «{title}» di {patient.name} per {when}."]
        if info.place.strip():
            lines.append(f"Dove: {info.place.strip()}")
        if info.notes.strip():
            lines.append(f"Da ricordare: {info.notes.strip()}")
        lines.append(await self._add_to_calendar(patient, appointment_id, starts_at, title, place, notes))
        return "\n".join(line for line in lines if line)

    async def _add_to_calendar(
        self, patient: User, appointment_id: int, starts_at: str, title: str, place: str, notes: str
    ) -> str:
        """Il calendario è un di più: se non va, la visita resta salvata e l'utente lo sa."""
        if self._calendar is None or not self._calendar.enabled:
            return ""
        try:
            event_id = await self._calendar.create_event(patient, starts_at, title, place, notes)
        except CalendarError as exc:
            self._log("errore", f"Calendario di {patient.name}: {exc}")
            return f"⚠️ {exc.user_message}"
        self._records.set_event_id(appointment_id, event_id)
        line = f"📅 L'ho aggiunta al calendario di {patient.name}."
        return line if "T" in starts_at else f"{line} L'ora non c'era: ho messo le {UNKNOWN_TIME}, da confermare."

    async def undo(self, sender: User, document_id: int) -> str:
        document = self._records.get_document(document_id)
        if document is None:
            return "Era già stato annullato."
        if document["sender_telegram_id"] != sender.telegram_id:
            return "Puoi annullare solo quello che hai inviato tu."
        warning = await self._remove_calendar_events(document)
        self._records.delete_document(document_id)
        try:
            await asyncio.to_thread(self._delete_file, document["file_path"])
        except StorageError:
            pass  # il file non si cancella ma i dati sì: meglio dell'opposto
        self._log("annullato", f"documento {document_id}")
        return "Annullato: ho tolto il documento." + warning

    async def _remove_calendar_events(self, document) -> str:
        events = [a["event_id"] for a in self._records.appointments_of_document(document["id"]) if a["event_id"]]
        patient = self._users.get(document["user_id"])
        if not events or patient is None or self._calendar is None:
            return ""
        try:
            for event_id in events:
                await self._calendar.delete_event(patient, event_id)
        except CalendarError as exc:
            self._log("errore", f"Calendario di {patient.name}: {exc}")
            return "\n⚠️ Non sono riuscito a togliere la visita dal calendario: va cancellata a mano."
        return "\nHo tolto anche la visita dal calendario."
