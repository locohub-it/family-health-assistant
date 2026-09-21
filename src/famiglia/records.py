"""Documenti, valori degli esami e appuntamenti salvati nel database."""

from __future__ import annotations

import time
from typing import Iterable

from .db import Database
from .gemini import LabResult


class Records:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add_document(
        self,
        user_id: int,
        sender_telegram_id: int,
        kind: str,
        file_path: str,
        doc_date: str,
        summary: str,
        raw_json: str,
        details: str = "",
    ) -> int:
        return self._db.execute_returning_id(
            "INSERT INTO documents (user_id, sender_telegram_id, kind, file_path, doc_date, summary, raw_json, created_at, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, sender_telegram_id, kind, file_path, doc_date, summary, raw_json, time.time(), details),
        )

    def add_lab_results(self, document_id: int, user_id: int, result_date: str, results: Iterable[LabResult]) -> int:
        count = 0
        for r in results:
            self._db.execute(
                "INSERT INTO lab_results (document_id, user_id, result_date, name, value, unit, reference, flag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (document_id, user_id, result_date, r.name, r.value, r.unit, r.reference, r.flag),
            )
            count += 1
        return count

    def add_appointment(
        self, document_id: int, user_id: int, starts_at: str, title: str, place: str, notes: str
    ) -> int:
        return self._db.execute_returning_id(
            "INSERT INTO appointments (document_id, user_id, starts_at, title, place, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (document_id, user_id, starts_at, title, place, notes, time.time()),
        )

    def set_event_id(self, appointment_id: int, event_id: str) -> None:
        self._db.execute("UPDATE appointments SET event_id = ? WHERE id = ?", (event_id, appointment_id))

    def set_coordinator_event(self, appointment_id: int, coordinator_user_id: int, event_id: str) -> None:
        self._db.execute(
            "UPDATE appointments SET coordinator_user_id = ?, coordinator_event_id = ? WHERE id = ?",
            (coordinator_user_id, event_id, appointment_id),
        )

    def get_appointment(self, appointment_id: int):
        rows = self._db.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        return rows[0] if rows else None

    def upcoming_appointments(self, user_ids: Iterable[int], from_date: str, limit: int):
        """Le visite di queste persone da `from_date` (AAAA-MM-GG) in poi, la più vicina per prima."""
        ids = list(user_ids)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return self._db.execute(
            f"SELECT * FROM appointments WHERE user_id IN ({marks}) AND starts_at >= ? ORDER BY starts_at, id LIMIT ?",
            (*ids, from_date, limit),
        )

    def pending_appointments(self, user_ids: Iterable[int]):
        """Le visite prescritte ma ancora senza data di prenotazione, di queste persone."""
        ids = list(user_ids)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return self._db.execute(f"SELECT * FROM appointments WHERE user_id IN ({marks}) AND starts_at = '' ORDER BY id", tuple(ids))

    def update_appointment(self, appointment_id: int, starts_at: str, title: str, place: str) -> None:
        self._db.execute(
            "UPDATE appointments SET starts_at = ?, title = ?, place = ? WHERE id = ?", (starts_at, title, place, appointment_id)
        )

    def clear_event_ids(self, appointment_id: int) -> None:
        self._db.execute(
            "UPDATE appointments SET event_id = '', coordinator_user_id = 0, coordinator_event_id = '' WHERE id = ?",
            (appointment_id,),
        )

    def delete_appointment(self, appointment_id: int) -> None:
        self._db.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))

    def get_document(self, document_id: int):
        rows = self._db.execute("SELECT * FROM documents WHERE id = ?", (document_id,))
        return rows[0] if rows else None

    def appointments_of_document(self, document_id: int):
        return self._db.execute("SELECT * FROM appointments WHERE document_id = ?", (document_id,))

    def delete_document(self, document_id: int) -> None:
        """Le righe collegate (valori, appuntamenti) spariscono con il documento (ON DELETE CASCADE)."""
        self._db.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    # --- Lettura per la consultazione ---------------------------------------------

    def managed_patient_ids(self, sender_telegram_id: int) -> set[int]:
        """Familiari per cui questa persona ha inviato documenti (oltre a sé stessa)."""
        rows = self._db.execute("SELECT DISTINCT user_id FROM documents WHERE sender_telegram_id = ?", (sender_telegram_id,))
        return {r["user_id"] for r in rows}

    def documents(self, user_id: int, kinds: tuple[str, ...], limit: int):
        marks = ",".join("?" for _ in kinds)
        return self._db.execute(
            f"SELECT * FROM documents WHERE user_id = ? AND kind IN ({marks}) "
            "ORDER BY COALESCE(NULLIF(doc_date, ''), strftime('%Y-%m-%d', created_at, 'unixepoch')) DESC, id DESC LIMIT ?",
            (user_id, *kinds, limit),
        )

    def lab_results_of(self, document_id: int):
        return self._db.execute("SELECT * FROM lab_results WHERE document_id = ? ORDER BY id", (document_id,))

    def appointments(self, user_id: int, limit: int):
        """Le visite con una data: quelle ancora senza data (in sospeso) si leggono con `pending_of`."""
        return self._db.execute(
            "SELECT * FROM appointments WHERE user_id = ? AND starts_at != '' ORDER BY starts_at DESC LIMIT ?", (user_id, limit)
        )

    def pending_of(self, user_id: int):
        return self._db.execute("SELECT * FROM appointments WHERE user_id = ? AND starts_at = '' ORDER BY id", (user_id,))
