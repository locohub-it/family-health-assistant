"""Utenti approvati: la lista bianca del bot, il calendario e il nome reale di ciascuno."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database
from .settings import Settings

MAX_NAME = 60


@dataclass(frozen=True)
class User:
    id: int
    telegram_id: int
    name: str  # come lo chiama il bot; è anche il nome della sua cartella
    role: str
    calendar_id: str
    first_name: str = ""  # nome e cognome reali, come scritti su ricette e referti
    last_name: str = ""

    @property
    def composio_user_id(self) -> str:
        """Identità dell'utente su Composio: stabile, così il collegamento Google sopravvive ai riavvii."""
        return f"famiglia-{self.telegram_id}"

    @property
    def full_name(self) -> str:
        """Nome reale se c'è, altrimenti quello breve: serve ad abbinare i documenti alla persona."""
        real = f"{self.first_name} {self.last_name}".strip()
        return real or self.name


def _user(row) -> User:
    return User(
        row["id"], row["telegram_id"], row["name"], row["role"], row["calendar_id"], row["first_name"], row["last_name"]
    )


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _validated(name: str, role: str, first_name: str, last_name: str) -> tuple[str, str, str, str]:
    name, role, first_name, last_name = _clean(name), _clean(role), _clean(first_name), _clean(last_name)
    name = name or first_name  # senza un nome breve, il bot usa il nome reale
    if not name:
        raise ValueError("Inserisci almeno il nome")
    for label, value in (("Il nome", name), ("Il nome reale", first_name), ("Il cognome", last_name)):
        if len(value) > MAX_NAME:
            raise ValueError(f"{label} è troppo lungo (massimo {MAX_NAME} caratteri)")
    if len(role) > 40:
        raise ValueError("Il ruolo è troppo lungo (massimo 40 caratteri)")
    return name, role, first_name, last_name


def coordinator_of(settings: Settings, users: "UserStore") -> User | None:
    """L'utente scelto come coordinatore, se esiste ancora."""
    raw = settings.get("coordinator_user_id")
    return users.get(int(raw)) if raw.isdigit() else None


class UserStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def all(self) -> list[User]:
        return [_user(r) for r in self._db.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE")]

    def get(self, user_id: int) -> User | None:
        rows = self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return _user(rows[0]) if rows else None

    def by_telegram_id(self, telegram_id: int) -> User | None:
        """None = utente non approvato: il bot non deve rispondergli."""
        rows = self._db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        return _user(rows[0]) if rows else None

    def add(self, telegram_id: int, name: str = "", role: str = "", first_name: str = "", last_name: str = "") -> User:
        if telegram_id <= 0:
            raise ValueError("L'ID Telegram deve essere un numero positivo")
        name, role, first_name, last_name = _validated(name, role, first_name, last_name)
        if self.by_telegram_id(telegram_id):
            raise ValueError("Esiste già un utente con questo ID Telegram")
        user_id = self._db.execute_returning_id(
            "INSERT INTO users (telegram_id, name, role, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, name, role, first_name, last_name),
        )
        return self.get(user_id)

    def update(self, user_id: int, name: str, role: str, first_name: str, last_name: str) -> User:
        """Cambia nome e ruolo. L'ID Telegram e il calendario collegato restano quelli."""
        if self.get(user_id) is None:
            raise ValueError("Utente non trovato")
        name, role, first_name, last_name = _validated(name, role, first_name, last_name)
        self._db.execute(
            "UPDATE users SET name = ?, role = ?, first_name = ?, last_name = ? WHERE id = ?",
            (name, role, first_name, last_name, user_id),
        )
        return self.get(user_id)

    def set_calendar(self, user_id: int, calendar_id: str) -> None:
        self._db.execute("UPDATE users SET calendar_id = ? WHERE id = ?", (calendar_id.strip() or "primary", user_id))

    def remove(self, user_id: int) -> None:
        self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
