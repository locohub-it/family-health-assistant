"""Modalità consultazione: una domanda (scritta o a voce) viene risposta da Gemini sui dati salvati.

Niente domande tradotte in SQL: i dati recenti delle persone interessate si passano a Gemini come
testo, così non c'è nessuna query generata da un modello da fidarsi.
"""

from __future__ import annotations

from typing import Callable, Protocol

from . import clock
from .documents import format_date
from .records import Records
from .users import User, UserStore

MAX_REPORTS = 15
MAX_APPOINTMENTS = 20
MAX_OTHER_DOCUMENTS = 10
TELEGRAM_LIMIT = 4000  # il massimo di Telegram è 4096 caratteri


class Answerer(Protocol):
    async def answer(
        self, context: str, sender_name: str, question: str | None = None, audio: bytes | None = None, audio_mime: str = ""
    ) -> str: ...


def _cut_point(text: str, limit: int) -> int:
    """Dove tagliare: a fine paragrafo, altrimenti a capo, altrimenti su uno spazio, altrimenti di netto."""
    for separator in ("\n\n", "\n", " "):
        cut = text.rfind(separator, 0, limit)
        if cut > limit // 2:  # un taglio troppo presto darebbe messaggi quasi vuoti
            return cut
    return limit


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Spezza una risposta lunga in messaggi accettati da Telegram."""
    text, parts = text.strip(), []
    while len(text) > limit:
        cut = _cut_point(text, limit)
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + [text] if text else parts


class Consultant:
    def __init__(self, users: UserStore, records: Records, answerer: Answerer, log: Callable[[str, str], None]) -> None:
        self._users = users
        self._records = records
        self._answerer = answerer
        self._log = log

    def accessible_patients(self, sender: User) -> list[User]:
        """Chi scrive e i familiari di cui ha inviato documenti: nessun altro."""
        allowed = self._records.managed_patient_ids(sender.telegram_id) | {sender.id}
        return [u for u in self._users.all() if u.id in allowed]

    def build_context(self, patients: list[User]) -> str:
        return "\n\n".join(self._patient_section(p) for p in patients)

    def _patient_section(self, patient: User) -> str:
        title = f"## {patient.name}" + (f" ({patient.role})" if patient.role else "")
        lines = [title]

        reports = self._records.documents(patient.id, ("referto",), MAX_REPORTS)
        lines.append("Referti, dal più recente:" if reports else "Nessun referto salvato.")
        for doc in reports:
            when = format_date(doc["doc_date"]) if doc["doc_date"] else "data non indicata"
            values = []
            for r in self._records.lab_results_of(doc["id"]):
                text = f"{r['name']} {r['value']} {r['unit']}".strip()
                if r["reference"]:
                    text += f" (rif. {r['reference']})"
                if r["flag"]:
                    text += f" [{r['flag'].upper()}]"
                values.append(text)
            lines.append(f"- {when}: " + ("; ".join(values) if values else doc["summary"] or "nessun valore"))

        appointments = self._records.appointments(patient.id, MAX_APPOINTMENTS)
        lines.append("Visite ed esami prenotati:" if appointments else "Nessuna visita salvata.")
        for a in appointments:
            day, _, hour = a["starts_at"].partition("T")
            place = f" – {a['place']}" if a["place"] else ""
            lines.append(f"- {format_date(day)}{' ' + hour if hour else ''}: {a['title']}{place}")

        others = self._records.documents(patient.id, ("ricetta", "altro"), MAX_OTHER_DOCUMENTS)
        if others:
            lines.append("Ricette e altri documenti:")
            for doc in others:
                when = format_date(doc["doc_date"]) if doc["doc_date"] else "data non indicata"
                lines.append(f"- {when} ({doc['kind']}): {doc['summary']}")
        return "\n".join(lines)

    async def ask(self, sender: User, question: str | None = None, audio: bytes | None = None, audio_mime: str = "") -> str:
        context = self.build_context(self.accessible_patients(sender))
        answer = await self._answerer.answer(context, sender.name, question, audio, audio_mime)
        self._log("domanda", f"{sender.name}: {'vocale' if audio else 'testo'}")
        return answer
