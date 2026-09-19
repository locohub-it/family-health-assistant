"""Utenti approvati: la lista bianca del bot e il calendario di ciascuno."""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database


@dataclass(frozen=True)
class User:
    id: int
    telegram_id: int
    name: str
    role: str
    calendar_id: str

    @property
    def composio_user_id(self) -> str:
        """Identità dell'utente su Composio: stabile, così il collegamento Google sopravvive ai riavvii."""
        return f"famiglia-{self.telegram_id}"


def _user(row) -> User:
    return User(row["id"], row["telegram_id"], row["name"], row["role"], row["calendar_id"])


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

    def add(self, telegram_id: int, name: str, role: str = "") -> User:
        name, role = name.strip(), role.strip()
        if telegram_id <= 0:
            raise ValueError("L'ID Telegram deve essere un numero positivo")
        if not name or len(name) > 60:
            raise ValueError("Il nome è obbligatorio (massimo 60 caratteri)")
        if len(role) > 40:
            raise ValueError("Il ruolo è troppo lungo (massimo 40 caratteri)")
        if self.by_telegram_id(telegram_id):
            raise ValueError("Esiste già un utente con questo ID Telegram")
        user_id = self._db.execute_returning_id(
            "INSERT INTO users (telegram_id, name, role) VALUES (?, ?, ?)", (telegram_id, name, role)
        )
        return self.get(user_id)

    def set_calendar(self, user_id: int, calendar_id: str) -> None:
        self._db.execute("UPDATE users SET calendar_id = ? WHERE id = ?", (calendar_id.strip() or "primary", user_id))

    def remove(self, user_id: int) -> None:
        self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
