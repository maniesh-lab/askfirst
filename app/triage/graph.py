"""The triage run as a LangGraph graph.

    load_state -> fetch -> rules -> duplicates -> classify -> merge -> plan_actions
      -> policy_gate -> apply_auto -> [mark_waiting -> approval_gate -> apply_approved]
      -> build_digest -> post_digest -> write_state

`load_state` reads the window from the "Triage log (bot)" issue. `fetch` reads issues updated
in the window (and every open issue, for duplicate checks),
dropping pull requests and the bot's own log issue. `rules` runs the deterministic rubric rules,
`duplicates` finds candidates, `classify` asks the LLM (validated by Steadfast), and `merge`
combines them with the rules winning.

`policy_gate` runs the shared policy over each planned action. Unattended (cron), irreversible
actions are only proposals; interactive, they pause at the same approval gate the form agent
uses, and `review approve <triage run>` works for them too. `post_digest` posts once per digest
(its hash is kept in state), and `write_state` records the window for the next run.

Acting on a decision starts with a claim (compare-and-set on the run's status, as for form
submits), so two people approving at once can't both comment and close: the second one gets
`ClaimLost` and changes nothing. If the process dies while applying, `runs continue` takes the
run over and skips the actions the audit log shows as done. An action that started but never
finished is not retried (its write may have reached GitHub); the digest lists it for a person.

Live objects (GitHub client, LLM, notifier) live in the runtime context, never in state.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import Settings
from app.runtime.approval import (
    ApprovalRequired,
    Decision,
    Mode,
    ReviewPacket,
    Verdict,
    issue_token,
    make_approval_gate,
)
from app.runtime.audit import AuditLog
from app.runtime.hashing import stable_hash
from app.runtime.llm import LLM
from app.runtime.notify import Channel, Notifier, NotifyError
from app.runtime.runs import RunIndex, RunPaths, RunStatus, utc_now
from app.triage.actions import (
    NOT_APPLIED,
    as_proposed,
    decide,
    execute,
    pending,
    plan_actions,
    triage_policy,
)
from app.triage.classify import IssueClass, classify_all, merge
from app.triage.digest import (
    build_digest,
    digest_hash,
    headline,
    render_discord,
    render_json,
    render_markdown,
)
from app.triage.duplicates import candidates
from app.triage.github import GitHubError, IssuesAPI
from app.triage.models import Digest, DigestWindow, Issue, PlannedAction, RuleResult, TriageResult
from app.triage.rubric import Rubric, apply_rules
from app.triage.state import LOG_LABEL, RunState, last_state, save_state

log = structlog.get_logger(__name__)

DEFAULT_WINDOW_DAYS = 7


class TriageState(TypedDict, total=False):
    run_id: str
    repo: str
    mode: str  # interactive | unattended
    write_mode: str  # propose | apply-labels
    dry_run: bool
    read_only: bool
    force: bool
    since: str | None
    started_at: str
    window_start: str
    window_end: str
    window_from: str  # since | state | default
    last_run_end: str | None
    issues: list[dict[str, Any]]
    open_issues: list[dict[str, Any]]
    rules: dict[str, dict[str, Any]]
    candidates: dict[str, list[list[Any]]]
    llm: dict[str, dict[str, Any]]
    unclassified: list[int]
    results: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    log_issue: int | None
    repo_private: bool
    last_window_start: str | None
    last_digest_hash: str | None
    last_touched: dict[str, str]
    touched: dict[str, str]
    plan_hash: str
    waiting_marked_for: str
    decision: dict[str, Any] | None
    gate_error: str | None
    digest: dict[str, Any]
    digest_hash: str
    posted: bool
    post_failed: bool
    skipped_post: bool
    outcome: str


class ClaimLost(Exception):
    """Another process is already acting on this run's decision."""


# An action a crashed try started but never recorded: its write may or may not have landed.
UNSURE = "may have been applied before a crash: check the issue by hand"


@dataclass
class TriageContext:
    settings: Settings
    audit: AuditLog
    index: RunIndex
    llm: LLM
    notifier: Notifier
    github: IssuesAPI
    rubric: Rubric
    now: Callable[[], datetime] = utc_now
    actions_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # True only inside `runs continue`: the one path allowed to take over a run whose apply
    # step crashed (see _claim).
    continuing: bool = False


Ctx = Runtime[TriageContext]


def audited(name: str) -> Callable:
    def wrap(fn: Callable) -> Callable:
        async def node(state: TriageState, runtime: Ctx) -> dict[str, Any]:
            ctx = runtime.context
            ctx.audit.record(state["run_id"], f"agent:{name}", "node_start", node=name)
            out = await fn(state, ctx)
            ctx.audit.record(state["run_id"], f"agent:{name}", "node_end", node=name)
            return out

        node.__name__ = name
        return node

    return wrap


def parse_since(text: str | None, now: datetime) -> datetime | None:
    """`7d`, `36h` or an ISO date/time."""
    if not text:
        return None
    if text[-1] in "dh" and text[:-1].isdigit():
        amount = int(text[:-1])
        return now - (timedelta(days=amount) if text[-1] == "d" else timedelta(hours=amount))
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=now.tzinfo)


def issues_of(state: TriageState, key: str = "issues") -> list[Issue]:
    return [Issue.model_validate(i) for i in state.get(key, [])]


def stateful(state: TriageState, ctx: TriageContext) -> bool:
    """Read-only targets (and dry runs) keep no state in the repo."""
    return not (state.get("read_only") or state.get("dry_run") or ctx.github.read_only)


@audited("load_state")
async def load_state(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    now = ctx.now()
    log_number, last = (None, None)
    if stateful(state, ctx) or not ctx.github.read_only:
        try:
            log_number, last = await last_state(ctx.github, state["repo"])
        except GitHubError as exc:
            log.warning("triage_state_unreadable", error=str(exc))
    try:
        repo_private = bool((await ctx.github.get_repo(state["repo"])).get("private", True))
    except GitHubError as exc:
        repo_private = True  # unknown: treat it as private (see `repo_private` in the CLI)
        log.warning("triage_repo_visibility_unknown", error=str(exc))
    start = parse_since(state.get("since"), now)
    window_from = "since"
    if start is None and last is not None and stateful(state, ctx):
        start, window_from = datetime.fromisoformat(last.window_end), "state"
    if start is None:
        start, window_from = now - timedelta(days=DEFAULT_WINDOW_DAYS), "default"
    return {
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "window_from": window_from,
        "last_run_end": last.window_end if last else None,
        "last_window_start": last.window_start if last else None,
        "last_digest_hash": last.digest_hash if last else None,
        "last_touched": last.touched if last and window_from == "state" else {},
        "log_issue": log_number,
        "repo_private": repo_private,
    }


def went_stale(open_issues: list[Issue], state: TriageState, stale_days: int) -> list[Issue]:
    """Open issues that crossed the stale line (no update for `stale_days`) in this window."""
    age = timedelta(days=stale_days)
    newest = datetime.fromisoformat(state["window_end"]) - age
    oldest = (
        None
        if state.get("window_from") == "default"
        else datetime.fromisoformat(state["window_start"]) - age
    )
    return [
        i
        for i in open_issues
        if i.updated_at <= newest and (oldest is None or i.updated_at > oldest)
    ]


@audited("fetch")
async def fetch(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    start = datetime.fromisoformat(state["window_start"])
    raw = await ctx.github.list_issues(state["repo"], since=start)
    open_raw = await ctx.github.list_issues(state["repo"], state="open")

    def keep(i: dict[str, Any]) -> bool:
        names = [lab["name"] if isinstance(lab, dict) else lab for lab in i.get("labels", [])]
        return LOG_LABEL not in names

    issues = [Issue.from_github(i) for i in raw if keep(i)]
    open_issues = [Issue.from_github(i) for i in open_raw if keep(i)]
    # A stale issue has, by definition, not been updated in the window, so the `since` fetch
    # never returns it. Add the open issues that went stale during this window. With no window
    # to compare against (first run, read-only target), that is every stale open issue, once.
    gone_stale = went_stale(open_issues, state, ctx.rubric.stale_after_days)
    seen = {i.number for i in issues}
    issues += [i for i in gone_stale if i.number not in seen]
    ctx.audit.record(
        state["run_id"],
        "agent:fetch",
        "issues_fetched",
        repo=state["repo"],
        in_window=len(issues),
        open=len(open_issues),
        gone_stale=len(gone_stale),
    )
    return {
        "issues": [i.model_dump(mode="json") for i in issues if i.state == "open"],
        "open_issues": [i.model_dump(mode="json") for i in open_issues],
    }


@audited("rules")
async def rules(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    now = ctx.now()
    last = datetime.fromisoformat(state["last_run_end"]) if state.get("last_run_end") else None
    out = {}
    touched = state.get("last_touched") or {}
    for issue in issues_of(state):
        bot_at = touched.get(str(issue.number))
        result = apply_rules(
            issue,
            ctx.rubric,
            now,
            last_run=last,
            bot_touched_at=datetime.fromisoformat(bot_at) if bot_at else None,
        )
        out[str(issue.number)] = result.model_dump()
        if result.escalate:
            ctx.audit.record(
                state["run_id"],
                "agent:rules",
                "escalated",
                issue=issue.number,
                reasons=result.reasons,
            )
    return {"rules": out}


@audited("duplicates")
async def duplicates(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    others = issues_of(state, "open_issues")
    return {
        "candidates": {
            str(i.number): [list(c) for c in candidates(i, others)] for i in issues_of(state)
        }
    }


@audited("classify")
async def classify(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    todo = [i for i in issues_of(state) if not state["rules"][str(i.number)]["skip"]]
    cands = {int(k): [(int(n), float(s)) for n, s in v] for k, v in state["candidates"].items()}
    results, failed = await classify_all(ctx.llm, todo, cands, ctx.rubric, run_id=state["run_id"])
    return {
        "llm": {str(k): v.model_dump() for k, v in results.items()},
        "unclassified": failed,
    }


@audited("merge")
async def merge_results(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    out = []
    for issue in issues_of(state):
        llm_raw = state.get("llm", {}).get(str(issue.number))
        result = merge(
            issue,
            RuleResult.model_validate(state["rules"][str(issue.number)]),
            IssueClass.model_validate(llm_raw) if llm_raw else None,
            ctx.rubric,
            audit=ctx.audit,
            run_id=state["run_id"],
        )
        out.append(result.model_dump(mode="json"))
    return {"results": out}


@audited("plan_actions")
async def plan(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    results = [TriageResult.model_validate(r) for r in state["results"]]
    labels = {i.number: i.labels for i in issues_of(state)}
    actions = plan_actions(results, labels)
    return {"actions": [a.model_dump() for a in actions]}


@audited("policy_gate")
async def policy_gate(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    decide(
        actions,
        triage_policy(state.get("write_mode", "propose")),
        Mode(state.get("mode", "unattended")),
    )
    for a in actions:
        ctx.audit.record(
            state["run_id"],
            "agent:policy_gate",
            "policy_verdict",
            action_id=a.id,
            kind=a.kind,
            verdict=a.verdict,
        )
    waiting = pending(actions)
    return {
        "actions": [a.model_dump() for a in actions],
        "plan_hash": stable_hash(
            [a.model_dump(include={"id", "kind", "issue", "params"}) for a in waiting]
        ),
    }


async def _run_actions(
    state: TriageState, ctx: TriageContext, chosen: list[PlannedAction], *, token: Any = None
) -> dict[str, str]:
    """Run the chosen actions. Returns the touched issues' `updated_at` after the writes."""
    # A dry run, --read-only, or a client with no token: nothing may be written.
    may_write = stateful(state, ctx)
    if may_write:
        await _ensure_labels(state, ctx, chosen)
    for a in chosen:
        if a.applied or a.error == UNSURE:
            continue  # done (or maybe done) by an earlier try that crashed
        if not may_write:
            a.error = NOT_APPLIED
            continue
        # Written before the GitHub call, so a crash in between leaves a trace (see UNSURE).
        ctx.audit.record(
            state["run_id"], "agent:apply", "action_started", action_id=a.id, issue=a.issue
        )
        try:
            await execute(
                a, ctx.github, state["repo"], token=token, plan_hash=state.get("plan_hash", "")
            )
            a.applied = True
        except (GitHubError, ApprovalRequired) as exc:
            a.error = str(exc)[:200]
        ctx.audit.record(
            state["run_id"],
            "agent:apply",
            "action_applied",
            outcome="ok" if a.applied else "failed",
            action_id=a.id,
            kind=a.kind,
            issue=a.issue,
            error=a.error,
        )
    return await _touched_times(state, ctx, {a.issue for a in chosen if a.applied})


async def _ensure_labels(
    state: TriageState, ctx: TriageContext, chosen: list[PlannedAction]
) -> None:
    """Create missing labels first, with the rubric's colours. Otherwise GitHub makes them
    itself, all grey and with no description."""
    names = sorted({lab for a in chosen if a.kind == "add_labels" for lab in a.params["labels"]})
    if not names:
        return
    colors = ctx.rubric.label_colors
    wanted = {
        name: (colors.get(name, "ededed"), "Added by the askfirst triage bot") for name in names
    }
    try:
        created = await ctx.github.ensure_labels(state["repo"], wanted)
    except GitHubError as exc:
        # Not fatal: adding the label still works, it is just grey.
        log.warning("triage_labels_not_created", error=str(exc))
        return
    if created:
        ctx.audit.record(state["run_id"], "agent:apply", "labels_created", labels=created)


async def _touched_times(
    state: TriageState, ctx: TriageContext, numbers: set[int]
) -> dict[str, str]:
    """Read back each changed issue's `updated_at` (GitHub's clock, not ours)."""
    touched = dict(state.get("touched") or {})
    for number in sorted(numbers):
        try:
            raw = await ctx.github.get_issue(state["repo"], number)
        except GitHubError as exc:
            log.warning("triage_touch_unreadable", issue=number, error=str(exc))
            continue
        touched[str(number)] = str(raw["updated_at"]).replace("Z", "+00:00")
    return touched


@audited("apply_auto")
async def apply_auto(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    touched = await _run_actions(
        state, ctx, [a for a in actions if a.verdict == Verdict.AUTO and not a.applied]
    )
    return {"actions": [a.model_dump() for a in actions], "touched": touched}


def after_auto(state: TriageState) -> str:
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    return "mark_waiting" if pending(actions) else "build_digest"


def build_packet(state: dict[str, Any]) -> ReviewPacket:
    actions = pending([PlannedAction.model_validate(a) for a in state["actions"]])
    return ReviewPacket(
        run_id=state["run_id"],
        kind="triage",
        title=f"Triage proposals for {state['repo']}",
        actions=[as_proposed(a) for a in actions],
        details={
            "repo": state["repo"],
            "count": len(actions),
            "actions": [a.model_dump(include={"id", "kind", "issue", "summary"}) for a in actions],
        },
        plan_hash=state["plan_hash"],
        created_at=state.get("started_at"),
    )


@audited("mark_waiting")
async def mark_waiting(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    if state.get("waiting_marked_for") != state["plan_hash"]:
        packet = build_packet(state)
        ctx.index.set_status(state["run_id"], RunStatus.WAITING_REVIEW, title=packet.title)
        ctx.audit.record(
            state["run_id"], "agent:mark_waiting", "waiting_review", proposals=len(packet.actions)
        )
        try:
            await ctx.notifier.send(
                Channel.REVIEW,
                f"Triage proposals waiting: `{state['run_id']}` ({len(packet.actions)} actions on "
                f"{state['repo']}). Run `python -m app.cli review show {state['run_id']}`.",
            )
        except Exception as exc:  # noqa: BLE001 - a failed ping must not lose the run
            log.warning("review_ping_failed", error=str(exc))
    return {"waiting_marked_for": state["plan_hash"]}


def after_gate(state: TriageState) -> str:
    decision = state.get("decision")
    if decision is None:
        return "approval_gate"
    return {"approve": "apply_approved", "reject": "record_reject", "edit": "apply_edit"}[
        decision["choice"]
    ]


def _claim(state: TriageState, ctx: TriageContext) -> None:
    """Make this process the only one acting on the decision, or raise `ClaimLost`.

    Normal path: the run must still be waiting (compare-and-set to SUBMITTING). Two approvals
    racing: exactly one wins. `runs continue` may also take over a run whose apply step died:
    still SUBMITTING (the process was killed) or FAILED (it raised).
    """
    run_id, index = state["run_id"], ctx.index
    if index.claim(run_id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING):
        return
    if ctx.continuing:
        run = index.get(run_id)
        killed = run is not None and run.status is RunStatus.SUBMITTING
        if killed or index.claim(run_id, RunStatus.FAILED, RunStatus.SUBMITTING):
            ctx.audit.record(run_id, "agent:claim", "claim_taken_over", after="crash")
            return
    ctx.audit.record(
        run_id,
        "agent:claim",
        "claim_lost",
        outcome="refused",
        reason="the run was not waiting (another approval?)",
    )
    raise ClaimLost(f"{run_id} is already being handled by another approval")


def _earlier_attempts(ctx: TriageContext, run_id: str) -> tuple[set[str], set[str]]:
    """From the audit log: actions an earlier, crashed try applied, and ones it started but
    never finished."""
    started: set[str] = set()
    finished: set[str] = set()
    applied: set[str] = set()
    for e in ctx.audit.read(run_id):
        action_id = e.detail.get("action_id")
        if e.action == "action_started":
            started.add(action_id)
        elif e.action == "action_applied":
            finished.add(action_id)
            if e.outcome == "ok":
                applied.add(action_id)
    return applied, started - finished


def _log_decision(state: TriageState, ctx: TriageContext) -> Decision:
    decision = Decision.model_validate(state["decision"])
    if ctx.continuing:
        return decision  # logged by the try that crashed; once is the truth
    ctx.audit.record(
        state["run_id"],
        f"human:{decision.reviewer}",
        "decision",
        choice=decision.choice,
        reason=decision.reason,
        plan_hash=decision.plan_hash,
    )
    return decision


@audited("apply_approved")
async def apply_approved(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    _claim(state, ctx)
    decision = _log_decision(state, ctx)
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    chosen = pending(actions)
    if ctx.continuing:
        applied, unsure = _earlier_attempts(ctx, state["run_id"])
        for a in chosen:
            if a.id in applied:
                a.applied = True
            elif a.id in unsure:
                a.error = UNSURE
                ctx.audit.record(
                    state["run_id"],
                    "agent:apply",
                    "action_unsure",
                    outcome="needs_human_check",
                    action_id=a.id,
                    issue=a.issue,
                )
    token = issue_token(decision, state["plan_hash"], state["run_id"])
    touched = await _run_actions(state, ctx, chosen, token=token)
    ctx.index.set_status(state["run_id"], RunStatus.RUNNING)
    return {"actions": [a.model_dump() for a in actions], "touched": touched}


@audited("record_reject")
async def record_reject(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    _claim(state, ctx)  # only one digest and one state comment per run
    _log_decision(state, ctx)
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    for a in pending(actions):
        a.verdict = "rejected"
    ctx.index.set_status(state["run_id"], RunStatus.RUNNING)
    return {"actions": [a.model_dump() for a in actions]}


@audited("apply_edit")
async def apply_edit(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    """Edits for triage: `drop=<id>,<id>` removes those proposals; the rest go back to the gate."""
    decision = _log_decision(state, ctx)
    drop = {x.strip() for x in str(decision.edits.get("drop", "")).split(",") if x.strip()}
    actions = [PlannedAction.model_validate(a) for a in state["actions"]]
    for a in actions:
        if a.id in drop:
            a.verdict = "dropped"
    waiting = pending(actions)
    if not waiting:
        # Everything was dropped: the run goes on to the digest, so it needs the claim too.
        _claim(state, ctx)
        ctx.index.set_status(state["run_id"], RunStatus.RUNNING)
    return {
        "actions": [a.model_dump() for a in actions],
        "plan_hash": stable_hash(
            [a.model_dump(include={"id", "kind", "issue", "params"}) for a in waiting]
        ),
        "decision": None,
    }


def after_edit(state: TriageState) -> str:
    return after_auto(state)


@audited("build_digest")
async def make_digest(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    started = datetime.fromisoformat(state["started_at"])
    digest = build_digest(
        state["repo"],
        DigestWindow(
            start=datetime.fromisoformat(state["window_start"]),
            end=datetime.fromisoformat(state["window_end"]),
        ),
        state["run_id"],
        [TriageResult.model_validate(r) for r in state["results"]],
        [
            PlannedAction.model_validate(a)
            for a in state["actions"]
            if a["verdict"] not in ("dropped",)
        ],
        mode=state.get("write_mode", "propose"),
        tokens=ctx.llm.usage.total_tokens,
        duration_s=(utc_now() - started).total_seconds(),
        actions_url=ctx.actions_url,
    )
    paths = RunPaths.for_run(ctx.settings.data_dir, state["run_id"]).ensure()
    (paths.root / "digest.md").write_text(render_markdown(digest), encoding="utf-8")
    (paths.root / "digest.json").write_text(
        json.dumps(render_json(digest), indent=2), encoding="utf-8"
    )
    return {"digest": digest.model_dump(mode="json"), "digest_hash": digest_hash(digest)}


@audited("post_digest")
async def post_digest(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    if state.get("posted"):
        return {}
    same = (
        state.get("last_window_start") == state["window_start"]
        and state.get("last_digest_hash") == state["digest_hash"]
    )
    if same and not state.get("force"):
        ctx.audit.record(
            state["run_id"], "agent:post_digest", "digest_skipped", reason="already posted"
        )
        return {"skipped_post": True}
    if state.get("dry_run"):
        return {"skipped_post": True}
    digest = Digest.model_validate(state["digest"])
    messages = render_discord(digest)
    sent, errors = 0, []
    for n, embeds in enumerate(messages):
        try:
            delivered = await ctx.notifier.send(
                Channel.DIGEST, "" if n else headline(digest)[:2000], embeds=embeds
            )
            # False means the digest channel has no webhook (only the review one is set), so
            # nothing reached Discord. Counting it as posted would save the window anyway.
            if delivered:
                sent += 1
            else:
                errors.append("no webhook for the digest channel")
        except NotifyError as exc:
            # One bad message must not lose the rest (or the run's state).
            errors.append(str(exc)[:200])
            log.warning("digest_message_failed", part=n + 1, error=str(exc))
    if sent == 0:
        ctx.audit.record(
            state["run_id"],
            "agent:post_digest",
            "digest_post_failed",
            outcome="failed",
            errors=errors,
        )
        # Not marked as posted, so no state is written and the next run posts it again.
        return {"post_failed": True}
    ctx.audit.record(
        state["run_id"],
        "agent:post_digest",
        "digest_posted",
        messages=sent,
        failed_parts=len(errors),
    )
    return {"posted": True}


@audited("write_state")
async def write_state(state: TriageState, ctx: TriageContext) -> dict[str, Any]:
    if state.get("post_failed"):
        return {"outcome": "post_failed"}
    if stateful(state, ctx) and not state.get("skipped_post"):
        digest = Digest.model_validate(state["digest"])
        summary = f"Run `{state['run_id']}`: {headline(digest)}"
        number = await save_state(
            ctx.github,
            state["repo"],
            state.get("log_issue"),
            summary,
            RunState(
                run_id=state["run_id"],
                window_start=state["window_start"],
                window_end=state["window_end"],
                digest_hash=state["digest_hash"],
                touched=state.get("touched") or {},
            ),
        )
        return {"log_issue": number, "outcome": "done"}
    return {"outcome": "done"}


def add_part_one(g: StateGraph) -> None:
    for name, fn in [
        ("load_state", load_state),
        ("fetch", fetch),
        ("rules", rules),
        ("duplicates", duplicates),
        ("classify", classify),
        ("merge", merge_results),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "load_state")
    g.add_edge("load_state", "fetch")
    g.add_edge("fetch", "rules")
    g.add_edge("rules", "duplicates")
    g.add_edge("duplicates", "classify")
    g.add_edge("classify", "merge")


def build_graph(checkpointer: Any) -> Any:
    g = StateGraph(TriageState, context_schema=TriageContext)
    add_part_one(g)
    for name, fn in [
        ("plan_actions", plan),
        ("policy_gate", policy_gate),
        ("apply_auto", apply_auto),
        ("mark_waiting", mark_waiting),
        ("apply_approved", apply_approved),
        ("record_reject", record_reject),
        ("apply_edit", apply_edit),
        ("build_digest", make_digest),
        ("post_digest", post_digest),
        ("write_state", write_state),
    ]:
        g.add_node(name, fn)
    g.add_node("approval_gate", make_approval_gate(build_packet))
    g.add_edge("merge", "plan_actions")
    g.add_edge("plan_actions", "policy_gate")
    g.add_edge("policy_gate", "apply_auto")
    g.add_conditional_edges("apply_auto", after_auto, ["mark_waiting", "build_digest"])
    g.add_edge("mark_waiting", "approval_gate")
    g.add_conditional_edges(
        "approval_gate",
        after_gate,
        ["approval_gate", "apply_approved", "record_reject", "apply_edit"],
    )
    g.add_edge("apply_approved", "build_digest")
    g.add_edge("record_reject", "build_digest")
    g.add_conditional_edges("apply_edit", after_edit, ["mark_waiting", "build_digest"])
    g.add_edge("build_digest", "post_digest")
    g.add_edge("post_digest", "write_state")
    g.add_edge("write_state", END)
    return g.compile(checkpointer=checkpointer)


def initial_state(
    run_id: str,
    repo: str,
    *,
    since: str | None = None,
    mode: str = "unattended",
    write_mode: str = "propose",
    dry_run: bool = False,
    read_only: bool = False,
    force: bool = False,
) -> TriageState:
    return TriageState(
        run_id=run_id,
        repo=repo,
        since=since,
        mode=mode,
        write_mode=write_mode,
        dry_run=dry_run,
        read_only=read_only,
        force=force,
        started_at=utc_now().isoformat(),
    )
