"""Impostazioni modificabili dal pannello web, salvate in SQLite (segreti cifrati)."""

from __future__ import annotations

import base64
import hashlib
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken

from .db import Database

DEFAULTS: dict[str, str] = {
    # Telegram
    "bot_token": "",
    # Gemini
    "gemini_api_key": "",
    "gemini_model": "gemini-3.8-flash",
    # Se il modello principale ha finito la quota o non risponde, si prova questo (vuoto = nessuno)
    "gemini_fallback_model": "gemini-3.5-flash-lite",
    # Intelligenza artificiale: per ogni funzione (docs, chat) un servizio e un modello principali, e una riserva.
    # I servizi (nome, tipo, indirizzo, chiave cifrata) stanno nella tabella ai_services.
    "ai_docs_service": "",
    "ai_docs_model": "",
    "ai_docs_backup_service": "",
    "ai_docs_backup_model": "",
    "ai_chat_service": "",
    "ai_chat_model": "",
    "ai_chat_backup_service": "",
    "ai_chat_backup_model": "",
    "ai_context_chars": "9000",  # dati inviati a ogni domanda con i servizi diversi da Gemini (limiti di token bassi)
    "ai_migrated": "",
    # Impostazioni delle versioni precedenti: si leggono una volta sola per passarle ai servizi, poi si svuotano.
    "gemini_api_key": "",
    "gemini_model": "gemini-3.8-flash",
    "gemini_fallback_model": "",
    "groq_api_key": "",
    "deepseek_api_key": "",
    "custom_api_key": "",
    "custom_base_url": "",
    "ai_docs_provider": "gemini",
    "ai_chat_provider": "gemini",
    "ai_models_cache": "",
    # Composio (Google Calendar)
    "composio_api_key": "",
    # Utente che riceve sul proprio calendario le visite di tutta la famiglia (id dell'utente; vuoto = nessuno)
    "coordinator_user_id": "",
    # Dove salvare le foto dei documenti, relativo alla radice montata in /storage ("." = la radice).
    # Vuoto = la cartella predefinita «Documenti» dentro la radice.
    "documents_dir": "",
    # Pannello
    "admin_password_hash": "",
}

SECRET_KEYS = {"bot_token", "gemini_api_key", "composio_api_key", "groq_api_key", "deepseek_api_key", "custom_api_key"}
BOT_KEYS = {"bot_token"}

REQUIRED_FOR_RUN = {
    "bot_token": "Token del bot Telegram",
}


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class Settings:
    def __init__(self, db: Database, secret_key: str) -> None:
        if not secret_key:
            raise RuntimeError("SECRET_KEY mancante nel file .env")
        self._db = db
        self._fernet = _fernet(secret_key)
        self._listeners: list[Callable[[set[str]], None]] = []

    def get(self, key: str) -> str:
        rows = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return DEFAULTS.get(key, "")
        value = rows[0]["value"]
        if key in SECRET_KEYS and value:
            try:
                return self._fernet.decrypt(value.encode()).decode()
            except InvalidToken:
                return ""
        return value

    def encrypt(self, value: str) -> str:
        """Cifra un testo con la stessa chiave dei segreti (per le chiavi dei servizi AI)."""
        return self._fernet.encrypt(value.encode()).decode() if value else ""

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode() if token else ""
        except InvalidToken:
            return ""

    def is_set(self, key: str) -> bool:
        return bool(self.get(key))

    def update(self, values: dict[str, str]) -> set[str]:
        """Salva i valori cambiati e notifica i listener. Restituisce le chiavi cambiate."""
        changed: set[str] = set()
        for key, value in values.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            if self.get(key) == value:
                continue
            stored = self._fernet.encrypt(value.encode()).decode() if key in SECRET_KEYS and value else value
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )
            changed.add(key)
        if changed:
            for listener in self._listeners:
                listener(changed)
        return changed

    def on_change(self, listener: Callable[[set[str]], None]) -> None:
        self._listeners.append(listener)

    def missing_for_run(self) -> list[str]:
        """Cosa manca a livello di impostazioni semplici; Service aggiunge quello dei servizi AI."""
        return [label for key, label in REQUIRED_FOR_RUN.items() if not self.is_set(key)]
