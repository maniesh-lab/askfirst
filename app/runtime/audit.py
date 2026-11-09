"""The audit log: an append-only record of everything a run did, one JSON line per event.

Each run has its own file, `data/runs/<run_id>/audit.jsonl`. Lines are only ever added (open in
append mode, write one line, flush, fsync), never rewritten, so the file is a trustworthy
timeline even after a crash. Secrets are masked before anything is written.

Actors: `agent:<node>` (a graph node), `human:<name>` (a reviewer) or `system`.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.runtime.logging import known_secrets, redact
from app.runtime.runs import RunPaths, utc_now


class AuditEvent(BaseModel):
    ts: datetime = Field(default_factory=utc_now)
    run_id: str
    actor: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    outcome: str = "ok"


class AuditLog:
    """Writes and reads per-run audit files under `<data_dir>/runs/`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def path(self, run_id: str) -> Path:
        return RunPaths.for_run(self.data_dir, run_id).audit

    def append(self, event: AuditEvent) -> AuditEvent:
        safe = AuditEvent.model_validate(
            redact(event.model_dump(mode="json"), extra_secrets=known_secrets())
        )
        path = self.path(event.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = safe.model_dump_json() + "\n"
        # "a" never truncates; fsync makes sure the line survives a crash right after.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return safe

    def record(
        self,
        run_id: str,
        actor: str,
        action: str,
        outcome: str = "ok",
        **detail: Any,
    ) -> AuditEvent:
        """Shortcut: `audit.record(run_id, "agent:fill", "field_filled", key="email")`."""
        return self.append(
            AuditEvent(run_id=run_id, actor=actor, action=action, detail=detail, outcome=outcome)
        )

    def read(self, run_id: str) -> list[AuditEvent]:
        path = self.path(run_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(AuditEvent.model_validate(json.loads(line)))
        return events

    def count(self, run_id: str, action: str, **match: Any) -> int:
        """How many events have this action (and these detail values). Used by tests."""
        return sum(
            1
            for e in self.read(run_id)
            if e.action == action and all(e.detail.get(k) == v for k, v in match.items())
        )
