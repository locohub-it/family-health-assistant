"""Cartella dei documenti: si sfoglia e si sceglie solo dentro la radice montata nel container.

Tutti i percorsi che arrivano dal pannello sono relativi alla radice e vengono risolti qui: un
percorso che esce dalla radice (`..`, symlink verso l'esterno) viene rifiutato.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

FORBIDDEN_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class StorageError(ValueError):
    pass


class Storage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @property
    def available(self) -> bool:
        return self.root.is_dir()

    def resolve(self, relative: str) -> Path:
        """Percorso assoluto di `relative`, garantito dentro la radice."""
        target = (self.root / (relative or ".")).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise StorageError("Percorso fuori dalla cartella radice")
        return target

    def relative(self, target: Path) -> str:
        rel = target.resolve().relative_to(self.root).as_posix()
        return rel or "."

    def subfolders(self, relative: str) -> list[str]:
        """Nomi delle sottocartelle (nascoste escluse). I symlink che escono dalla radice sono omessi."""
        folder = self.resolve(relative)
        if not folder.is_dir():
            raise StorageError("La cartella non esiste")
        names = []
        for entry in folder.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            try:
                self.resolve(self.relative(entry))
            except (StorageError, ValueError):
                continue
            names.append(entry.name)
        return sorted(names, key=str.casefold)

    def make_folder(self, relative: str, name: str) -> str:
        name = name.strip()
        if not name or name in {".", ".."} or name.startswith(".") or FORBIDDEN_NAME.search(name) or len(name) > 100:
            raise StorageError("Nome cartella non valido")
        parent = self.resolve(relative)
        if not parent.is_dir():
            raise StorageError("La cartella non esiste")
        new = self.resolve(self.relative(parent / name))
        try:
            new.mkdir(exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Impossibile creare la cartella: {exc.strerror or exc}") from exc
        return self.relative(new)

    def ensure_folder(self, relative: str) -> Path:
        """La cartella (e quelle sopra) viene creata se non c'è: un percorso mancante non deve fermare il bot."""
        folder = self.resolve(relative)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Impossibile creare la cartella: {exc.strerror or exc}") from exc
        return folder

    def check_writable(self, relative: str) -> Path:
        """Verifica che si possa scrivere davvero (permessi NAS, volume in sola lettura)."""
        folder = self.resolve(relative)
        if not folder.is_dir():
            raise StorageError("La cartella non esiste")
        probe = folder / f".scrittura-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise StorageError(f"Non riesco a scrivere in questa cartella: {exc.strerror or exc}") from exc
        return folder

    def save(self, base: str, parts: list[str], filename: str, data: bytes) -> str:
        """Scrive un file in base/parts/filename e restituisce il percorso relativo alla radice."""
        clean = [_safe_part(p) for p in parts]
        self.ensure_folder(base)
        folder = self.check_writable(base)
        for part in clean:
            folder = folder / part
            try:
                folder.mkdir(exist_ok=True)
            except OSError as exc:
                raise StorageError(f"Impossibile creare la cartella: {exc.strerror or exc}") from exc
        target = self.resolve(self.relative(folder / _safe_part(filename)))
        counter = 1
        stem, suffix = target.stem, target.suffix
        while target.exists():
            counter += 1
            target = target.with_name(f"{stem}-{counter}{suffix}")
        try:
            target.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"Non riesco a salvare il file: {exc.strerror or exc}") from exc
        return self.relative(target)

    def delete(self, relative: str) -> None:
        """Toglie un file salvato in precedenza; se non c'è più non è un errore."""
        if not relative:
            return
        target = self.resolve(relative)
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                raise StorageError(f"Non riesco a cancellare il file: {exc.strerror or exc}") from exc


def _safe_part(name: str) -> str:
    cleaned = FORBIDDEN_NAME.sub("_", name).strip().strip(".")
    if not cleaned:
        raise StorageError("Nome file o cartella non valido")
    return cleaned[:120]
