"""Visite con i pulsanti: segnarne una nuova, vederle, cambiarle o toglierle.

Niente intelligenza artificiale e nessun codice Telegram: si riceve un tocco o un testo e si restituisce cosa mostrare.
Il testo scritto viene interpretato solo mentre è in corso un passaggio guidato, o se è esattamente «modifica appuntamento»
o «elimina appuntamento»: una domanda normale non può essere scambiata per un inserimento.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import clock
from .documents import NOT_FOUND, DocumentService, format_date
from .users import User, UserStore
from .when import WhenError, is_past, make_title, parse_when

FLOW_TTL = 600  # secondi: un passaggio lasciato a metà non deve inghiottire la domanda di mezz'ora dopo
NEW, CANCEL, CONFIRM, PICK = "v:n", "v:x", "v:ok", "v:p:"  # prefissi dei pulsanti: v = nuova visita, a = una visita esistente

_PHRASE = re.compile(r"^\s*(modifica|elimina)\s+(?:l['’]\s*)?(?:gli\s+)?appuntament[oi]\s*[.!]?\s*$", re.IGNORECASE)

WHEN_PROMPT = "Per quando è la visita? Scrivimi il giorno e, se lo sai, l'ora. Per esempio:\n• 26/10 alle 15\n• 26 ottobre 15:30\n• domani alle 9"
TITLE_PROMPT = "Che visita è? Scrivimelo in poche parole, per esempio: dalla dottoressa Rossi, oculista, prelievo del sangue."
EXPIRED = "Questa richiesta è scaduta. Per ricominciare scrivi /visita."
PAST = "Quella data è già passata. Riscrivimela con la data giusta, anche con l'anno se serve."


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # righe di (scritta, codice)


def wants_list(text: str) -> str | None:
    """«modifica appuntamento» → "edit", «elimina appuntamento» → "delete"; qualunque altra frase → None."""
    found = _PHRASE.match(text or "")
    return {"modifica": "edit", "elimina": "delete"}[found.group(1).lower()] if found else None


def _when_text(starts_at: str) -> str:
    day, _, hour = starts_at.partition("T")
    return format_date(day) + (f" alle {hour}" if hour else " (orario da confermare)")


class Visits:
    def __init__(self, users: UserStore, documents: DocumentService, now: Callable = clock.now) -> None:
        self._users = users
        self._documents = documents
        self._now = now

    # --- Schede delle visite -----------------------------------------------------------

    def _card(self, row, patient_name: str) -> str:
        lines = [f"📅 {_when_text(row['starts_at'])}", f"«{row['title']}» – {patient_name}"]
        if row["place"]:
            lines.append(f"Dove: {row['place']}")
        return "\n".join(lines)

    def _name_of(self, user_id: int) -> str:
        found = self._users.get(user_id)
        return found.name if found else "?"

    def _card_reply(self, row, extra: str = "", actions: tuple[str, ...] = ("edit", "delete")) -> Reply:
        buttons = []
        if "edit" in actions:
            buttons.append(("✏️ Modifica", f"a:e:{row['id']}"))
        if "delete" in actions:
            buttons.append(("🗑️ Elimina", f"a:d:{row['id']}"))
        text = self._card(row, self._name_of(row["user_id"]))
        return Reply(f"{extra}\n\n{text}" if extra else text, [buttons] if buttons else [])

    def list_cards(self, user: User, action: str | None = None) -> list[Reply]:
        """Le prossime visite, una scheda ciascuna con i suoi pulsanti. `action` limita i pulsanti a «edit» o «delete»."""
        rows = self._documents.upcoming_appointments(user)
        if not rows:
            return [Reply("Non ho visite in programma per te.", [[("➕ Segna una visita", NEW)]])]
        header = {"edit": "Quale visita vuoi modificare?", "delete": "Quale visita vuoi eliminare?"}.get(action, "Ecco le tue prossime visite:")
        actions = (action,) if action else ("edit", "delete")
        return [Reply(header)] + [self._card_reply(row, actions=actions) for row in rows]

    # --- Nuova visita ----------------------------------------------------------------------

    def start_new(self, user: User, state: dict) -> Reply:
        state.clear()
        state.update(kind="new", at=time.time(), patient=user.id)
        people = self._users.all()
        if len(people) > 1:
            state["step"] = "patient"
            return self._ask_patient(user, people)
        state["step"] = "when"
        return Reply(WHEN_PROMPT, [[("✖️ Annulla", CANCEL)]])

    def _ask_patient(self, user: User, people: list[User]) -> Reply:
        ordered = [user] + [p for p in people if p.id != user.id]
        rows = [[("Per me" if p.id == user.id else p.name, f"{PICK}{p.id}")] for p in ordered]
        return Reply("Per chi è la visita?", rows + [[("✖️ Annulla", CANCEL)]])

    # --- Tocchi sui pulsanti ------------------------------------------------------------------

    async def on_button(self, user: User, state: dict, data: str) -> Reply:
        if data == CANCEL:
            state.clear()
            return Reply("Va bene, non ho cambiato niente.")
        if data == NEW:
            return self.start_new(user, state)
        if data.startswith(PICK):
            return self._picked(state, data.removeprefix(PICK))
        if data == CONFIRM:
            return await self._confirm(user, state)
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "a" and parts[2].isdigit():
            return await self._appointment_button(user, state, parts[1], int(parts[2]))
        return Reply(EXPIRED)

    def _picked(self, state: dict, raw: str) -> Reply:
        patient = self._users.get(int(raw)) if raw.isdigit() else None
        if state.get("kind") != "new" or state.get("step") != "patient" or patient is None or self._stale(state):
            return Reply(EXPIRED)
        state.update(patient=patient.id, step="when", at=time.time())
        return Reply(f"Visita per {patient.name}.\n\n{WHEN_PROMPT}", [[("✖️ Annulla", CANCEL)]])

    async def _confirm(self, user: User, state: dict) -> Reply:
        if state.get("kind") != "new" or state.get("step") != "confirm" or self._stale(state):
            return Reply(EXPIRED)
        patient = self._users.get(state["patient"])
        if patient is None:
            state.clear()
            return Reply(EXPIRED)
        starts_at, title = state["starts_at"], state["title"]
        state.clear()
        outcome = await self._documents.add_appointment(user, patient, starts_at, title)
        buttons = [[("✏️ Modifica", f"a:e:{outcome.appointment_id}"), ("🗑️ Elimina", f"a:d:{outcome.appointment_id}")]]
        return Reply(outcome.text, buttons if outcome.appointment_id else [])

    async def _appointment_button(self, user: User, state: dict, action: str, appointment_id: int) -> Reply:
        row = self._documents.own_appointment(user, appointment_id)[0]
        if row is None:
            return Reply(NOT_FOUND)
        if action == "b":  # indietro: si mostra di nuovo la scheda
            state.clear()
            return self._card_reply(row)
        if action == "e":
            return Reply(self._card(row, self._name_of(row["user_id"])) + "\n\nCosa vuoi cambiare?", [
                [("📅 Data e ora", f"a:ew:{appointment_id}")],
                [("📝 Tipo di visita", f"a:et:{appointment_id}")],
                [("📍 Dove si fa", f"a:ep:{appointment_id}")],
                [("↩️ Indietro", f"a:b:{appointment_id}")],
            ])
        if action in ("ew", "et", "ep"):
            field_name = {"ew": "when", "et": "title", "ep": "place"}[action]
            state.clear()
            state.update(kind="edit", at=time.time(), field=field_name, id=appointment_id)
            prompt = {
                "when": "Scrivi la nuova data e ora. Per esempio:\n• 26/10 alle 15\n• domani alle 9",
                "title": TITLE_PROMPT,
                "place": "Scrivi dove si fa la visita (studio, ospedale, indirizzo). Scrivi «-» per toglierlo.",
            }[field_name]
            return Reply(prompt, [[("✖️ Annulla", f"a:b:{appointment_id}")]])
        if action == "d":
            return Reply(
                self._card(row, self._name_of(row["user_id"])) + "\n\nVuoi eliminare questa visita? Sparirà anche dal calendario.",
                [[("🗑️ Sì, elimina", f"a:dy:{appointment_id}"), ("No, tienila", f"a:b:{appointment_id}")]],
            )
        if action == "dy":
            state.clear()
            return Reply(await self._documents.delete_appointment(user, appointment_id))
        return Reply(EXPIRED)

    # --- Testo scritto durante un passaggio guidato ---------------------------------------------

    def _stale(self, state: dict) -> bool:
        return time.time() - state.get("at", 0) > FLOW_TTL

    def is_active(self, state: dict) -> bool:
        if state.get("kind") and self._stale(state):
            state.clear()
        return bool(state.get("kind"))

    async def on_text(self, user: User, state: dict, text: str) -> Reply | None:
        """Continua il passaggio in corso. None = nessun passaggio in corso: il testo è una domanda normale."""
        if not self.is_active(state):
            return None
        if state["kind"] == "edit":
            return await self._edit_step(user, state, text)
        step = state.get("step")
        if step == "patient":
            return self._ask_patient(user, self._users.all())
        if step == "when":
            return self._when_step(state, text)
        if step == "title":
            state.update(title=make_title(text), step="confirm", at=time.time())
            patient = self._users.get(state["patient"])
            summary = f"📅 {_when_text(state['starts_at'])}\n«{state['title']}» – {patient.name if patient else '?'}"
            return Reply(f"Controlla se va bene:\n\n{summary}", [[("✅ Conferma", CONFIRM), ("✖️ Annulla", CANCEL)]])
        return Reply("Tocca uno dei pulsanti qui sopra, oppure /visita per ricominciare.")

    def _when_step(self, state: dict, text: str) -> Reply:
        try:
            day, hour = parse_when(text, self._now().date())
        except WhenError as exc:
            return Reply(str(exc), [[("✖️ Annulla", CANCEL)]])
        if is_past(day, hour, self._now()):
            return Reply(PAST, [[("✖️ Annulla", CANCEL)]])
        state.update(starts_at=f"{day}T{hour}" if hour else day, step="title", at=time.time())
        return Reply(TITLE_PROMPT, [[("✖️ Annulla", CANCEL)]])

    async def _edit_step(self, user: User, state: dict, text: str) -> Reply:
        appointment_id, which = state["id"], state["field"]
        cancel = [[("✖️ Annulla", f"a:b:{appointment_id}")]]
        changes: dict = {}
        if which == "when":
            try:
                day, hour = parse_when(text, self._now().date())
            except WhenError as exc:
                return Reply(str(exc), cancel)
            if is_past(day, hour, self._now()):
                return Reply(PAST, cancel)
            changes["starts_at"] = f"{day}T{hour}" if hour else day
        elif which == "title":
            changes["title"] = make_title(text)
        else:
            cleaned = re.sub(r"\s+", " ", text).strip()[:120]
            changes["place"] = "" if cleaned.lower() in {"-", "nessuno", "niente", "nessun luogo"} else cleaned
        state.clear()
        note = await self._documents.edit_appointment(user, appointment_id, **changes)
        row = self._documents.own_appointment(user, appointment_id)[0]
        if row is None:
            return Reply(note)
        return self._card_reply(row, extra=f"Fatto, ho cambiato la visita.\n{note}".strip())
