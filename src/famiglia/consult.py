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
# (referti, visite, altri documenti) da tenere, dal più completo al più stringato
TRIM_STEPS = ((MAX_REPORTS, MAX_APPOINTMENTS, MAX_OTHER_DOCUMENTS), (8, 10, 5), (4, 6, 3), (2, 4, 2), (1, 2, 1))
DEFAULT_CONTEXT_CHARS = 60000
TRIM_NOTE = "\n\n(Per brevità sono mostrati solo i documenti più recenti.)"
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


def _indented(title: str, text: str) -> str:
    """Dettagli su più righe, rientrati sotto la voce a cui appartengono."""
    return f"  {title}:\n" + "\n".join(f"    {line.strip()}" for line in text.splitlines() if line.strip())


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

    def build_context(self, patients: list[User], budget: int | None = None) -> str:
        """Testo con i dati dei familiari. Se supera `budget` caratteri si tengono i documenti più recenti.

        I servizi con un limite basso di token al minuto non reggono uno storico lungo: meglio poche
        cose recenti che un errore. Si scende a passi (referti, visite, altri documenti) finché sta nel tetto.
        """
        budget = budget or DEFAULT_CONTEXT_CHARS
        context = ""
        for step, limits in enumerate(TRIM_STEPS):
            context = "\n\n".join(self._patient_section(p, limits) for p in patients)
            if len(context) <= budget:
                return context if step == 0 else context + TRIM_NOTE
        return context[: max(0, budget - len(TRIM_NOTE))].rstrip() + TRIM_NOTE  # storico enorme: taglio netto

    def _patient_section(self, patient: User, limits: tuple[int, int, int] = TRIM_STEPS[0]) -> str:
        max_reports, max_appointments, max_others = limits
        title = f"## {patient.name}" + (f" ({patient.role})" if patient.role else "")
        lines = [title]

        reports = self._records.documents(patient.id, ("referto",), max_reports)
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
            if doc["details"]:
                lines.append(_indented("Note del referto", doc["details"]))

        appointments = self._records.appointments(patient.id, max_appointments)
        lines.append("Visite ed esami prenotati:" if appointments else "Nessuna visita salvata.")
        for a in appointments:
            day, _, hour = a["starts_at"].partition("T")
            place = f" – {a['place']}" if a["place"] else ""
            when = f"{format_date(day)} {hour}" if hour else f"{format_date(day)} (ora da confermare)"
            lines.append(f"- {when}: {a['title']}{place}")
            if a["notes"]:
                lines.append(f"  Da ricordare (scritto sul foglio della prenotazione): {a['notes']}")

        others = self._records.documents(patient.id, ("ricetta", "altro"), max_others)
        if others:
            lines.append("Ricette e altri documenti:")
            for doc in others:
                when = format_date(doc["doc_date"]) if doc["doc_date"] else "data non indicata"
                lines.append(f"- {when} ({doc['kind']}): {doc['summary']}")
                if doc["details"]:
                    lines.append(_indented("Dettagli", doc["details"]))
        return "\n".join(lines)

    async def ask(self, sender: User, question: str | None = None, audio: bytes | None = None, audio_mime: str = "") -> str:
        budget_of = getattr(self._answerer, "context_budget", None)
        context = self.build_context(self.accessible_patients(sender), budget_of() if budget_of else None)
        answer = await self._answerer.answer(context, sender.name, question, audio, audio_mime)
        self._log("domanda", f"{sender.name}: {'vocale' if audio else 'testo'}")
        return answer
