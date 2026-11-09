"""The approval layer both agents share: risk levels, the policy, review packets, decisions,
approval tokens and the LangGraph approval gate.

The one rule that matters: **an irreversible action needs an `ApprovalToken`**, and a token can
only come from `issue_token()`, which only makes one from an *approved* decision whose plan hash
matches the plan about to run. The functions that do irreversible things call `require_token()`
first. That holds in code, not just in a prompt.

How a token is protected: `ApprovalToken(...)` can't be built directly (the constructor refuses
without a private key object), and each token carries an HMAC signature made with a secret that
only lives in this process. `require_token()` checks the signature, so a hand-made token fails.
Tokens are never stored: the node that needs one issues it from the approved decision.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.runtime.runs import utc_now


class Risk(StrEnum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class Mode(StrEnum):
    # A person can answer, so irreversible actions pause at the gate.
    INTERACTIVE = "interactive"
    # Nobody is watching (cron), so irreversible actions are only proposed, never run.
    UNATTENDED = "unattended"


class Verdict(StrEnum):
    AUTO = "auto"
    NEEDS_HUMAN = "needs_human"
    DEFER = "defer"
    BLOCK = "block"


class ApprovalRequired(Exception):
    """An irreversible action was attempted without a valid token for this exact plan."""


class StaleDecision(Exception):
    """The decision was made for a different plan than the one waiting (plan hash differs)."""


class ProposedAction(BaseModel):
    id: str
    kind: str
    target: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    risk: Risk
    summary: str = ""


class ReviewPacket(BaseModel):
    """Everything a person needs to decide. `details` holds the module's own data."""

    run_id: str
    kind: str
    title: str
    actions: list[ProposedAction] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    plan_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class Decision(BaseModel):
    choice: Literal["approve", "edit", "reject"]
    reviewer: str
    reason: str | None = None
    edits: dict[str, Any] = Field(default_factory=dict)
    plan_hash: str
    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check(self) -> Decision:
        if not self.reviewer.strip():
            raise ValueError("reviewer is required")
        if self.choice == "reject" and not (self.reason or "").strip():
            raise ValueError("a reject needs a reason")
        if self.choice == "edit" and not self.edits:
            raise ValueError("an edit needs at least one change")
        return self


# Only code in this module holds these two.
_ISSUER = object()
_SECRET = secrets.token_bytes(32)


def _sign(run_id: str, plan_hash: str, reviewer: str, issued_at: datetime) -> str:
    message = f"{run_id}|{plan_hash}|{reviewer}|{issued_at.isoformat()}".encode()
    return hmac.new(_SECRET, message, hashlib.sha256).hexdigest()


class ApprovalToken(BaseModel):
    """Proof that a person approved this exact plan. Made only by `issue_token()`."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    plan_hash: str
    reviewer: str
    issued_at: datetime
    signature: str

    def __init__(self, *, _issuer: object = None, **data: Any) -> None:
        if _issuer is not _ISSUER:
            raise TypeError("ApprovalToken can only be made by issue_token()")
        super().__init__(**data)

    @classmethod
    def model_validate(cls, *args: Any, **kwargs: Any) -> ApprovalToken:
        raise TypeError("ApprovalToken can only be made by issue_token()")

    @classmethod
    def model_validate_json(cls, *args: Any, **kwargs: Any) -> ApprovalToken:
        raise TypeError("ApprovalToken can only be made by issue_token()")


def issue_token(decision: Decision, current_plan_hash: str, run_id: str) -> ApprovalToken:
    """A token for this run's current plan. Raises unless the decision approves that plan."""
    if decision.choice != "approve":
        raise ApprovalRequired(f"decision is {decision.choice!r}, not approve")
    if decision.plan_hash != current_plan_hash:
        raise StaleDecision("the approved plan differs from the current plan")
    issued_at = utc_now()
    return ApprovalToken(
        _issuer=_ISSUER,
        run_id=run_id,
        plan_hash=current_plan_hash,
        reviewer=decision.reviewer,
        issued_at=issued_at,
        signature=_sign(run_id, current_plan_hash, decision.reviewer, issued_at),
    )


def require_token(token: ApprovalToken | None, plan_hash: str) -> None:
    """Call first in every function that does something irreversible."""
    if not isinstance(token, ApprovalToken):
        raise ApprovalRequired("no approval token")
    expected = _sign(token.run_id, token.plan_hash, token.reviewer, token.issued_at)
    if not hmac.compare_digest(expected, token.signature):
        raise ApprovalRequired("approval token signature is not valid")
    if token.plan_hash != plan_hash:
        raise ApprovalRequired("approval token is for a different plan")


class Policy:
    """Decides, for each action and mode, whether it runs by itself, waits, is deferred or is
    blocked.

    `kinds` maps every known action kind to its risk. The registered risk wins over whatever
    the action claims, so a mislabelled action can't make itself less risky. An unknown kind is
    blocked. `auto_reversible` lists the reversible kinds allowed to run without a person.
    """

    def __init__(self, kinds: dict[str, Risk], auto_reversible: Iterable[str] = ()) -> None:
        self.kinds = dict(kinds)
        self.auto_reversible = set(auto_reversible)

    def risk_of(self, action: ProposedAction) -> Risk | None:
        return self.kinds.get(action.kind)

    def decide(self, action: ProposedAction, mode: Mode) -> Verdict:
        risk = self.risk_of(action)
        if risk is None:
            return Verdict.BLOCK
        if risk is Risk.READ:
            return Verdict.AUTO
        if risk is Risk.REVERSIBLE and action.kind in self.auto_reversible:
            return Verdict.AUTO
        return Verdict.NEEDS_HUMAN if mode is Mode.INTERACTIVE else Verdict.DEFER


def make_approval_gate(build_packet: Callable[[dict[str, Any]], ReviewPacket]) -> Callable:
    """A LangGraph node that pauses the graph for a person's decision.

    LangGraph runs this node again from the top when the graph resumes, so everything before
    `interrupt()` must be free of side effects: `build_packet` must be a pure function of the
    state. Marking the run as waiting, the audit event and the notification happen in a node
    *before* the gate; the decision's audit event is written by the node *after* it.

    Returns `{"decision": <dict>, "gate_error": None}`, or on a bad resume value or a stale
    plan hash `{"decision": None, "gate_error": "..."}` so the graph can route back here and
    wait again.
    """

    def approval_gate(state: dict[str, Any]) -> dict[str, Any]:
        packet = build_packet(state)
        value = interrupt(packet.model_dump(mode="json"))
        try:
            decision = Decision.model_validate(value)
        except ValidationError as exc:
            return {"decision": None, "gate_error": f"invalid decision: {exc.errors()[0]['msg']}"}
        if decision.plan_hash != packet.plan_hash:
            return {"decision": None, "gate_error": "decision is for a different plan (stale)"}
        return {"decision": decision.model_dump(mode="json"), "gate_error": None}

    return approval_gate
