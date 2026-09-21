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
from .calendar import UNKNOWN_TIME, Calendar, CalendarError, PastDateError
from .gemini import AppointmentInfo, DocumentReader, Extraction
from .records import Records
from .settings import Settings
from .storage import Storage, StorageError
from .users import User, UserStore, coordinator_of

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


NOT_FOUND = "Questa visita non c'è più, oppure non puoi cambiarla."
MAX_PENDING = 6  # visite in sospeso ricavate da un solo documento


@dataclass
class Outcome:
    text: str
    document_id: int | None = None  # se valorizzato il bot mostra il pulsante «Annulla»
    appointment_id: int | None = None  # visita inserita con i pulsanti: da qui si può modificare o eliminare


def _tokens(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    return {t for t in re.split(r"[^a-z]+", TITLES.sub(" ", plain)) if len(t) > 1}


def match_patient(patient_name: str, users: list[User]) -> list[User]:
    """Familiari il cui nome compare nel nome scritto sul documento (i più somiglianti)."""
    wanted = _tokens(patient_name)
    if not wanted:
        return []
    # Si confronta con il nome reale (nome + cognome) se c'è, altrimenti con quello breve.
    scored = [(len(wanted & _tokens(u.full_name)), u) for u in users]
    scored = [(s, u) for s, u in scored if s and (_tokens(u.full_name) <= wanted or wanted <= _tokens(u.full_name))]
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
            patient.id,
            sender.telegram_id,
            kind,
            saved_path,
            doc_date,
            extraction.summary,
            extraction.model_dump_json(),
            extraction.details.strip(),
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
            text = self._save_lab_report(document_id, patient, extraction, doc_date)
        elif extraction.kind == "appuntamento":
            text = await self._save_appointment(document_id, patient, extraction)
        else:
            label = "la ricetta" if extraction.kind == "ricetta" else "il documento"
            text = f"Ho salvato {label} di {who}.\n{extraction.summary}"
        return text + self._save_pending(document_id, patient, extraction)

    @staticmethod
    def pending_names(extraction: Extraction) -> list[str]:
        """Visite da prenotare: quelle prescritte senza data, più una prenotazione letta senza data."""
        names = list(extraction.pending_visits)
        info = extraction.appointment
        if extraction.kind == "appuntamento" and not (info and _valid_date(info.date)):
            names.insert(0, info.title if info and info.title.strip() else "Visita medica")
        unique: list[str] = []
        for name in names:
            name = re.sub(r"\s+", " ", name).strip(" .")[:80]
            if name and name.casefold() not in {u.casefold() for u in unique}:
                unique.append(name)
        return unique[:MAX_PENDING]

    def _save_pending(self, document_id: int, patient: User, extraction: Extraction) -> str:
        """Una visita prescritta ma non ancora prenotata si salva «in sospeso»: la data si darà più avanti."""
        names = self.pending_names(extraction)
        for name in names:
            self._records.add_appointment(document_id, patient.id, "", name, "", "")
        if not names:
            return ""
        self._log("in sospeso", f"{patient.name}: {', '.join(names)}")
        shown = f"«{names[0]}»" if len(names) == 1 else "\n" + "\n".join(f"• {n}" for n in names)
        head = f"{shown} è tra le visite in sospeso" if len(names) == 1 else f"Queste visite sono tra quelle in sospeso:{shown}"
        return f"\n\n🕓 {head}: quando avrai la data della prenotazione tocca /in_sospeso e me la dici."

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
            return f"Ho salvato il documento di {patient.name}: non c'è ancora una data, quindi non l'ho messo sul calendario."
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
        self, patient: User, appointment_id: int, starts_at: str, title: str, place: str, notes: str, updated: bool = False
    ) -> str:
        """Il calendario è un di più: se non va, la visita resta salvata e l'utente lo sa."""
        if self._calendar is None or not self._calendar.enabled:
            return ""
        lines: list[str] = []
        try:
            event_id = await self._calendar.create_event(patient, starts_at, title, place, notes)
        except PastDateError as exc:
            return f"⚠️ {exc.user_message}"  # visita passata: non serve nemmeno al coordinatore
        except CalendarError as exc:
            self._log("errore", f"Calendario di {patient.name}: {exc}")
            lines.append(f"⚠️ {exc.user_message}")
        else:
            self._records.set_event_id(appointment_id, event_id)
            line = f"📅 Ho aggiornato il calendario di {patient.name}." if updated else f"📅 L'ho aggiunta al calendario di {patient.name}."
            lines.append(line if "T" in starts_at else f"{line} L'ora non c'era: ho messo le {UNKNOWN_TIME}, da confermare.")

        coordinator = coordinator_of(self._settings, self._users)
        if coordinator is not None and coordinator.id != patient.id:
            lines.append(await self._add_for_coordinator(coordinator, patient, appointment_id, starts_at, title, place, notes))
        return "\n".join(lines)

    async def _add_for_coordinator(
        self, coordinator: User, patient: User, appointment_id: int, starts_at: str, title: str, place: str, notes: str
    ) -> str:
        """Copia della visita sul calendario di chi coordina la famiglia, con il nome del paziente."""
        details = f"Paziente: {patient.full_name}" + (f"\n{notes}" if notes else "")
        try:
            event_id = await self._calendar.create_event(coordinator, starts_at, f"{title} – {patient.name}", place, details)
        except CalendarError as exc:
            self._log("errore", f"Calendario di {coordinator.name} (coordinatore): {exc}")
            return f"⚠️ Non sono riuscito ad aggiungerla al calendario di {coordinator.name}: {exc.user_message}"
        self._records.set_coordinator_event(appointment_id, coordinator.id, event_id)
        return f"📅 E anche a quello di {coordinator.name} (coordinatore)."

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

    async def _delete_events(self, appointment, patient: User | None) -> tuple[int, bool]:
        """Toglie una visita dal calendario del paziente e da quello del coordinatore. Restituisce (tolte, qualcuna non riuscita)."""
        if self._calendar is None:
            return 0, False
        targets: list[tuple[User | None, str]] = []
        if appointment["event_id"]:
            targets.append((patient, appointment["event_id"]))
        if appointment["coordinator_event_id"]:
            targets.append((self._users.get(appointment["coordinator_user_id"]), appointment["coordinator_event_id"]))
        removed, failed = 0, False
        for user, event_id in targets:
            if user is None:  # l'utente non c'è più: non si sa su quale calendario agire
                failed = True
                continue
            try:
                await self._calendar.delete_event(user, event_id)
                removed += 1
            except CalendarError as exc:
                self._log("errore", f"Calendario di {user.name}: {exc}")
                failed = True
        return removed, failed

    async def _remove_calendar_events(self, document) -> str:
        """Toglie le visite del documento dal calendario del paziente e da quello del coordinatore che le aveva ricevute."""
        patient = self._users.get(document["user_id"])
        removed, failed = 0, False
        for appointment in self._records.appointments_of_document(document["id"]):
            done, bad = await self._delete_events(appointment, patient)
            removed, failed = removed + done, failed or bad
        if failed:
            return "\n⚠️ Non sono riuscito a togliere la visita da un calendario: va cancellata a mano."
        if not removed:
            return ""
        return "\nHo tolto anche la visita dal calendario." if removed == 1 else "\nHo tolto anche la visita dai calendari."

    # --- Visite inserite, modificate o tolte con i pulsanti --------------------------------

    def manageable_ids(self, sender: User) -> set[int]:
        """Persone di cui `sender` può gestire le visite: sé stesso e i familiari per cui ha inviato documenti o visite."""
        return self._records.managed_patient_ids(sender.telegram_id) | {sender.id}

    def upcoming_appointments(self, sender: User, limit: int = 8):
        """Le prossime visite di chi scrive e dei familiari che gestisce."""
        return self._records.upcoming_appointments(self.manageable_ids(sender), clock.now().strftime("%Y-%m-%d"), limit)

    def pending_appointments(self, sender: User):
        """Le visite prescritte a chi scrive e ai familiari che gestisce, ancora senza data di prenotazione."""
        return self._records.pending_appointments(self.manageable_ids(sender))

    def own_appointment(self, sender: User, appointment_id: int):
        """(visita, paziente) se esiste e `sender` può gestirla, altrimenti (None, None)."""
        row = self._records.get_appointment(appointment_id)
        patient = self._users.get(row["user_id"]) if row else None
        if row is None or patient is None or patient.id not in self.manageable_ids(sender):
            return None, None
        return row, patient

    async def add_appointment(self, sender: User, patient: User, starts_at: str, title: str, place: str = "") -> Outcome:
        """Visita inserita a mano: si salva come quelle lette dai documenti (database, calendario, coordinatore), senza file."""
        day, _, hour = starts_at.partition("T")
        extraction = Extraction(
            kind="appuntamento",
            patient_name=patient.full_name,
            document_date=clock.now().strftime("%Y-%m-%d"),
            summary=f"Visita segnata con i pulsanti: {title}",
            details="",
            appointment=AppointmentInfo(title=title, date=day, time=hour, place=place, notes=""),
            lab_results=[],
        )
        document_id = self._records.add_document(
            patient.id, sender.telegram_id, "appuntamento", "", extraction.document_date, extraction.summary,
            extraction.model_dump_json(), "",
        )
        text = await self._save_appointment(document_id, patient, extraction)
        self._log("documento", f"{patient.name}: visita segnata con i pulsanti")
        saved = self._records.appointments_of_document(document_id)
        return Outcome(text, document_id, saved[0]["id"] if saved else None)

    async def edit_appointment(
        self, sender: User, appointment_id: int, *, starts_at: str | None = None, title: str | None = None, place: str | None = None
    ) -> str:
        """Cambia data, tipo o luogo. Sul calendario la visita si rifà: si toglie la vecchia e si mette la nuova."""
        row, patient = self.own_appointment(sender, appointment_id)
        if row is None:
            return NOT_FOUND
        new_start = row["starts_at"] if starts_at is None else starts_at
        new_title = row["title"] if title is None else title
        new_place = row["place"] if place is None else place
        _, failed = await self._delete_events(row, patient)
        self._records.clear_event_ids(appointment_id)
        self._records.update_appointment(appointment_id, new_start, new_title, new_place)
        # Una visita in sospeso non ha ancora un evento: darle la data è il primo inserimento, non un aggiornamento.
        lines = [await self._add_to_calendar(patient, appointment_id, new_start, new_title, new_place, row["notes"], updated=bool(row["starts_at"]))]
        if failed:
            lines.append("⚠️ Non sono riuscito a togliere la vecchia visita da un calendario: va cancellata a mano.")
        self._log("appuntamento", f"{patient.name}: visita modificata")
        return "\n".join(line for line in lines if line)

    async def delete_appointment(self, sender: User, appointment_id: int) -> str:
        """Toglie la visita dal database e dai calendari. Il documento da cui viene resta, tranne se era inserita a mano."""
        row, patient = self.own_appointment(sender, appointment_id)
        if row is None:
            return NOT_FOUND
        removed, failed = await self._delete_events(row, patient)
        document_id = row["document_id"]
        self._records.delete_appointment(appointment_id)
        document = self._records.get_document(document_id)
        if document is not None and not document["file_path"] and not self._records.appointments_of_document(document_id):
            self._records.delete_document(document_id)  # una visita senza file non ha altro da conservare
        self._log("appuntamento", f"{patient.name}: visita eliminata")
        text = f"Ho eliminato la visita «{row['title']}» di {patient.name}."
        if failed:
            return text + "\n⚠️ Non sono riuscito a toglierla da un calendario: va cancellata a mano."
        return text + ("\nÈ sparita anche dal calendario." if removed == 1 else "\nÈ sparita anche dai calendari." if removed else "")
