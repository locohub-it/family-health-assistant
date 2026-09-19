"""Collega tra loro database, impostazioni, utenti, Gemini, documenti e bot Telegram."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .bot import BotRunner
from .db import Database
from .documents import DocumentService
from .gemini import DocumentReader, Gemini
from .records import Records
from .settings import BOT_KEYS, Settings
from .storage import Storage
from .users import UserStore


class Service:
    def __init__(
        self,
        data_dir: str | Path,
        secret_key: str,
        storage_root: str | Path,
        gemini_http: httpx.AsyncClient | None = None,
        reader: DocumentReader | None = None,
    ) -> None:
        self.db = Database(Path(data_dir) / "famiglia.db")
        self.settings = Settings(self.db, secret_key)
        self.users = UserStore(self.db)
        self.storage = Storage(storage_root)
        self.records = Records(self.db)
        self.gemini = Gemini(self.settings, gemini_http)
        self.documents = DocumentService(
            self.settings, self.users, self.storage, self.records, reader or self.gemini, self.log
        )
        self.bot = BotRunner(self.settings, self.users, self.documents, self.log)
        self.settings.on_change(self._settings_changed)

    def _settings_changed(self, keys: set[str]) -> None:
        if keys & BOT_KEYS:
            self.bot.restart()

    def log(self, kind: str, detail: str = "") -> None:
        """Registro delle attività, mostrato nella pagina Stato."""
        self.db.execute("INSERT INTO activity (ts, kind, detail) VALUES (?, ?, ?)", (time.time(), kind, detail))

    def recent_activity(self, limit: int = 50):
        return self.db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
