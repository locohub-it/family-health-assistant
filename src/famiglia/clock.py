"""Fuso orario della famiglia: le date dei documenti sono sempre locali."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Europe/Rome")


def now() -> datetime:
    return datetime.now(TIMEZONE)
