"""Wiring for form runs: builds the graph, its checkpointer and its live helpers in one place,
so the CLI, the demo script and the tests all start runs the same way."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2

from app.browser.graph import FormContext, build_graph, initial_state
from app.config import Settings
from app.runtime import graphs
from app.runtime.approval import Decision
from app.runtime.audit import AuditLog
from app.runtime.checkpoint import open_checkpointer
from app.runtime.llm import LLM, make_llm
from app.runtime.logging import bind_run, register_secrets
from app.runtime.notify import Notifier, make_notifier
from app.runtime.runs import RunIndex, RunStatus, new_run_id


@dataclass
class FormRunner:
    graph: Any
    context: FormContext

    async def start(
        self,
        target: str,
        profile: str,
        *,
        data: dict[str, Any] | None = None,
        mode: str = "interactive",
        run_id: str | None = None,
    ) -> tuple[str, graphs.Outcome]:
        run_id = run_id or new_run_id("form")
        self.context.index.create(run_id, title=target)
        with bind_run(run_id, module="browser"):
            outcome = await graphs.start(
                self.graph,
                initial_state(run_id, target, profile, data, mode),
                run_id,
                context=self.context,
            )
        self._mark_failed(run_id, outcome)
        return run_id, outcome

    async def resume(self, run_id: str, decision: Decision) -> graphs.Outcome:
        with bind_run(run_id, module="browser"):
            outcome = await graphs.resume(self.graph, run_id, decision, context=self.context)
        self._mark_failed(run_id, outcome)
        return outcome

    async def continue_run(self, run_id: str) -> graphs.Outcome:
        with bind_run(run_id, module="browser"):
            outcome = await graphs.continue_run(self.graph, run_id, context=self.context)
        self._mark_failed(run_id, outcome)
        return outcome

    def _mark_failed(self, run_id: str, outcome: graphs.Outcome) -> None:
        if outcome.status != "failed":
            return
        run = self.context.index.get(run_id)
        if run is not None and run.status is RunStatus.SUBMITTING:
            # It failed after the run was claimed for the click: the form may have gone
            # through. Never "failed" (someone might just retry and submit twice).
            self.context.index.set_status(run_id, RunStatus.NEEDS_HUMAN_CHECK)
            self.context.audit.record(
                run_id,
                "system",
                "run_failed",
                outcome="needs_human_check",
                error=outcome.error,
                note="failed after the submit was attempted; check the site before anything else",
            )
            return
        self.context.index.set_status(run_id, RunStatus.FAILED)
        self.context.audit.record(
            run_id, "system", "run_failed", outcome="failed", error=outcome.error
        )


@asynccontextmanager
async def open_form_runner(
    settings: Settings,
    *,
    llm: LLM | None = None,
    notifier: Notifier | None = None,
    durable: bool = True,
    **context_options: Any,
) -> AsyncIterator[FormRunner]:
    register_secrets(settings.secret_values())
    audit = AuditLog(settings.data_dir)
    async with (
        httpx2.AsyncClient(timeout=20) as client,
        open_checkpointer(settings, durable=durable) as saver,
    ):
        context = FormContext(
            settings=settings,
            audit=audit,
            index=RunIndex.in_data_dir(settings.data_dir),
            llm=llm or make_llm(settings, audit=audit),
            notifier=notifier or make_notifier(settings, client),
            **context_options,
        )
        try:
            yield FormRunner(build_graph(saver), context)
        finally:
            await context.browser.close()


def load_batch_rows(csv_path: str, data_columns: dict[str, str]) -> list[dict[str, Any]]:
    """One data dict per CSV row, with columns renamed to field keys by the site profile."""
    import csv

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        data = {}
        for column, value in row.items():
            if column is None or value in (None, ""):
                continue
            data[data_columns.get(column, column)] = value.strip()
        out.append(data)
    return out
