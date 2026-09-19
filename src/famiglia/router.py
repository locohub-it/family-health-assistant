"""Sceglie il provider AI per ogni funzione (lettura dei documenti, domande) in base alle impostazioni del pannello."""

from __future__ import annotations

import json
from typing import Callable

from .ai import PROVIDERS, ROLES, AiError, Endpoint, suggest_model, whisper_model
from .gemini import Extraction, Gemini
from .openai_compat import OpenAICompat
from .settings import Settings

CACHE_KEY = "ai_models_cache"


class AiRouter:
    """Ha la stessa interfaccia di Gemini per documenti e domande, ma inoltra al provider scelto."""

    def __init__(self, settings: Settings, gemini: Gemini, openai: OpenAICompat, log: Callable[[str, str], None]) -> None:
        self._settings = settings
        self._gemini = gemini
        self._openai = openai
        self._log = log

    # --- Configurazione --------------------------------------------------------------

    def provider(self, role: str) -> str:
        value = self._settings.get(f"ai_{role}_provider")
        return value if value in PROVIDERS else "gemini"

    def model(self, role: str) -> str:
        return self._settings.get(f"ai_{role}_model")

    def endpoint(self, provider: str, model: str = "") -> Endpoint:
        preset = PROVIDERS[provider]
        base_url = preset["base_url"] or (self._settings.get("custom_base_url").strip() if provider == "custom" else "")
        key = self._settings.get(f"{provider}_api_key")
        if not key:
            raise AiError(f"{preset['label']} non è configurato: manca la chiave. Avvisa chi gestisce il bot.")
        if not base_url:
            raise AiError("Manca l'indirizzo del servizio compatibile OpenAI. Avvisa chi gestisce il bot.")
        return Endpoint(provider, preset["label"], base_url, key, model)

    def role_endpoint(self, role: str) -> Endpoint:
        model = self.model(role)
        if not model:
            raise AiError(f"Nessun modello scelto per «{ROLES[role]}»: chi gestisce il bot deve sceglierlo dal pannello.")
        return self.endpoint(self.provider(role), model)

    # --- Le due funzioni del bot -----------------------------------------------------

    async def analyze_document(self, data: bytes, mime: str) -> Extraction:
        if self.provider("docs") == "gemini":
            return await self._gemini.analyze_document(data, mime, self.model("docs") or None)
        return await self._openai.analyze_document(self.role_endpoint("docs"), data, mime)

    async def answer(
        self, context: str, sender_name: str, question: str | None = None, audio: bytes | None = None, audio_mime: str = ""
    ) -> str:
        if self.provider("chat") == "gemini":
            return await self._gemini.answer(context, sender_name, question, audio, audio_mime, self.model("chat") or None)
        endpoint = self.role_endpoint("chat")
        if audio:
            question = await self._transcribe(endpoint, audio, audio_mime)
        return await self._openai.answer(endpoint, context, sender_name, question or "")

    async def _transcribe(self, endpoint: Endpoint, audio: bytes, mime: str) -> str:
        """I vocali si trascrivono con il modello Whisper del provider, se ne ha uno."""
        model = whisper_model(self.cached_models(endpoint.provider))
        if not model:
            try:
                model = whisper_model(await self.refresh_models(endpoint.provider))
            except AiError:
                model = ""
        if not model:
            raise AiError(f"Con {endpoint.label} non posso ascoltare i vocali: scrivimi la domanda.")
        return await self._openai.transcribe(endpoint, model, audio, mime)

    # --- Elenco dei modelli ----------------------------------------------------------

    def cached_models(self, provider: str) -> list[str]:
        try:
            cache = json.loads(self._settings.get(CACHE_KEY) or "{}")
        except ValueError:
            return []
        return [str(m) for m in cache.get(provider, [])] if isinstance(cache, dict) else []

    async def refresh_models(self, provider: str) -> list[str]:
        """Chiede al provider i modelli disponibili con la chiave in uso e li tiene da parte."""
        ids = await self._gemini.list_models() if provider == "gemini" else await self._openai.list_models(self.endpoint(provider))
        try:
            cache = json.loads(self._settings.get(CACHE_KEY) or "{}")
        except ValueError:
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        cache[provider] = ids
        self._settings.update({CACHE_KEY: json.dumps(cache)})
        return ids

    async def refresh_and_pick(self) -> list[tuple[str, bool]]:
        """Aggiorna l'elenco dei provider in uso e sceglie da solo il modello dove manca o non esiste più.

        Restituisce messaggi (testo, ok) da mostrare all'admin.
        """
        messages: list[tuple[str, bool]] = []
        refreshed: dict[str, list[str]] = {}
        for role, label in ROLES.items():
            provider = self.provider(role)
            if provider not in refreshed:
                try:
                    refreshed[provider] = await self.refresh_models(provider)
                except AiError as exc:
                    refreshed[provider] = []
                    messages.append((f"{PROVIDERS[provider]['label']}: {exc.user_message}", False))
            ids = refreshed[provider]
            current, name = self.model(role), PROVIDERS[provider]["label"]
            if not ids:
                continue
            if provider == "gemini":
                # Per Gemini il modello resta quello di «Chiavi API», salvo una scelta esplicita per questa funzione.
                shown = current or self._settings.get("gemini_model")
                messages.append((f"{label}: Gemini, modello {shown} ({len(ids)} disponibili)", True))
            elif not current:
                chosen = suggest_model(role, ids)
                if chosen:
                    self._settings.update({f"ai_{role}_model": chosen})
                    messages.append((f"{label}: scelto {chosen} tra i {len(ids)} modelli di {name}", True))
                else:
                    messages.append((f"{label}: nessun modello adatto tra quelli di {name}: scrivi il nome a mano", False))
            elif current not in ids:
                # Potrebbe essere un nome valido ma non elencato: non si sostituisce, si segnala.
                messages.append((f"{label}: «{current}» non compare tra i {len(ids)} modelli di {name}: verifica con «Prova»", False))
            else:
                messages.append((f"{label}: {current} (trovato tra i {len(ids)} modelli di {name})", True))
        return messages

    # --- Prova ----------------------------------------------------------------------

    async def probe(self, role: str) -> tuple[str, bool, str]:
        """Prova reale del modello scelto per una funzione. Per i documenti verifica anche le immagini."""
        provider, model = self.provider(role), self.model(role)
        label = f"{ROLES[role]} · {PROVIDERS[provider]['label']} · {model or 'nessun modello'}"
        if provider == "gemini":
            ok, detail = await self._gemini.probe_model(model or self._settings.get("gemini_model"), image=role == "docs")
            return label, ok, detail
        try:
            endpoint = self.role_endpoint(role)
        except AiError as exc:
            return label, False, exc.user_message
        ok, detail = await self._openai.probe(endpoint, role)
        return label, ok, detail
