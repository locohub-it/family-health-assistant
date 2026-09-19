"""Collega tra loro database, impostazioni, utenti e cartella dei documenti."""

from __future__ import annotations

import time
from pathlib import Path

from .db import Database
from .settings import Settings
from .storage import Storage
from .users import UserStore


class Service:
    def __init__(self, data_dir: str | Path, secret_key: str, storage_root: str | Path) -> None:
        self.db = Database(Path(data_dir) / "famiglia.db")
        self.settings = Settings(self.db, secret_key)
        self.users = UserStore(self.db)
        self.storage = Storage(storage_root)

    def log(self, kind: str, detail: str = "") -> None:
        """Registro delle attività, mostrato nella pagina Stato."""
        self.db.execute("INSERT INTO activity (ts, kind, detail) VALUES (?, ?, ?)", (time.time(), kind, detail))

    def recent_activity(self, limit: int = 50):
        return self.db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
