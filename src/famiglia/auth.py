"""Password del pannello (bcrypt) salvata nelle impostazioni."""

from __future__ import annotations

import bcrypt

from .settings import Settings

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(settings: Settings, password: str) -> bool:
    stored = settings.get("admin_password_hash")
    return bool(stored) and bcrypt.checkpw(password.encode(), stored.encode())


def set_password(settings: Settings, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"La password deve avere almeno {MIN_PASSWORD_LENGTH} caratteri")
    settings.update({"admin_password_hash": hash_password(password)})


def bootstrap_password(settings: Settings, initial_password: str) -> None:
    """Usa ADMIN_PASSWORD del .env solo se non esiste ancora una password."""
    if not settings.is_set("admin_password_hash"):
        if not initial_password:
            raise RuntimeError("Imposta ADMIN_PASSWORD nel file .env per il primo accesso")
        set_password(settings, initial_password)
