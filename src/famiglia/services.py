"""Servizi AI inseriti dall'admin: nome, tipo, indirizzo e chiave (cifrata), con l'elenco dei loro modelli."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .ai import KINDS, ROLES, Endpoint
from .db import Database
from .settings import Settings

MAX_NAME = 40
ADDRESS = re.compile(r"^https?://[^\s]+$")


@dataclass(frozen=True)
class AiService:
    id: int
    name: str
    kind: str
    base_url: str
    api_key: str
    models: tuple[str, ...] = ()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def endpoint(self, model: str = "") -> Endpoint:
        return Endpoint(self.kind, self.name, self.base_url, self.api_key, model)


class AiServices:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    # --- Lettura ---------------------------------------------------------------------

    def _service(self, row) -> AiService:
        try:
            models = tuple(str(m) for m in json.loads(row["models"] or "[]"))
        except ValueError:
            models = ()
        return AiService(row["id"], row["name"], row["kind"], row["base_url"], self._settings.decrypt(row["api_key"]), models)

    def all(self) -> list[AiService]:
        return [self._service(r) for r in self._db.execute("SELECT * FROM ai_services ORDER BY name COLLATE NOCASE")]

    def get(self, service_id: int) -> AiService | None:
        rows = self._db.execute("SELECT * FROM ai_services WHERE id = ?", (service_id,))
        return self._service(rows[0]) if rows else None

    # --- Scrittura -------------------------------------------------------------------

    def _validated(self, name: str, kind: str, base_url: str, own_id: int | None) -> tuple[str, str, str]:
        name = re.sub(r"\s+", " ", name or "").strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"Il nome è obbligatorio (massimo {MAX_NAME} caratteri)")
        if kind not in KINDS:
            raise ValueError("Tipo di servizio non valido")
        if any(s.name.casefold() == name.casefold() and s.id != own_id for s in self.all()):
            raise ValueError(f"Esiste già un servizio chiamato «{name}»")
        base_url = (base_url or "").strip().rstrip("/")
        if kind == "gemini":
            base_url = ""  # Gemini ha il suo indirizzo, incorporato nella libreria di Google
        elif not ADDRESS.match(base_url):
            raise ValueError("L'indirizzo deve iniziare con http:// o https://, ad esempio https://api.groq.com/openai/v1")
        return name, kind, base_url

    def add(self, name: str, kind: str, base_url: str, api_key: str) -> AiService:
        name, kind, base_url = self._validated(name, kind, base_url, None)
        api_key = (api_key or "").strip()
        if kind == "gemini" and not api_key:
            raise ValueError("Per Google Gemini serve la chiave API")
        service_id = self._db.execute_returning_id(
            "INSERT INTO ai_services (name, kind, base_url, api_key) VALUES (?, ?, ?, ?)",
            (name, kind, base_url, self._settings.encrypt(api_key)),
        )
        return self.get(service_id)

    def update(self, service_id: int, name: str, kind: str, base_url: str, api_key: str = "") -> AiService:
        """Cambia i dati del servizio; una chiave vuota lascia quella attuale."""
        current = self.get(service_id)
        if current is None:
            raise ValueError("Servizio non trovato")
        name, kind, base_url = self._validated(name, kind, base_url, service_id)
        key = (api_key or "").strip() or current.api_key
        if kind == "gemini" and not key:
            raise ValueError("Per Google Gemini serve la chiave API")
        # Cambiando indirizzo o tipo l'elenco dei modelli non vale più.
        keep_models = kind == current.kind and base_url == current.base_url
        self._db.execute(
            "UPDATE ai_services SET name = ?, kind = ?, base_url = ?, api_key = ?, models = ? WHERE id = ?",
            (name, kind, base_url, self._settings.encrypt(key), json.dumps(list(current.models)) if keep_models else "", service_id),
        )
        return self.get(service_id)

    def set_models(self, service_id: int, models: list[str]) -> None:
        self._db.execute("UPDATE ai_services SET models = ? WHERE id = ?", (json.dumps(models), service_id))

    def remove(self, service_id: int) -> None:
        self._db.execute("DELETE FROM ai_services WHERE id = ?", (service_id,))
        # Le funzioni che lo usavano restano senza servizio: l'admin dovrà sceglierne un altro.
        cleared = {}
        for role in ROLES:
            for prefix in (f"ai_{role}", f"ai_{role}_backup"):
                if self._settings.get(f"{prefix}_service") == str(service_id):
                    cleared[f"{prefix}_service"] = ""
                    cleared[f"{prefix}_model"] = ""
        if cleared:
            self._settings.update(cleared)

    # --- Passaggio dalla configurazione delle versioni precedenti -------------------------

    def migrate_legacy(self) -> None:
        """Trasforma le chiavi e le scelte salvate dalle versioni precedenti in servizi. Una volta sola."""
        settings = self._settings
        if settings.get("ai_migrated") == "1":
            return
        try:
            cache = json.loads(settings.get("ai_models_cache") or "{}")
        except ValueError:
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        known = (
            ("gemini", "Google Gemini", "gemini", ""),
            ("groq", "Groq", "openai", "https://api.groq.com/openai/v1"),
            ("deepseek", "DeepSeek", "openai", "https://api.deepseek.com"),
            ("custom", "Altro servizio", "openai", settings.get("custom_base_url")),
        )
        ids: dict[str, int] = {}
        for provider, name, kind, base_url in known:
            key = settings.get(f"{provider}_api_key")
            if not key or (kind == "openai" and not base_url):
                continue
            service = self.add(name, kind, base_url, key)
            ids[provider] = service.id
            if isinstance(cache.get(provider), list):
                self.set_models(service.id, [str(m) for m in cache[provider]])

        updates: dict[str, str] = {}
        for role in ROLES:
            provider = settings.get(f"ai_{role}_provider") or "gemini"
            if provider not in ids:
                continue
            model = settings.get(f"ai_{role}_model") or (settings.get("gemini_model") if provider == "gemini" else "")
            updates[f"ai_{role}_service"], updates[f"ai_{role}_model"] = str(ids[provider]), model
            if provider == "gemini" and settings.get("gemini_fallback_model"):
                updates[f"ai_{role}_backup_service"] = str(ids[provider])
                updates[f"ai_{role}_backup_model"] = settings.get("gemini_fallback_model")
        legacy = ("gemini_api_key", "groq_api_key", "deepseek_api_key", "custom_api_key", "custom_base_url",
                  "gemini_fallback_model", "ai_models_cache", "ai_docs_provider", "ai_chat_provider")
        settings.update({**updates, **{key: "" for key in legacy}, "ai_migrated": "1"})
