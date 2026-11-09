"""Policy table, decisions and approval tokens."""

import pytest
from pydantic import ValidationError

from app.runtime.approval import (
    ApprovalRequired,
    ApprovalToken,
    Decision,
    Mode,
    Policy,
    ProposedAction,
    Risk,
    StaleDecision,
    Verdict,
    issue_token,
    require_token,
)

POLICY = Policy(
    {
        "look": Risk.READ,
        "label": Risk.REVERSIBLE,
        "draft": Risk.REVERSIBLE,
        "send": Risk.IRREVERSIBLE,
    },
    auto_reversible={"label"},
)


def action(kind: str, risk: Risk = Risk.READ) -> ProposedAction:
    return ProposedAction(id="1", kind=kind, risk=risk)


@pytest.mark.parametrize(
    ("kind", "mode", "verdict"),
    [
        ("look", Mode.INTERACTIVE, Verdict.AUTO),
        ("look", Mode.UNATTENDED, Verdict.AUTO),
        ("label", Mode.INTERACTIVE, Verdict.AUTO),
        ("label", Mode.UNATTENDED, Verdict.AUTO),
        ("draft", Mode.INTERACTIVE, Verdict.NEEDS_HUMAN),
        ("draft", Mode.UNATTENDED, Verdict.DEFER),
        ("send", Mode.INTERACTIVE, Verdict.NEEDS_HUMAN),
        ("send", Mode.UNATTENDED, Verdict.DEFER),
        ("format_disk", Mode.INTERACTIVE, Verdict.BLOCK),
        ("format_disk", Mode.UNATTENDED, Verdict.BLOCK),
    ],
)
def test_policy_table(kind: str, mode: Mode, verdict: Verdict) -> None:
    assert POLICY.decide(action(kind), mode) is verdict


def test_registered_risk_beats_claimed_risk() -> None:
    # "send" claims to be READ, but the registry says IRREVERSIBLE.
    assert POLICY.decide(action("send", Risk.READ), Mode.UNATTENDED) is Verdict.DEFER


def decision(choice: str = "approve", plan_hash: str = "h1", **kw) -> Decision:
    return Decision(choice=choice, reviewer="boss", plan_hash=plan_hash, **kw)


def test_reject_needs_reason_and_edit_needs_changes() -> None:
    with pytest.raises(ValidationError):
        decision("reject")
    with pytest.raises(ValidationError):
        decision("edit")
    with pytest.raises(ValidationError):
        Decision(choice="approve", reviewer=" ", plan_hash="h")
    assert decision("reject", reason="wrong job").reason == "wrong job"


def test_token_from_approve_with_matching_hash() -> None:
    token = issue_token(decision(), "h1", "form-1")
    require_token(token, "h1")
    assert (token.run_id, token.plan_hash, token.reviewer) == ("form-1", "h1", "boss")


def test_no_token_from_reject_or_edit_or_wrong_hash() -> None:
    with pytest.raises(ApprovalRequired):
        issue_token(decision("reject", reason="no"), "h1", "r")
    with pytest.raises(ApprovalRequired):
        issue_token(decision("edit", edits={"a": "b"}), "h1", "r")
    with pytest.raises(StaleDecision):
        issue_token(decision(plan_hash="old"), "h1", "r")


def test_require_token_refuses_missing_wrong_plan_and_forged() -> None:
    with pytest.raises(ApprovalRequired):
        require_token(None, "h1")
    token = issue_token(decision(), "h1", "r")
    with pytest.raises(ApprovalRequired):
        require_token(token, "h2")
    forged = ApprovalToken.model_construct(**{**token.model_dump(), "plan_hash": "h2"})
    with pytest.raises(ApprovalRequired, match="signature"):
        require_token(forged, "h2")


def test_token_cannot_be_built_directly() -> None:
    with pytest.raises(TypeError):
        ApprovalToken(
            run_id="r", plan_hash="h", reviewer="x", issued_at="2026-01-01", signature="s"
        )
    with pytest.raises(TypeError):
        ApprovalToken.model_validate({})
    with pytest.raises(TypeError):
        ApprovalToken.model_validate_json("{}")
