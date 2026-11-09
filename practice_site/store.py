"""Submissions storage for the practice site: one SQLite table (stdlib `sqlite3`)."""

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PREFIXES = {"job_apply": "JOB", "listing": "LST", "draft": "DRF", "contact": "CON", "survey": "SRV"}


class SubmissionStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                " id TEXT PRIMARY KEY, form TEXT NOT NULL, data TEXT NOT NULL,"
                " flagged_bot INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, form: str, data: dict[str, Any], flagged_bot: bool = False) -> str:
        confirmation = f"{PREFIXES[form]}-{secrets.token_hex(3).upper()}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO submissions (id, form, data, flagged_bot, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    confirmation,
                    form,
                    json.dumps(data, ensure_ascii=False),
                    int(flagged_bot),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        return confirmation

    def all(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM submissions ORDER BY rowid").fetchall()
        return [
            {**dict(r), "data": json.loads(r["data"]), "flagged_bot": bool(r["flagged_bot"])}
            for r in rows
        ]

    def clear(self) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM submissions").rowcount
