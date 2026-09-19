"""Reimposta la password del pannello: python -m famiglia.set_password"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from .auth import set_password
from .db import Database
from .settings import Settings


def main() -> None:
    settings = Settings(Database(Path(os.environ.get("DATA_DIR", "data")) / "famiglia.db"), os.environ.get("SECRET_KEY", ""))
    password = getpass.getpass("Nuova password: ")
    if password != getpass.getpass("Ripeti password: "):
        sys.exit("Le password non coincidono")
    try:
        set_password(settings, password)
    except ValueError as exc:
        sys.exit(str(exc))
    print("Password aggiornata")


if __name__ == "__main__":
    main()
