"""Data e ora scritte a mano («26/10 alle 15», «26 ottobre 15:30», «domani alle 9»), senza intelligenza artificiale.

Regole fisse e prevedibili: se il testo non si capisce con certezza si chiede di riscriverlo, non si indovina.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONTHS = {
    "gennaio": 1, "gen": 1, "febbraio": 2, "feb": 2, "marzo": 3, "mar": 3, "aprile": 4, "apr": 4, "maggio": 5, "mag": 5,
    "giugno": 6, "giu": 6, "luglio": 7, "lug": 7, "agosto": 8, "ago": 8, "settembre": 9, "set": 9, "sett": 9,
    "ottobre": 10, "ott": 10, "novembre": 11, "nov": 11, "dicembre": 12, "dic": 12,
}
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))

HELP = (
    "Non ho capito la data. Scrivimela così, per esempio:\n"
    "• 26/10 alle 15\n"
    "• 26 ottobre 15:30\n"
    "• domani alle 9"
)

_TIME_WORDED = re.compile(r"\b(?:alle\s+ore|alle|ore)\s*(\d{1,2})(?:\s*[:.h]\s*(\d{2}))?(?:\s+e\s+(mezza|mezzo))?\b")
_TIME_COLON = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_NUMERIC = re.compile(r"\b(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{4}|\d{2}))?\b")
_DATE_WORDED = re.compile(rf"\b(\d{{1,2}})\s*°?\s*({_MONTH_NAMES})\b(?:\s+(\d{{4}}))?")
_RELATIVE = re.compile(r"\b(oggi|domani|dopodomani)\b")
_TIME_DOT = re.compile(r"(?<![\d/.:-])(\d{1,2})\.(\d{2})(?![\d/.:-])")  # «9.30», ma solo dopo aver tolto la data
_BARE_HOUR = re.compile(r"(?<![\d/.:-])(\d{1,2})(?![\d/.:-])")


class WhenError(ValueError):
    """Il testo non si capisce: il messaggio dice come riscriverlo."""


def _cut(text: str, match: re.Match) -> str:
    return text[: match.start()] + " " + text[match.end() :]


def _time_of(text: str) -> tuple[str, str]:
    """Ora scritta nel testo (HH:MM, vuota se non c'è) e il testo senza quella parte."""
    found = _TIME_WORDED.search(text)
    if found:
        hour, minute = int(found.group(1)), int(found.group(2) or (30 if found.group(3) else 0))
        return _clock(hour, minute), _cut(text, found)
    found = _TIME_COLON.search(text)
    if found:
        return _clock(int(found.group(1)), int(found.group(2))), _cut(text, found)
    return "", text


def _clock(hour: int, minute: int) -> str:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WhenError("L'ora non è valida: scrivila da 0 a 23, per esempio «alle 15» o «15:30».")
    return f"{hour:02d}:{minute:02d}"


def _date_of(text: str, today: date) -> tuple[date, str]:
    """Il giorno scritto nel testo e il testo senza quella parte."""
    found = _RELATIVE.search(text)
    if found:
        offset = {"oggi": 0, "domani": 1, "dopodomani": 2}[found.group(1)]
        return today + timedelta(days=offset), _cut(text, found)
    for pattern, worded in ((_DATE_NUMERIC, False), (_DATE_WORDED, True)):
        found = pattern.search(text)
        if not found:
            continue
        day = int(found.group(1))
        month = MONTHS[found.group(2)] if worded else int(found.group(2))
        year_text = found.group(3)
        year = int(year_text) + (2000 if year_text and len(year_text) == 2 else 0) if year_text else today.year
        try:
            result = date(year, month, day)
        except ValueError:
            raise WhenError(f"Il {day}/{month} non è una data che esiste. {HELP}") from None
        if not year_text and result < today:
            result = date(year + 1, month, day)  # senza l'anno si intende la prossima volta che cade
        return result, _cut(text, found)
    raise WhenError(HELP)


def parse_when(text: str, today: date) -> tuple[str, str]:
    """Data (AAAA-MM-GG) e ora (HH:MM, vuota se non scritta) di una frase come «26 ottobre alle 15»."""
    clean = re.sub(r"[,;]", " ", (text or "").lower())
    hour, rest = _time_of(clean)
    day, rest = _date_of(rest, today)
    if not hour:
        dotted = _TIME_DOT.search(rest)
        if dotted:
            hour, rest = _clock(int(dotted.group(1)), int(dotted.group(2))), _cut(rest, dotted)
        else:
            bare = _BARE_HOUR.search(rest)
            if bare:  # «26 ottobre 15»: il numero rimasto è l'ora
                hour, rest = _clock(int(bare.group(1)), 0), _cut(rest, bare)
    if re.search(r"\d", rest):  # una cifra che non so leggere: meglio chiedere che ignorarla
        raise WhenError(HELP)
    return day.isoformat(), hour


def is_past(day: str, hour: str, now: datetime) -> bool:
    """La visita è già passata? Senza ora conta il giorno."""
    if hour:
        return f"{day}T{hour}" < now.strftime("%Y-%m-%dT%H:%M")
    return day < now.strftime("%Y-%m-%d")


_KIND_WORDS = re.compile(
    r"\b(visita|controllo|esame|analisi|prelievo|ecograf\w*|risonanza|radiograf\w*|vaccin\w*|tac|mammograf\w*|ecg|"
    r"elettrocardiogramma|holter|terapia|seduta|day hospital)\b",
    re.IGNORECASE,
)


def make_title(text: str) -> str:
    """«dalla dottoressa Rossi» diventa «Visita dalla dottoressa Rossi»; «Prelievo del sangue» resta com'è."""
    title = re.sub(r"\s+", " ", text or "").strip(" .")[:80]
    if not title:
        return "Visita medica"
    if not _KIND_WORDS.search(title):
        title = f"Visita {title[0].lower()}{title[1:]}"
    return title[0].upper() + title[1:]
