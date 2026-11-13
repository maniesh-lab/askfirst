"""Wiring for triage runs: the graph, its checkpointer and its live helpers in one place."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
import structlog

from app.config import Settings
from app.runtime import graphs
from app.runtime.approval import Decision, StaleDecision
from app.runtime.audit import AuditLog
from app.runtime.checkpoint import open_checkpointer
from app.runtime.llm import LLM, make_llm
from app.runtime.logging import bind_run, register_secrets
from app.runtime.notify import Notifier, make_notifier
from app.runtime.runs import RunIndex, RunStatus, new_run_id
from app.triage.actions import UNSURE
from app.triage.github import GitHubClient, IssuesAPI
from app.triage.graph import TriageContext, build_graph, initial_state
from app.triage.rubric import Rubric

log = structlog.get_logger(__name__)


@dataclass
class TriageRunner:
    graph: Any
    context: TriageContext

    async def start(
        self, repo: str, *, run_id: str | None = None, **options: Any
    ) -> tuple[str, graphs.Outcome]:
        run_id = run_id or new_run_id("triage")
        self.context.index.create(run_id, title=f"Triage {repo}")
        with bind_run(run_id, module="triage"):
            outcome = await graphs.start(
                self.graph, initial_state(run_id, repo, **options), run_id, context=self.context
            )
        self._finish(run_id, outcome)
        return run_id, outcome

    async def resume(self, run_id: str, decision: Decision) -> graphs.Outcome:
        """Act on a person's decision, after claiming the run.

        The claim is a compare-and-set (waiting -> submitting), taken before the graph is
        touched: of two people deciding at once, the second changes nothing, not even the
        checkpoint. Form runs do the same before their click.
        """
        index = self.context.index
        if not index.claim(run_id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING):
            self.context.audit.record(
                run_id,
                "system",
                "claim_lost",
                outcome="refused",
                reason="the run was not waiting (another decision?)",
            )
            return graphs.Outcome(
                "failed", error=f"ClaimLost: {run_id} is already being handled by another decision"
            )
        try:
            with bind_run(run_id, module="triage"):
                outcome = await graphs.resume(self.graph, run_id, decision, context=self.context)
        except StaleDecision, LookupError:
            index.claim(run_id, RunStatus.SUBMITTING, RunStatus.WAITING_REVIEW)  # hand it back
            raise
        self._finish(run_id, outcome)
        return outcome

    async def continue_run(self, run_id: str) -> graphs.Outcome:
        """Carry on a run that stopped mid-way. Only once its process has stopped: a run still
        applying looks the same as one that was killed while applying."""
        self.context.continuing = True
        try:
            with bind_run(run_id, module="triage"):
                outcome = await graphs.continue_run(self.graph, run_id, context=self.context)
        finally:
            self.context.continuing = False
        self._finish(run_id, outcome)
        return outcome

    def _finish(self, run_id: str, outcome: graphs.Outcome) -> None:
        index = self.context.index
        if outcome.status == "failed":
            index.set_status(run_id, RunStatus.FAILED)
            self.context.audit.record(
                run_id, "system", "run_failed", outcome="failed", error=outcome.error
            )
            return
        run = index.get(run_id)
        if run is None:
            return
        if outcome.status == "waiting":
            # Back at the gate (an edit, or a decision the gate refused): it waits again.
            index.claim(run_id, RunStatus.SUBMITTING, RunStatus.WAITING_REVIEW)
        elif run.status in (RunStatus.RUNNING, RunStatus.SUBMITTING, RunStatus.FAILED):
            # FAILED too: a run finished by `runs continue` is done, not failed.
            unsure = any(a.get("error") == UNSURE for a in outcome.values.get("actions", []))
            index.set_status(run_id, RunStatus.NEEDS_HUMAN_CHECK if unsure else RunStatus.DONE)


@asynccontextmanager
async def open_triage_runner(
    settings: Settings,
    *,
    github: IssuesAPI | None = None,
    llm: LLM | None = None,
    notifier: Notifier | None = None,
    read_only: bool = False,
    durable: bool = True,
    **context_options: Any,
) -> AsyncIterator[TriageRunner]:
    register_secrets(settings.secret_values())
    audit = AuditLog(settings.data_dir)
    async with (
        httpx2.AsyncClient(timeout=30) as client,
        open_checkpointer(settings, durable=durable) as saver,
    ):
        token = (
            settings.triage_github_token.get_secret_value()
            if settings.triage_github_token
            else None
        )
        if github is None and not token and not read_only:
            log.warning(
                "triage_read_only_no_token",
                note="TRIAGE_GITHUB_TOKEN is not set: nothing is written to GitHub",
            )
        context = TriageContext(
            settings=settings,
            audit=audit,
            index=RunIndex.in_data_dir(settings.data_dir),
            llm=llm or make_llm(settings, audit=audit),
            notifier=notifier or make_notifier(settings, client),
            github=github or GitHubClient(client, token, read_only=read_only or not token),
            rubric=Rubric.load(),
            **context_options,
        )
        yield TriageRunner(build_graph(saver), context)
