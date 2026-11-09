"""The form run as a LangGraph graph: fill, stop for a person, then (after approval) replay
and submit exactly once.

    open -> [login] -> detect -> map -> draft -> fill -> (next_step -> detect ...)
         -> verify -> policy -> package_review -> mark_waiting -> approval_gate
    approve -> prepare_submit -> mark_submit_attempted -> submit -> confirm
    edit    -> apply_edit -> package_review -> mark_waiting -> approval_gate (new plan hash)
    reject  -> record_reject

Why it is shaped like this:
- A browser can't be saved in a checkpoint, and a pause can last hours or survive a restart. So
  after approval, `prepare_submit` opens a **fresh** browser, refills the page from the approved
  plan, checks every step's schema hash (the page hasn't changed) and every value, and only then
  submits. A changed page goes back through the fill steps and to the reviewer again.
- **At most once.** `mark_submit_attempted` is checkpointed before the click. If the process dies
  after it, the resumed `submit` node finds no live, freshly replayed page and marks the run
  `needs_human_check` instead of clicking again.
- `mark_waiting` runs before the gate and does its side effects once per plan hash; the gate is
  pure (LangGraph re-runs it from the top on resume); the decision is audited after the gate.
- Live objects (browser, LLM, notifier) live in the runtime context, never in state.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from playwright.async_api import Page

from app.browser import draft as drafting
from app.browser.detect import detect, resolve
from app.browser.fill import (
    FillPlan,
    apply_edits,
    fill_step,
    final_button_for,
    plan_steps,
    verify_all,
)
from app.browser.login import login, on_login_page, storage_path
from app.browser.mapping import Profile, map_step
from app.browser.review import build_packet, ping_text, write_review_files
from app.browser.schema import Field, FormStep, dedupe_keys
from app.browser.session import BrowserSession, FixtureServer, settle
from app.browser.sites import SITES_DIR, Target, parse_target
from app.browser.submit import SubmitResult, check_success, submit
from app.config import Settings
from app.runtime.approval import (
    Decision,
    Mode,
    Policy,
    ProposedAction,
    Risk,
    Verdict,
    issue_token,
    make_approval_gate,
)
from app.runtime.audit import AuditLog
from app.runtime.llm import LLM
from app.runtime.notify import Channel, Notifier
from app.runtime.runs import RunIndex, RunPaths, RunStatus, utc_now

log = structlog.get_logger(__name__)

MAX_STEPS = 10
MAX_REPLANS = 2

# What a form run may do, and how risky each action is.
FORM_POLICY = Policy(
    {
        "fill_field": Risk.REVERSIBLE,
        "click_next": Risk.REVERSIBLE,
        "save_draft": Risk.REVERSIBLE,
        "submit_form": Risk.IRREVERSIBLE,
    },
    auto_reversible={"fill_field", "click_next"},
)


class FormState(TypedDict, total=False):
    run_id: str
    target: str
    profile_path: str
    data: dict[str, Any]
    mode: str
    started_at: str
    step_index: int
    schema: list[dict[str, Any]]
    mappings: dict[str, list[dict[str, Any]]]
    drafts: dict[str, str]
    draft_failed: list[str]
    fill_failed: list[str]
    plan: dict[str, Any] | None
    page_title: str
    screenshots: list[str]
    warnings: list[str]
    replans: int
    verdict: str
    packet_time: str
    waiting_marked_for: str
    decision: dict[str, Any] | None
    gate_error: str | None
    submit_attempted: bool
    claim_lost: bool
    page_changed: bool
    result: dict[str, Any] | None
    outcome: str


class BrowserLost(Exception):
    """The live browser for this run is gone (the process restarted before review)."""


@dataclass
class BrowserHolder:
    """The run's live browser. Not in state: it can't be checkpointed."""

    session: BrowserSession | None = None
    page: Page | None = None
    # Set by prepare_submit to the plan hash it replayed, so submit knows the page is fresh.
    ready_for: str | None = None

    async def open(
        self, ctx: FormContext, run_id: str, storage: Path | None
    ) -> tuple[BrowserSession, Page]:
        await self.close()
        session = BrowserSession(
            ctx.settings,
            run_id=run_id,
            audit=ctx.audit,
            extra_hosts=ctx.extra_hosts,
            fixture_server=ctx.fixture_server,
            headless=ctx.headless,
            storage_state=str(storage) if storage and storage.is_file() else None,
            record_video_dir=ctx.video_dir,
        )
        await session.__aenter__()
        # Stored before new_page(), so close() can still shut it down if that fails.
        self.session = session
        self.page = await session.new_page()
        return session, self.page

    def need(self) -> tuple[BrowserSession, Page]:
        if self.session is None or self.page is None:
            raise BrowserLost("the browser for this run is gone; start a new run")
        return self.session, self.page

    async def close(self) -> None:
        if self.session is not None:
            await self.session.__aexit__(None, None, None)
        self.session = self.page = None
        self.ready_for = None


@dataclass
class FormContext:
    settings: Settings
    audit: AuditLog
    index: RunIndex
    llm: LLM
    notifier: Notifier
    browser: BrowserHolder = field(default_factory=BrowserHolder)
    # Tests only (the fake fixture origin). Real runs use ALLOWED_HOSTS alone.
    extra_hosts: set[str] = field(default_factory=set)
    fixture_server: FixtureServer | None = None
    headless: bool | None = None
    keep_browser_open: bool = False
    video_dir: str | None = None
    sites_dir: Path = SITES_DIR
    # Tests only: simulate a crash right after the submit click.
    crash_after_click: bool = False
    _profiles: dict[str, Profile] = field(default_factory=dict)

    def profile(self, path: str) -> Profile:
        if path not in self._profiles:
            self._profiles[path] = Profile.load(path)
        return self._profiles[path]

    def target(self, text: str) -> Target:
        return parse_target(text, self.settings, self.sites_dir)


class SimulatedCrash(Exception):
    """Raised by the test hook right after the submit click."""


Ctx = Runtime[FormContext]


def audited(name: str) -> Callable:
    """Wrap a node so its start and end are audit events (the crash tests count these)."""

    def wrap(fn: Callable) -> Callable:
        async def node(state: FormState, runtime: Ctx) -> dict[str, Any]:
            ctx = runtime.context
            ctx.audit.record(state["run_id"], f"agent:{name}", "node_start", node=name)
            out = await fn(state, ctx)
            ctx.audit.record(state["run_id"], f"agent:{name}", "node_end", node=name)
            return out

        node.__name__ = name
        return node

    return wrap


def _paths(ctx: FormContext, state: FormState) -> RunPaths:
    return RunPaths.for_run(ctx.settings.data_dir, state["run_id"]).ensure()


def _current_step(state: FormState) -> FormStep:
    return FormStep.model_validate(state["schema"][-1])


# Fill phase ---------------------------------------------------------------------------


@audited("open")
async def open_page(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    session, page = await ctx.browser.open(ctx, state["run_id"], storage_path(ctx.settings, target))
    await session.goto(page, target.url)
    session.guard.stage = "fill"
    return {
        "step_index": 0,
        "schema": [],
        "mappings": {},
        "drafts": {},
        "draft_failed": [],
        "fill_failed": [],
        "plan": None,
        "screenshots": [],
        "decision": None,
        "gate_error": None,
    }


def needs_login(state: FormState, ctx: FormContext) -> bool:
    target = ctx.target(state["target"])
    _, page = ctx.browser.need()
    return bool(target.form and target.form.login and on_login_page(page, target))


@audited("login")
async def do_login(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    session, page = ctx.browser.need()
    await login(session, page, target, ctx.settings, run_id=state["run_id"], audit=ctx.audit)
    await session.goto(page, target.url)
    return {}


@audited("detect")
async def detect_step(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    _, page = ctx.browser.need()
    step = await detect(page, step_index=state.get("step_index", 0))
    dedupe_keys(step, [FormStep.model_validate(s) for s in state.get("schema", [])])
    out: dict[str, Any] = {"schema": [*state.get("schema", []), step.model_dump(mode="json")]}
    if not state.get("schema"):
        out["page_title"] = step.title
    ctx.audit.record(
        state["run_id"],
        "agent:detect",
        "step_detected",
        step=step.index,
        fields=len(step.fields),
        fillable=len(step.fillable_fields()),
        schema_hash=step.schema_hash,
        target=target.label,
    )
    return out


@audited("map")
async def map_fields(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    step = _current_step(state)
    overrides = target.form.overrides if target.form else {}
    mappings = map_step(
        step, ctx.profile(state["profile_path"]), overrides=overrides, data=state.get("data") or {}
    )
    counts: dict[str, int] = {}
    for m in mappings:
        counts[m.status] = counts.get(m.status, 0) + 1
    ctx.audit.record(state["run_id"], "agent:map", "fields_mapped", step=step.index, **counts)
    return {
        "mappings": {
            **state.get("mappings", {}),
            str(step.index): [m.model_dump(mode="json") for m in mappings],
        }
    }


@audited("draft")
async def draft_fields(state: FormState, ctx: FormContext) -> dict[str, Any]:
    step = _current_step(state)
    wanted = {m["key"] for m in state["mappings"][str(step.index)] if m["status"] == "needs_draft"}
    fields = [f for f in step.fields if f.key in wanted]
    answers, failed, error = await drafting.draft_answers(
        ctx.llm,
        fields,
        ctx.profile(state["profile_path"]),
        title=step.title,
        headings=step.headings,
        context=step.context,
        run_id=state["run_id"],
    )
    warnings = list(state.get("warnings", []))
    if error:
        warnings.append(f"step {step.index + 1}: drafting failed ({error}); those fields are empty")
    if fields:
        ctx.audit.record(
            state["run_id"],
            "agent:draft",
            "drafted",
            step=step.index,
            keys=sorted(answers),
            failed=failed,
        )
    return {
        "drafts": {**state.get("drafts", {}), **answers},
        "draft_failed": [*state.get("draft_failed", []), *failed],
        "warnings": warnings,
    }


async def _screenshot(page: Page, paths: RunPaths, name: str) -> str:
    rel = f"artifacts/{name}.png"
    await page.screenshot(path=str(paths.root / rel), full_page=True)
    return rel


@audited("fill")
async def fill_fields(state: FormState, ctx: FormContext) -> dict[str, Any]:
    step = _current_step(state)
    _, page = ctx.browser.need()
    from app.browser.mapping import Mapping

    mappings = [Mapping.model_validate(m) for m in state["mappings"][str(step.index)]]
    steps = plan_steps(step, mappings, state.get("drafts", {}))
    results = await fill_step(page, steps)
    failed = [r.key for r in results if not r.ok]
    for r in results:
        ctx.audit.record(
            state["run_id"],
            "agent:fill",
            "field_filled",
            outcome="ok" if r.ok else "failed",
            key=r.key,
            attempts=r.attempts,
            error=r.error,
        )
    shot = await _screenshot(page, _paths(ctx, state), f"step-{step.index + 1}")
    plan = FillPlan.model_validate(state["plan"]) if state.get("plan") else FillPlan(url=step.url)
    plan.steps = [s for s in plan.steps if s.step_index != step.index] + steps
    plan.schema_hashes = [*plan.schema_hashes[: step.index], step.schema_hash]
    return {
        "plan": plan.model_dump(mode="json"),
        "fill_failed": [*state.get("fill_failed", []), *failed],
        "screenshots": [*state.get("screenshots", []), shot],
    }


def after_fill(state: FormState) -> str:
    step = _current_step(state)
    if step.buttons_of("submit"):
        return "verify"
    if step.buttons_of("next") and step.index + 1 < MAX_STEPS:
        return "next_step"
    return "verify"


@audited("next_step")
async def next_step(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    step = _current_step(state)
    _, page = ctx.browser.need()
    names = target.form.steps if target.form else []
    buttons = step.buttons_of("next")
    button = next((b for b in buttons if b.text in names), buttons[0])
    await resolve(page, button.locator).click()
    await settle(page, 250)
    plan = FillPlan.model_validate(state["plan"])
    plan.next_buttons = [*plan.next_buttons[: step.index], button.text]
    ctx.audit.record(
        state["run_id"], "agent:next_step", "clicked_next", step=step.index, button=button.text
    )
    return {"step_index": step.index + 1, "plan": plan.model_dump(mode="json")}


@audited("verify")
async def verify(state: FormState, ctx: FormContext) -> dict[str, Any]:
    target = ctx.target(state["target"])
    step = _current_step(state)
    _, page = ctx.browser.need()
    plan = FillPlan.model_validate(state["plan"])
    warnings = list(state.get("warnings", []))
    bad = await verify_all(page, plan.for_step(step.index))
    if bad:
        warnings.append(f"values changed after filling: {', '.join(bad)}")
    wanted = target.form.submit_button if target.form else None
    plan.final_button = final_button_for(step, wanted)
    if plan.final_button is None:
        warnings.append("no single final submit button found; submit is not possible")
    elif wanted and plan.final_button.text != wanted:
        warnings.append(
            f"final button is {plan.final_button.text!r}, site profile expects {wanted!r}"
        )
    if state.get("fill_failed"):
        warnings.append(f"could not fill: {', '.join(state['fill_failed'])}")
    plan.profile_version = ctx.profile(state["profile_path"]).version_hash()
    plan.plan_hash = plan.compute_hash()
    return {"plan": plan.model_dump(mode="json"), "warnings": list(dict.fromkeys(warnings))}


def after_verify(state: FormState) -> str:
    # No single final button means nothing could ever be submitted: don't ask for approval.
    return "policy" if (state.get("plan") or {}).get("final_button") else "give_up"


@audited("policy")
async def policy(state: FormState, ctx: FormContext) -> dict[str, Any]:
    plan = FillPlan.model_validate(state["plan"])
    action = ProposedAction(
        id="submit", kind="submit_form", target=plan.url, risk=Risk.IRREVERSIBLE
    )
    verdict = FORM_POLICY.decide(action, Mode(state.get("mode", "interactive")))
    ctx.audit.record(
        state["run_id"], "agent:policy", "policy_verdict", kind="submit_form", verdict=verdict.value
    )
    return {"verdict": verdict.value}


def after_policy(state: FormState) -> str:
    return "package_review" if state["verdict"] == Verdict.NEEDS_HUMAN else "defer"


@audited("defer")
async def defer(state: FormState, ctx: FormContext) -> dict[str, Any]:
    ctx.audit.record(state["run_id"], "agent:defer", "proposal_recorded", kind="submit_form")
    ctx.index.set_status(state["run_id"], RunStatus.DONE)
    await ctx.browser.close()
    return {"outcome": "deferred"}


# Review ---------------------------------------------------------------------------------


@audited("package_review")
async def package_review(state: FormState, ctx: FormContext) -> dict[str, Any]:
    stamped = {**state, "packet_time": utc_now().isoformat()}
    packet = build_packet(stamped)
    write_review_files(_paths(ctx, state), packet)
    return {"packet_time": stamped["packet_time"]}


@audited("mark_waiting")
async def mark_waiting(state: FormState, ctx: FormContext) -> dict[str, Any]:
    plan_hash = state["plan"]["plan_hash"]
    if state.get("waiting_marked_for") != plan_hash:
        packet = build_packet(state)
        ctx.index.set_status(state["run_id"], RunStatus.WAITING_REVIEW, title=packet.title[:120])
        ctx.audit.record(
            state["run_id"],
            "agent:mark_waiting",
            "waiting_review",
            plan_hash=plan_hash,
            **packet.details["counts"],
        )
        try:
            await ctx.notifier.send(Channel.REVIEW, ping_text(packet))
        except Exception as exc:  # noqa: BLE001 - a failed ping must not lose the run
            log.warning("review_ping_failed", error=str(exc))
    if not ctx.keep_browser_open:
        await ctx.browser.close()
    return {"waiting_marked_for": plan_hash}


def gate_packet(state: dict[str, Any]):
    return build_packet(state)


def after_gate(state: FormState) -> str:
    decision = state.get("decision")
    if decision is None:
        return "approval_gate"
    return {"approve": "prepare_submit", "edit": "apply_edit", "reject": "record_reject"}[
        decision["choice"]
    ]


def _record_decision(state: FormState, ctx: FormContext) -> Decision:
    decision = Decision.model_validate(state["decision"])
    ctx.audit.record(
        state["run_id"],
        f"human:{decision.reviewer}",
        "decision",
        choice=decision.choice,
        reason=decision.reason,
        edits=sorted(decision.edits),
        plan_hash=decision.plan_hash,
    )
    return decision


@audited("apply_edit")
async def apply_edit(state: FormState, ctx: FormContext) -> dict[str, Any]:
    decision = _record_decision(state, ctx)
    plan = FillPlan.model_validate(state["plan"])
    fields: dict[str, tuple[int, Field]] = {}
    for step in state["schema"]:
        for f in FormStep.model_validate(step).fields:
            fields[f.key] = (step["index"], f)
    problems = apply_edits(plan, decision.edits, fields, decision.reviewer)
    plan.plan_hash = plan.compute_hash()
    warnings = [w for w in state.get("warnings", []) if not w.startswith("edit:")]
    warnings += [f"edit: {p}" for p in problems]
    warnings.append("edit: values were changed by the reviewer after the screenshots were taken")
    edited = set(decision.edits)
    return {
        "plan": plan.model_dump(mode="json"),
        "warnings": list(dict.fromkeys(warnings)),
        "draft_failed": [k for k in state.get("draft_failed", []) if k not in edited],
        "decision": None,
    }


@audited("record_reject")
async def record_reject(state: FormState, ctx: FormContext) -> dict[str, Any]:
    _record_decision(state, ctx)
    ctx.index.set_status(state["run_id"], RunStatus.REJECTED)
    return {"outcome": "rejected"}


# Submit phase ---------------------------------------------------------------------------


@audited("prepare_submit")
async def prepare_submit(state: FormState, ctx: FormContext) -> dict[str, Any]:
    """Fresh browser, refill from the approved plan, check the page is the same, read back."""
    if not state.get("decision"):
        raise RuntimeError("prepare_submit needs an approved decision")
    _record_decision(state, ctx)
    target = ctx.target(state["target"])
    plan = FillPlan.model_validate(state["plan"])
    session, page = await ctx.browser.open(ctx, state["run_id"], storage_path(ctx.settings, target))
    await session.goto(page, target.url)
    if target.form and target.form.login and on_login_page(page, target):
        await login(session, page, target, ctx.settings, run_id=state["run_id"], audit=ctx.audit)
        await session.goto(page, target.url)
    session.guard.stage = "fill"
    warnings = list(state.get("warnings", []))
    seen: list[FormStep] = []
    for i, expected_hash in enumerate(plan.schema_hashes):
        step = await detect(page, step_index=i)
        dedupe_keys(step, seen)
        seen.append(step)
        if step.schema_hash != expected_hash:
            warnings.append(
                f"page changed at step {i + 1} since approval; filled again for a new review"
            )
            ctx.audit.record(state["run_id"], "agent:prepare_submit", "page_changed", step=i)
            await ctx.browser.close()
            return {
                "warnings": warnings,
                "replans": state.get("replans", 0) + 1,
                "decision": None,
                "page_changed": True,
            }
        results = await fill_step(page, plan.for_step(i))
        if any(not r.ok for r in results):
            bad = [r.key for r in results if not r.ok]
            raise RuntimeError(f"replay could not fill {bad}; nothing was submitted")
        if i < len(plan.next_buttons):
            await (
                page.get_by_role("button", name=plan.next_buttons[i], exact=True)
                .filter(visible=True)
                .click()
            )
            await settle(page, 250)
    last = plan.final_button.step_index if plan.final_button else len(plan.schema_hashes) - 1
    bad = await verify_all(page, plan.for_step(last))
    if bad:
        raise RuntimeError(f"replayed values did not read back: {bad}; nothing was submitted")
    ctx.browser.ready_for = plan.plan_hash
    ctx.audit.record(
        state["run_id"], "agent:prepare_submit", "replay_verified", steps=len(plan.schema_hashes)
    )
    return {"page_changed": False}


def after_prepare(state: FormState) -> str:
    if state.get("page_changed"):
        return "open" if state.get("replans", 0) <= MAX_REPLANS else "give_up"
    return "mark_submit_attempted"


@audited("give_up")
async def give_up(state: FormState, ctx: FormContext) -> dict[str, Any]:
    has_button = bool((state.get("plan") or {}).get("final_button"))
    reason = "page_keeps_changing" if has_button else "no_submit_button"
    ctx.index.set_status(state["run_id"], RunStatus.FAILED)
    ctx.audit.record(
        state["run_id"], "agent:give_up", "run_stopped", outcome="failed", reason=reason
    )
    await ctx.browser.close()
    return {"outcome": reason}


@audited("mark_submit_attempted")
async def mark_submit_attempted(state: FormState, ctx: FormContext) -> dict[str, Any]:
    # This is checkpointed before the click. Everything after it must never click twice.
    # The claim is a compare-and-set, so if two approvals race, only one may go on to click.
    if not ctx.index.claim(state["run_id"], RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING):
        ctx.audit.record(
            state["run_id"],
            "agent:mark_submit_attempted",
            "submit_claim_lost",
            outcome="refused",
            reason="the run was not waiting (another approval?)",
        )
        ctx.browser.ready_for = None
        return {"submit_attempted": True, "claim_lost": True}
    ctx.audit.record(state["run_id"], "agent:mark_submit_attempted", "submit_attempted")
    return {"submit_attempted": True, "claim_lost": False}


@audited("submit")
async def do_submit(state: FormState, ctx: FormContext) -> dict[str, Any]:
    plan = FillPlan.model_validate(state["plan"])
    if state.get("claim_lost"):
        return {
            "result": {
                "status": "duplicate_approval",
                "url": plan.url,
                "message": "another approval already took this run; nothing clicked",
            }
        }
    if ctx.browser.ready_for != plan.plan_hash or ctx.browser.page is None:
        # Resumed after a crash: the click may or may not have happened. Never click again.
        ctx.audit.record(
            state["run_id"],
            "agent:submit",
            "submit_skipped",
            outcome="needs_human_check",
            reason="resumed after submit was attempted; not clicking twice",
        )
        return {
            "result": {
                "status": "needs_human_check",
                "url": plan.url,
                "message": "the run stopped after the submit was attempted; check the site",
            }
        }
    target = ctx.target(state["target"])
    session, page = ctx.browser.need()
    decision = Decision.model_validate(state["decision"])
    token = issue_token(decision, plan.plan_hash, state["run_id"])
    # One-shot: whatever happens next, this process may not click for this plan again.
    ctx.browser.ready_for = None

    async def crash() -> None:
        if ctx.crash_after_click:
            raise SimulatedCrash("simulated crash right after the click")

    result = await submit(
        session, page, plan, token, target.form.success if target.form else None, after_click=crash
    )
    ctx.browser.ready_for = None
    shot = await _screenshot(page, _paths(ctx, state), "confirmation")
    ctx.audit.record(
        state["run_id"],
        "agent:submit",
        "submitted",
        outcome=result.status,
        confirmation_id=result.confirmation_id,
        button=plan.final_button.text,
    )
    return {
        "result": {**result.model_dump(), "screenshot": shot},
        "screenshots": [*state.get("screenshots", []), shot],
    }


@audited("confirm")
async def confirm(state: FormState, ctx: FormContext) -> dict[str, Any]:
    result = state["result"] or {}
    if result.get("status") == "duplicate_approval":
        # The other approval owns the run's status; don't overwrite it.
        ctx.audit.record(
            state["run_id"], "agent:confirm", "run_finished", outcome="duplicate_approval"
        )
        await ctx.browser.close()
        return {"outcome": "duplicate_approval"}
    status = {
        "submitted": RunStatus.DONE,
        "needs_human_check": RunStatus.NEEDS_HUMAN_CHECK,
    }.get(result.get("status", ""), RunStatus.FAILED)
    ctx.index.set_status(state["run_id"], status)
    paths = _paths(ctx, state)
    import json

    paths.result.write_text(
        json.dumps({"run_id": state["run_id"], **result}, indent=2), encoding="utf-8"
    )
    ctx.audit.record(
        state["run_id"], "agent:confirm", "run_finished", outcome=result.get("status", "failed")
    )
    await ctx.browser.close()
    return {"outcome": result.get("status", "failed")}


def build_graph(checkpointer: Any) -> Any:
    g = StateGraph(FormState, context_schema=FormContext)
    for name, fn in [
        ("open", open_page),
        ("login", do_login),
        ("detect", detect_step),
        ("map", map_fields),
        ("draft", draft_fields),
        ("fill", fill_fields),
        ("next_step", next_step),
        ("verify", verify),
        ("policy", policy),
        ("defer", defer),
        ("package_review", package_review),
        ("mark_waiting", mark_waiting),
        ("apply_edit", apply_edit),
        ("record_reject", record_reject),
        ("prepare_submit", prepare_submit),
        ("give_up", give_up),
        ("mark_submit_attempted", mark_submit_attempted),
        ("submit", do_submit),
        ("confirm", confirm),
    ]:
        g.add_node(name, fn)
    g.add_node("approval_gate", make_approval_gate(gate_packet))

    async def route_open(state: FormState, runtime: Ctx) -> str:
        return "login" if needs_login(state, runtime.context) else "detect"

    g.add_edge(START, "open")
    g.add_conditional_edges("open", route_open, ["login", "detect"])
    g.add_edge("login", "detect")
    g.add_edge("detect", "map")
    g.add_edge("map", "draft")
    g.add_edge("draft", "fill")
    g.add_conditional_edges("fill", after_fill, ["next_step", "verify"])
    g.add_edge("next_step", "detect")
    g.add_conditional_edges("verify", after_verify, ["policy", "give_up"])
    g.add_conditional_edges("policy", after_policy, ["package_review", "defer"])
    g.add_edge("defer", END)
    g.add_edge("package_review", "mark_waiting")
    g.add_edge("mark_waiting", "approval_gate")
    g.add_conditional_edges(
        "approval_gate",
        after_gate,
        ["approval_gate", "prepare_submit", "apply_edit", "record_reject"],
    )
    g.add_edge("apply_edit", "package_review")
    g.add_edge("record_reject", END)
    g.add_conditional_edges(
        "prepare_submit", after_prepare, ["open", "give_up", "mark_submit_attempted"]
    )
    g.add_edge("give_up", END)
    g.add_edge("mark_submit_attempted", "submit")
    g.add_edge("submit", "confirm")
    g.add_edge("confirm", END)
    return g.compile(checkpointer=checkpointer)


def initial_state(
    run_id: str,
    target: str,
    profile_path: str,
    data: dict[str, Any] | None = None,
    mode: str = "interactive",
) -> FormState:
    return FormState(
        run_id=run_id,
        target=target,
        profile_path=profile_path,
        data=data or {},
        mode=mode,
        started_at=utc_now().isoformat(),
        warnings=[],
        replans=0,
        submit_attempted=False,
    )


__all__ = [
    "BrowserHolder",
    "FormContext",
    "SimulatedCrash",
    "SubmitResult",
    "build_graph",
    "check_success",
    "initial_state",
]
