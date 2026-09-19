"""Sceglie servizio e modello per ogni funzione (lettura dei documenti, domande), con una riserva."""

from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from .ai import ROLES, AiError, Endpoint, NotConfigured, suggest_model, vision_candidates, whisper_model
from .gemini import Extraction, Gemini
from .openai_compat import OpenAICompat
from .services import AiService, AiServices
from .settings import Settings

GEMINI_CONTEXT_CHARS = 60000
DEFAULT_OTHER_CONTEXT_CHARS = 9000  # circa 3.000 token: sta nel limite di 8.000 al minuto del piano gratuito di Groq
MIN_CONTEXT_CHARS = 2000
T = TypeVar("T")


class AiRouter:
    """Ha la stessa interfaccia per documenti e domande, ma inoltra al servizio scelto (e alla riserva se serve)."""

    def __init__(self, services: AiServices, settings: Settings, gemini: Gemini, openai: OpenAICompat, log: Callable[[str, str], None]) -> None:
        self._services = services
        self._settings = settings
        self._gemini = gemini
        self._openai = openai
        self._log = log

    # --- Scelte dell'admin -----------------------------------------------------------

    @staticmethod
    def prefix(role: str, backup: bool = False) -> str:
        return f"ai_{role}_backup" if backup else f"ai_{role}"

    def slot(self, role: str, backup: bool = False) -> tuple[AiService | None, str]:
        """Servizio e modello scelti per una funzione (principale o riserva); il servizio è None se non scelto o rimosso."""
        prefix = self.prefix(role, backup)
        raw = self._settings.get(f"{prefix}_service")
        service = self._services.get(int(raw)) if raw.isdigit() else None
        return service, self._settings.get(f"{prefix}_model")

    def _endpoint(self, role: str, backup: bool) -> tuple[AiService, Endpoint]:
        service, model = self.slot(role, backup)
        if service is None:
            raise NotConfigured(f"Nessun servizio scelto per «{ROLES[role]}»: chi gestisce il bot deve sceglierlo in Modelli IA.")
        if not model:
            raise NotConfigured(f"Nessun modello scelto per «{ROLES[role]}» ({service.name}): chi gestisce il bot deve sceglierlo in Modelli IA.")
        return service, service.endpoint(model)

    def _backend(self, service: AiService):
        return self._gemini if service.kind == "gemini" else self._openai

    def context_budget(self) -> int:
        """Quanti caratteri di dati mandare a ogni domanda: il più piccolo tra i servizi che potrebbero rispondere."""
        try:
            configured = max(MIN_CONTEXT_CHARS, int(self._settings.get("ai_context_chars")))
        except ValueError:
            configured = DEFAULT_OTHER_CONTEXT_CHARS
        budgets = [
            GEMINI_CONTEXT_CHARS if service.kind == "gemini" else configured
            for service, _ in (self.slot("chat", False), self.slot("chat", True))
            if service is not None
        ]
        return min(budgets) if budgets else configured

    # --- Esecuzione con riserva ----------------------------------------------------------

    async def _run(self, role: str, action: Callable[[AiService, Endpoint], Awaitable[T]]) -> T:
        """Prova la principale; se non risponde (quota, errore, servizio assente) prova la riserva, se c'è."""
        failures: list[AiError] = []
        for backup in (False, True):
            if backup and self.slot(role, True)[0] is None:
                break
            try:
                service, endpoint = self._endpoint(role, backup)
                result = await action(service, endpoint)
            except AiError as exc:
                failures.append(exc)
                continue
            if failures:
                self._log(
                    "errore",
                    f"{ROLES[role]}: la principale non ha risposto ({failures[0]}); ha risposto la riserva ({service.name} · {endpoint.model})",
                )
            return result
        if len(failures) > 1:
            self._log("errore", f"{ROLES[role]}: anche la riserva non ha risposto ({failures[1]})")
        raise failures[0]  # il messaggio più utile è quello della principale

    async def analyze_document(self, data: bytes, mime: str) -> Extraction:
        async def action(service: AiService, endpoint: Endpoint) -> Extraction:
            return await self._backend(service).analyze_document(endpoint, data, mime)

        return await self._run("docs", action)

    async def answer(
        self, context: str, sender_name: str, question: str | None = None, audio: bytes | None = None, audio_mime: str = ""
    ) -> str:
        async def action(service: AiService, endpoint: Endpoint) -> str:
            if service.kind == "gemini":
                return await self._gemini.answer(endpoint, context, sender_name, question, audio, audio_mime)
            text = await self._transcribe(service, endpoint, audio, audio_mime) if audio else (question or "")
            return await self._openai.answer(endpoint, context, sender_name, text)

        return await self._run("chat", action)

    async def _transcribe(self, service: AiService, endpoint: Endpoint, audio: bytes, mime: str) -> str:
        """I vocali si trascrivono con il modello Whisper del servizio, se ne ha uno."""
        model = whisper_model(list(service.models))
        if not model:
            try:
                model = whisper_model(await self.refresh_models(service.id))
            except AiError:
                model = ""
        if not model:
            raise AiError(f"Con {service.name} non posso ascoltare i vocali: scrivimi la domanda.")
        return await self._openai.transcribe(endpoint, model, audio, mime)

    # --- Elenco dei modelli e scelta automatica -------------------------------------------

    async def refresh_models(self, service_id: int) -> list[str]:
        """Chiede al servizio i modelli disponibili con la sua chiave e li tiene da parte."""
        service = self._services.get(service_id)
        if service is None:
            raise AiError("Servizio non trovato.")
        ids = await self._backend(service).list_models(service.endpoint())
        self._services.set_models(service_id, ids)
        return ids

    async def refresh_and_pick(self) -> list[tuple[str, bool | None]]:
        """Aggiorna l'elenco dei servizi in uso e sceglie da solo il modello dove manca.

        Restituisce messaggi (testo, esito) da mostrare all'admin: True = riuscito, False = errore vero,
        None = nota (per esempio una chiave non ancora inserita, che non è un guasto).
        Un modello scelto dall'admin non viene mai sostituito: se non compare nell'elenco si segnala.
        """
        messages: list[tuple[str, bool | None]] = []
        listed: dict[int, list[str]] = {}
        for role, label in ROLES.items():
            for backup in (False, True):
                service, model = self.slot(role, backup)
                if service is None:
                    continue
                if service.id not in listed:
                    listed[service.id] = await self._refresh_safely(service, messages)
                ids = listed[service.id]
                if not ids:
                    continue
                name = f"{label} · {'riserva' if backup else 'principale'}"
                if not model:
                    chosen, text, ok = await self._choose(role, service, ids, name)
                    if chosen:
                        self._settings.update({f"{self.prefix(role, backup)}_model": chosen})
                    messages.append((text, ok))
                elif model not in ids:
                    # Potrebbe essere un nome valido ma non elencato: non si sostituisce, si segnala.
                    messages.append((f"{name}: «{model}» non compare tra i {len(ids)} modelli di {service.name}: verifica con «Prova»", False))
                else:
                    messages.append((f"{name}: {model} (trovato tra i {len(ids)} modelli di {service.name})", True))
        return messages

    async def _refresh_safely(self, service: AiService, messages: list) -> list[str]:
        try:
            return await self.refresh_models(service.id)
        except NotConfigured:
            messages.append((f"{service.name}: chiave non impostata, elenco dei modelli non caricato.", None))
        except AiError as exc:
            messages.append((f"{service.name}: {exc.user_message}", False))
        return []

    async def _choose(self, role: str, service: AiService, ids: list[str], name: str) -> tuple[str, str, bool]:
        if role == "docs" and service.kind == "openai":
            return await self._pick_vision_model(service, ids, name)
        chosen = suggest_model(role, ids)
        if chosen:
            return chosen, f"{name}: scelto {chosen} tra i {len(ids)} modelli di {service.name}", True
        return "", f"{name}: nessun modello adatto tra quelli di {service.name}: scrivi il nome a mano", False

    async def _pick_vision_model(self, service: AiService, ids: list[str], name: str) -> tuple[str, str, bool]:
        """Per i documenti serve la visione: si prova ogni candidato con un'immagine e si tiene il primo che la legge."""
        tried, last_error = [], ""
        for model in vision_candidates(ids):
            ok, detail = await self._openai.probe(service.endpoint(model), "docs")
            tried.append(model)
            if ok:
                return model, f"{name}: scelto {model}, che legge le immagini (provati {len(tried)} su {len(ids)} modelli di {service.name})", True
            last_error = detail
        return (
            "",
            f"{name}: nessun modello di {service.name} che legga le immagini (provati: {', '.join(tried) or 'nessuno'}). "
            f"Scrivi il nome a mano oppure scegli un altro servizio. Ultimo errore: {last_error}",
            False,
        )

    async def repick(self) -> list[tuple[str, bool | None]]:
        """Dimentica i modelli scelti e li sceglie di nuovo (per i documenti, con la prova delle immagini)."""
        cleared = {}
        for role in ROLES:
            for backup in (False, True):
                cleared[f"{self.prefix(role, backup)}_model"] = ""
        self._settings.update(cleared)
        return await self.refresh_and_pick()

    # --- Prova ------------------------------------------------------------------------

    async def probe(self, role: str) -> list[tuple[str, bool, str]]:
        """Prova reale del modello principale e di quello di riserva. Per i documenti verifica anche le immagini."""
        results: list[tuple[str, bool, str]] = []
        for backup in (False, True):
            service, model = self.slot(role, backup)
            which = "riserva" if backup else "principale"
            if service is None:
                if not backup:
                    results.append((f"{ROLES[role]} · {which}", False, "Nessun servizio scelto: sceglilo in «Modelli da usare»."))
                continue
            label = f"{ROLES[role]} · {which} · {service.name} · {model or 'nessun modello'}"
            if not model:
                results.append((label, False, "Nessun modello scelto."))
                continue
            ok, detail = await self._backend(service).probe(service.endpoint(model), role)
            if not ok and role == "docs" and "non sa leggere le immagini" in detail:
                detail += " Premi «Scegli di nuovo in automatico» per cercare un modello che le legga."
            results.append((label, ok, detail))
        return results

    def summary(self) -> list[tuple[str, str, str]]:
        """Per la pagina Stato: funzione, modello principale e riserva, in parole."""
        rows = []
        for role, label in ROLES.items():
            texts = []
            for backup in (False, True):
                service, model = self.slot(role, backup)
                texts.append(f"{service.name} · {model or 'modello da scegliere'}" if service else "")
            rows.append((label, texts[0] or "servizio da scegliere", texts[1]))
        return rows
