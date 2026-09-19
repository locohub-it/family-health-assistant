"""Impostazioni modificabili dal pannello web, salvate in SQLite (segreti cifrati)."""

from __future__ import annotations

import base64
import hashlib
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken

from .ai import PROVIDERS, ROLES
from .db import Database

DEFAULTS: dict[str, str] = {
    # Telegram
    "bot_token": "",
    # Gemini
    "gemini_api_key": "",
    "gemini_model": "gemini-3.8-flash",
    # Se il modello principale ha finito la quota o non risponde, si prova questo (vuoto = nessuno)
    "gemini_fallback_model": "gemini-3.5-flash-lite",
    # Intelligenza artificiale per funzione: provider (gemini, groq, deepseek, custom) e modello
    "ai_docs_provider": "gemini",
    "ai_docs_model": "",
    "ai_chat_provider": "gemini",
    "ai_chat_model": "",
    "groq_api_key": "",
    "deepseek_api_key": "",
    "custom_api_key": "",
    "custom_base_url": "",
    "ai_context_chars": "9000",  # dati inviati a ogni domanda con i servizi diversi da Gemini (limiti di token bassi)
    "ai_models_cache": "",  # elenco dei modelli per provider, ricavato dal provider stesso (JSON)
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
        """Cosa manca per partire: il token e la chiave di ogni servizio AI effettivamente scelto."""
        missing = [label for key, label in REQUIRED_FOR_RUN.items() if not self.is_set(key)]
        seen: set[str] = set()
        for role in ROLES:
            provider = self.get(f"ai_{role}_provider")
            provider = provider if provider in PROVIDERS else "gemini"
            if provider in seen:
                continue
            seen.add(provider)
            if not self.is_set(f"{provider}_api_key"):
                missing.append(f"Chiave di {PROVIDERS[provider]['label']}")
            if provider == "custom" and not self.is_set("custom_base_url"):
                missing.append("Indirizzo del servizio compatibile OpenAI")
        return missing
