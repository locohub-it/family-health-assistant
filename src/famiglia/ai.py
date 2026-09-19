"""Provider di intelligenza artificiale: errori comuni, provider predefiniti e scelta automatica del modello."""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass


class AiError(Exception):
    """Errore di un provider AI con un messaggio già pronto da mostrare all'utente."""

    def __init__(self, user_message: str, detail: str = "", quota: bool = False) -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.quota = quota  # vero se il provider ha risposto 429 (troppe richieste o quota finita)


class NotConfigured(AiError):
    """Manca la chiave o l'indirizzo: non è un guasto, è una cosa ancora da inserire."""


# Tipi di servizio: Gemini usa l'SDK di Google, tutti gli altri parlano il formato compatibile OpenAI.
KINDS: dict[str, str] = {"openai": "Compatibile OpenAI", "gemini": "Google Gemini"}
# Solo suggerimenti per l'indirizzo: l'admin inserisce a mano nome e chiave di qualunque servizio.
SUGGESTED_ADDRESSES: tuple[tuple[str, str], ...] = (
    ("Groq", "https://api.groq.com/openai/v1"),
    ("DeepSeek", "https://api.deepseek.com"),
    ("OpenRouter", "https://openrouter.ai/api/v1"),
    ("OpenAI", "https://api.openai.com/v1"),
    ("Ollama sul tuo server", "http://localhost:11434/v1"),
)

# Le due funzioni per cui si sceglie un modello: leggere i documenti (serve la visione) e rispondere alle domande.
ROLES: dict[str, str] = {"docs": "Lettura dei documenti", "chat": "Domande e risposte"}


@dataclass(frozen=True)
class Endpoint:
    kind: str  # "gemini" | "openai"
    label: str
    base_url: str
    api_key: str
    model: str


# Modelli che non rispondono in chat: audio, embedding, sicurezza...
NOT_FOR_CHAT = re.compile(
    r"whisper|tts|speech|embed|guard|moderation|playai|transcri|rerank|imagen|dall-e|veo|lyria|compound|safeguard", re.I
)
# Nomi che, per ruolo, indicano il modello più adatto; il primo che compare vince.
PREFERRED = {
    "docs": ("scout", "maverick", "vision", "gemini", "gpt-4o", "pixtral", "llava", "-vl", "qwen3", "qwen"),
    "chat": ("deepseek-chat", "v4-flash", "deepseek-flash", "flash", "versatile", "gpt-oss-120b", "70b", "instruct", "chat"),
}
UNSTABLE = re.compile(r"lite|preview|exp|beta|mini|tiny|small", re.I)


def usable_models(ids: list[str]) -> list[str]:
    return [i for i in ids if not NOT_FOR_CHAT.search(i)]


def _version(model_id: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", model_id))


def suggest_model(role: str, ids: list[str]) -> str:
    """Il modello che sembra più adatto, così l'admin non deve cercare il nome giusto.

    Si scorrono i criteri del ruolo: il primo che trova un modello stabile vince, e tra i candidati vince
    la versione più alta. I modelli sperimentali (lite, preview, exp...) restano l'ultima risorsa.
    """
    usable = usable_models(ids)
    unstable_backup: list[str] = []
    for pattern in PREFERRED[role]:
        matches = [i for i in usable if pattern in i.lower()]
        stable = [i for i in matches if not UNSTABLE.search(i)]
        if stable:
            return max(stable, key=lambda i: (_version(i), i))
        unstable_backup += matches
    if unstable_backup:
        return max(unstable_backup, key=lambda i: (_version(i), i))
    return usable[0] if usable else ""


def vision_candidates(ids: list[str], limit: int = 6) -> list[str]:
    """Modelli da provare, nell'ordine, per trovarne uno che legga le immagini.

    I nomi non bastano a saperlo (cambiano ogni pochi mesi): il router li prova davvero con un'immagine di prova.
    Prima quelli con un nome noto per la visione (stabili e più recenti per primi), poi gli altri.
    """
    usable = usable_models(ids)
    ordered: list[str] = []
    for pattern in PREFERRED["docs"]:
        matches = [i for i in usable if pattern in i.lower() and i not in ordered]
        ordered += sorted(matches, key=lambda i: (bool(UNSTABLE.search(i)), tuple(-n for n in _version(i)), i))
    ordered += [i for i in usable if i not in ordered]
    return ordered[:limit]


def whisper_model(ids: list[str]) -> str:
    """Il modello di trascrizione vocale, se il provider ne ha uno."""
    matches = [i for i in ids if "whisper" in i.lower()]
    return max(matches, key=lambda i: ("turbo" in i.lower(), _version(i), i)) if matches else ""


def tiny_png(size: int = 32) -> bytes:
    """Un'immagine bianca valida, per provare se un modello accetta le immagini."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\xff\xff" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
