"""Action planning and the executor: what the bot proposes, and what it refuses to do alone."""

import pytest

from app.runtime.approval import ApprovalRequired
from app.triage.actions import execute, plan_actions
from app.triage.github import FakeGitHub
from app.triage.models import PlannedAction, TriageResult


def result(number: int, **kw) -> TriageResult:
    return TriageResult(number=number, title=f"issue {number}", **kw)


def test_skipped_but_stale_issue_gets_a_close_proposal_only() -> None:
    actions = plan_actions([result(4, skipped=True, stale=True)], {4: ["triaged"]})
    assert [(a.id, a.kind) for a in actions] == [("close-stale-4", "close_issue")]
    assert plan_actions([result(5, skipped=True)], {}) == []
    stale_security = result(6, skipped=True, stale=True, escalate=True)
    assert plan_actions([stale_security], {6: ["triaged", "type:security"]}) == []


def test_escalated_issue_gets_labels_only() -> None:
    # A stale, needs-info "duplicate" that is a security report: a person must look at it.
    escalated = result(
        7,
        escalate=True,
        category="security",
        priority="P0",
        labels=["type:security", "priority:P0"],
        needs_info=True,
        missing=["steps"],
        duplicate_of=3,
        stale=True,
    )
    actions = plan_actions([escalated], {})
    assert [a.kind for a in actions] == ["add_labels"]


@pytest.mark.parametrize(
    "action",
    [
        PlannedAction(id="comment-1", kind="comment", issue=1, params={"body": "hi"}, summary="c"),
        PlannedAction(
            id="close-stale-1", kind="close_issue", issue=1, params={"reason": "stale"}, summary="x"
        ),
        PlannedAction(
            id="close-dup-1",
            kind="close_issue",
            issue=1,
            params={"reason": "duplicate", "duplicate_of": 2},
            summary="d",
        ),
    ],
)
async def test_irreversible_actions_refuse_without_a_token(action: PlannedAction) -> None:
    gh = FakeGitHub([{"number": 1, "title": "t", "updated_at": "2026-11-01T00:00:00+00:00"}])
    with pytest.raises(ApprovalRequired):
        await execute(action, gh, "o/r", token=None, plan_hash="abc")
    assert gh.writes == [] and gh.issues[1].get("state", "open") == "open"
