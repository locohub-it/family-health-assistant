"""Collega tra loro database, impostazioni, utenti, Gemini, documenti e bot Telegram."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .bot import BotRunner
from .calendar import Calendar
from .consult import Answerer, Consultant
from .db import Database
from .documents import DEFAULT_DOCUMENTS_DIR, DocumentService
from .gemini import DocumentReader, Gemini
from .openai_compat import OpenAICompat
from .ai import ROLES
from .router import AiRouter
from .services import AiServices
from .records import Records
from .settings import BOT_KEYS, Settings
from .storage import Storage, StorageError
from .users import UserStore


class Service:
    def __init__(
        self,
        data_dir: str | Path,
        secret_key: str,
        storage_root: str | Path,
        gemini_http: httpx.AsyncClient | None = None,
        reader: DocumentReader | None = None,
        answerer: Answerer | None = None,
        composio_factory: Callable[[str], Any] | None = None,
        ai_http: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = Database(Path(data_dir) / "famiglia.db")
        self.settings = Settings(self.db, secret_key)
        self.users = UserStore(self.db)
        self.storage = Storage(storage_root)
        self.fallback_storage = Storage(Path(data_dir) / "documenti")  # nel volume dei dati: sempre disponibile
        self.records = Records(self.db)
        self.ai_services = AiServices(self.db, self.settings)
        self.ai_services.migrate_legacy()  # passa a "servizi" la configurazione AI delle versioni precedenti
        self.gemini = Gemini(gemini_http, self.log)
        self.openai = OpenAICompat(ai_http)
        self.ai = AiRouter(self.ai_services, self.settings, self.gemini, self.openai, self.log)
        self.calendar = Calendar(self.settings, composio_factory)
        self.documents = DocumentService(
            self.settings, self.users, self.storage, self.records, reader or self.ai, self.log, self.calendar, self.fallback_storage
        )
        self.consultant = Consultant(self.users, self.records, answerer or self.ai, self.log)
        self.bot = BotRunner(self.settings, self.users, self.documents, self.consultant, self.log, self.calendar)
        self.settings.on_change(self._settings_changed)

    def _settings_changed(self, keys: set[str]) -> None:
        if keys & BOT_KEYS:
            self.bot.restart()

    def missing_for_run(self) -> list[str]:
        """Cosa manca per partire: il token del bot e, per ogni funzione, un servizio AI con la sua chiave e un modello."""
        missing = self.settings.missing_for_run()
        for role, label in ROLES.items():
            service, model = self.ai.slot(role)
            if service is None:
                missing.append(f"Servizio AI per «{label}»")
            elif service.kind == "gemini" and not service.has_key:
                missing.append(f"Chiave di {service.name}")
            elif not model:
                missing.append(f"Modello per «{label}»")
        return missing

    def ensure_default_folder(self) -> None:
        """Crea «Documenti» nella radice, così esiste e si vede già nel selettore."""
        if not self.storage.available:
            return
        try:
            self.storage.ensure_folder(DEFAULT_DOCUMENTS_DIR)
        except StorageError as exc:
            self.log("errore", f"Cartella predefinita non creabile: {exc}")

    def log(self, kind: str, detail: str = "") -> None:
        """Registro delle attività, mostrato nella pagina Stato."""
        self.db.execute("INSERT INTO activity (ts, kind, detail) VALUES (?, ?, ?)", (time.time(), kind, detail))

    def recent_activity(self, limit: int = 50):
        return self.db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
