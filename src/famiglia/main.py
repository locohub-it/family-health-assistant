"""Avvio: pannello web e bot Telegram nello stesso processo asyncio."""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from .auth import bootstrap_password
from .service import Service
from .web.app import create_app


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret_key = os.environ.get("SECRET_KEY", "")
    service = Service(os.environ.get("DATA_DIR", "data"), secret_key, os.environ.get("STORAGE_ROOT", "/storage"))
    bootstrap_password(service.settings, os.environ.get("ADMIN_PASSWORD", ""))

    app = create_app(
        service,
        secret_key=secret_key,
        admin_user=os.environ.get("ADMIN_USER", "admin"),
        cookie_secure=os.environ.get("COOKIE_SECURE") == "1",
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=os.environ.get("LISTEN_HOST", "0.0.0.0"), port=int(os.environ.get("LISTEN_PORT", "8080")))
    )
    await server.serve()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
