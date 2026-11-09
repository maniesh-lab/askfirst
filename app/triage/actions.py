"""What the bot would like to change on the issues, and the shared policy that decides what it
may actually do.

- **Add labels** is REVERSIBLE. It runs by itself only in `apply-labels` mode (and only
  allowlisted labels ever reach this point).
- **A "please add info" comment** is seen by people, so it counts as IRREVERSIBLE: proposal only.
- **Closing** an issue (duplicate or stale) is IRREVERSIBLE: proposal only, never automatic.
- **Escalated** (security) issues only ever get labels. A person handles them: the bot never
  proposes closing one, and never asks in public for steps to reproduce a vulnerability.

The shared `Policy` decides. Unattended (cron), irreversible actions are DEFERRED: listed in the
digest as proposals, never run. Interactive, they wait at the approval gate, and the executor
needs an `ApprovalToken` for them, which can only come from a person's approval.
"""

from typing import Any

from app.runtime.approval import (
    ApprovalToken,
    Mode,
    Policy,
    ProposedAction,
    Risk,
    Verdict,
    require_token,
)
from app.triage.github import IssuesAPI
from app.triage.models import PlannedAction, TriageResult

KIND_RISK = {
    "add_labels": Risk.REVERSIBLE,
    "comment": Risk.IRREVERSIBLE,
    "close_issue": Risk.IRREVERSIBLE,
}

# The error text on an action skipped because the run may not write (dry run, read-only).
NOT_APPLIED = "not applied (dry run / read-only)"
# Verdicts that leave an action for a person to decide (now at the gate, or later by hand).
WAITING_VERDICTS = {Verdict.NEEDS_HUMAN.value, Verdict.DEFER.value}

MISSING_TEXT = {
    "steps": "the exact steps to reproduce it",
    "expected": "what you expected to happen",
    "actual": "what happened instead (any error message helps)",
}


def triage_policy(write_mode: str) -> Policy:
    auto = {"add_labels"} if write_mode == "apply-labels" else set()
    return Policy(KIND_RISK, auto_reversible=auto)


def info_comment(result: TriageResult) -> str:
    wanted = [MISSING_TEXT.get(m, m) for m in result.missing] or ["a bit more detail"]
    lines = "\n".join(f"- {w}" for w in wanted)
    return (
        "Thanks for the report! To look into this, could you add:\n"
        f"{lines}\n\n_(Posted after a person approved it, by the triage bot.)_"
    )


def plan_actions(
    results: list[TriageResult], current_labels: dict[int, list[str]]
) -> list[PlannedAction]:
    actions: list[PlannedAction] = []
    for r in results:
        if r.skipped and r.stale and not r.escalate:
            # Already triaged and untouched since, but it has now gone stale.
            actions.append(stale_close(r))
            continue
        if r.skipped or r.unclassified:
            continue
        new = [lab for lab in [*r.labels, "triaged"] if lab not in current_labels.get(r.number, [])]
        if new:
            actions.append(
                PlannedAction(
                    id=f"label-{r.number}",
                    kind="add_labels",
                    issue=r.number,
                    params={"labels": new},
                    summary=f"#{r.number}: add {', '.join(new)}",
                )
            )
        if r.escalate:
            continue  # labels only: see the module docstring
        if r.needs_info:
            actions.append(
                PlannedAction(
                    id=f"comment-{r.number}",
                    kind="comment",
                    issue=r.number,
                    params={"body": info_comment(r)},
                    summary=f"#{r.number}: ask for {', '.join(r.missing) or 'more detail'}",
                )
            )
        if r.duplicate_of:
            actions.append(
                PlannedAction(
                    id=f"close-dup-{r.number}",
                    kind="close_issue",
                    issue=r.number,
                    params={"reason": "duplicate", "duplicate_of": r.duplicate_of},
                    summary=f"#{r.number}: close as a duplicate of #{r.duplicate_of}",
                )
            )
        elif r.stale:
            actions.append(stale_close(r))
    return actions


def stale_close(r: TriageResult) -> PlannedAction:
    return PlannedAction(
        id=f"close-stale-{r.number}",
        kind="close_issue",
        issue=r.number,
        params={"reason": "stale"},
        summary=f"#{r.number}: close as stale",
    )


def as_proposed(action: PlannedAction) -> ProposedAction:
    return ProposedAction(
        id=action.id,
        kind=action.kind,
        target=f"#{action.issue}",
        params=action.params,
        risk=KIND_RISK[action.kind],
        summary=action.summary,
    )


def decide(actions: list[PlannedAction], policy: Policy, mode: Mode) -> list[PlannedAction]:
    for a in actions:
        a.verdict = policy.decide(as_proposed(a), mode).value
    return actions


async def execute(
    action: PlannedAction,
    github: IssuesAPI,
    repo: str,
    *,
    token: ApprovalToken | None = None,
    plan_hash: str = "",
) -> None:
    """Carry out one action. Irreversible kinds refuse without a token for this exact plan."""
    if KIND_RISK[action.kind] is Risk.IRREVERSIBLE:
        require_token(token, plan_hash)
    params: dict[str, Any] = action.params
    if action.kind == "add_labels":
        await github.add_labels(repo, action.issue, list(params["labels"]))
    elif action.kind == "comment":
        await github.create_comment(repo, action.issue, str(params["body"]))
    elif action.kind == "close_issue":
        if params.get("duplicate_of"):
            await github.create_comment(
                repo, action.issue, f"Closing as a duplicate of #{params['duplicate_of']}."
            )
        await github.close_issue(repo, action.issue, "not_planned")


def pending(actions: list[PlannedAction]) -> list[PlannedAction]:
    return [a for a in actions if a.verdict == Verdict.NEEDS_HUMAN and not a.applied]
