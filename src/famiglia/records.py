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
        self, user_id: int, sender_telegram_id: int, kind: str, file_path: str, doc_date: str, summary: str, raw_json: str
    ) -> int:
        return self._db.execute_returning_id(
            "INSERT INTO documents (user_id, sender_telegram_id, kind, file_path, doc_date, summary, raw_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, sender_telegram_id, kind, file_path, doc_date, summary, raw_json, time.time()),
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

    def get_document(self, document_id: int):
        rows = self._db.execute("SELECT * FROM documents WHERE id = ?", (document_id,))
        return rows[0] if rows else None

    def appointments_of_document(self, document_id: int):
        return self._db.execute("SELECT * FROM appointments WHERE document_id = ?", (document_id,))

    def delete_document(self, document_id: int) -> None:
        """Le righe collegate (valori, appuntamenti) spariscono con il documento (ON DELETE CASCADE)."""
        self._db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
