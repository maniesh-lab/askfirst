"""Run IDs, run folders and the run index.

A "run" is one form fill or one triage pass. Each run gets:
- an ID that sorts by time and is easy to read: `form-20261103-0831-ab12`
- a folder `data/runs/<run_id>/` for its audit log, screenshots, review files and result
- a row in the run index (`data/app.sqlite`), so `runs list` can show every run and its status

The index uses the stdlib `sqlite3` module on purpose: one small table, no ORM.
"""

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUBMITTING = "submitting"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"
    NEEDS_HUMAN_CHECK = "needs_human_check"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_run_id(kind: str, now: datetime | None = None) -> str:
    """`<kind>-YYYYMMDD-HHMM-<4 hex>`: sorts by time, short enough to type."""
    if not kind.isidentifier() or "-" in kind:
        raise ValueError(f"run kind must be a simple word, got {kind!r}")
    stamp = (now or utc_now()).strftime("%Y%m%d-%H%M")
    return f"{kind}-{stamp}-{secrets.token_hex(2)}"


def run_kind(run_id: str) -> str:
    return run_id.split("-", 1)[0]


@dataclass(frozen=True)
class RunPaths:
    """Where one run's files live. `ensure()` makes the folders."""

    root: Path

    @classmethod
    def for_run(cls, data_dir: Path, run_id: str) -> RunPaths:
        if "/" in run_id or ".." in run_id:
            raise ValueError(f"bad run id {run_id!r}")
        return cls(Path(data_dir) / "runs" / run_id)

    @property
    def audit(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def review_json(self) -> Path:
        return self.root / "review.json"

    @property
    def review_html(self) -> Path:
        return self.root / "review.html"

    @property
    def result(self) -> Path:
        return self.root / "result.json"

    def ensure(self) -> RunPaths:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        return self


class RunRecord(BaseModel):
    id: str
    kind: str
    status: RunStatus
    title: str
    created_at: datetime
    updated_at: datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class RunIndex:
    """The `runs` table in `data/app.sqlite`. Each call opens and closes its own connection,
    so it is safe across processes (the CLI, a test subprocess, a graph run)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    @classmethod
    def in_data_dir(cls, data_dir: Path) -> RunIndex:
        return cls(Path(data_dir) / "app.sqlite")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # timeout: wait for another process's write instead of failing with "locked".
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create(self, run_id: str, title: str = "", status: RunStatus = RunStatus.RUNNING) -> None:
        now = utc_now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (id, kind, status, title, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, run_kind(run_id), status.value, title, now, now),
            )

    def set_status(self, run_id: str, status: RunStatus, title: str | None = None) -> None:
        now = utc_now().isoformat()
        with self._connect() as conn:
            if title is None:
                conn.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                    (status.value, now, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status = ?, title = ?, updated_at = ? WHERE id = ?",
                    (status.value, title, now, run_id),
                )

    def claim(self, run_id: str, expected: RunStatus, new: RunStatus) -> bool:
        """Move a run from `expected` to `new` only if it is still `expected` (compare-and-set).
        Two processes approving the same run at once: exactly one gets True."""
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (new.value, utc_now().isoformat(), run_id, expected.value),
            ).rowcount
        return changed == 1

    def get(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return RunRecord(**dict(row)) if row else None

    def list(
        self, status: RunStatus | None = None, kind: str | None = None, limit: int = 50
    ) -> list[RunRecord]:
        query, params = "SELECT * FROM runs WHERE 1=1", []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [RunRecord(**dict(r)) for r in rows]
