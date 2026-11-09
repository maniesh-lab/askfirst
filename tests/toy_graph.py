"""A toy graph that exercises the whole shared runtime: a -> b -> policy -> mark_waiting ->
approval_gate -> act (IRREVERSIBLE) -> finish.

Used by the runtime tests, and run as a script by the crash test:
    python -m tests.toy_graph <data_dir> <run_id>
runs the graph to the gate on the SQLite checkpointer, prints WAITING, then sleeps until killed.
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.config import Settings
from app.runtime import graphs
from app.runtime.approval import (
    Decision,
    Mode,
    Policy,
    ProposedAction,
    ReviewPacket,
    Risk,
    Verdict,
    issue_token,
    make_approval_gate,
    require_token,
)
from app.runtime.audit import AuditLog
from app.runtime.checkpoint import open_checkpointer
from app.runtime.hashing import stable_hash

POLICY = Policy({"step": Risk.REVERSIBLE, "launch": Risk.IRREVERSIBLE}, auto_reversible={"step"})


class ToyState(TypedDict, total=False):
    run_id: str
    mode: str
    plan: dict[str, Any]
    plan_hash: str
    verdict: str
    waiting_marked: bool
    decision: dict[str, Any] | None
    gate_error: str | None
    proposals: list[dict[str, Any]]
    outcome: str


@dataclass
class ToyContext:
    audit: AuditLog
    effects: Path  # one line per real irreversible effect, readable across processes


def _action(state: ToyState) -> ProposedAction:
    return ProposedAction(
        id="launch-1", kind="launch", target="moon", params=state["plan"], risk=Risk.IRREVERSIBLE
    )


def node_end(runtime: Runtime[ToyContext], state: ToyState, name: str) -> None:
    runtime.context.audit.record(state["run_id"], f"agent:{name}", "node_end", node=name)


def a(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    node_end(runtime, state, "a")
    return {"plan": {"target": "moon", "fuel": 3}}


def b(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    node_end(runtime, state, "b")
    return {"plan_hash": stable_hash(state["plan"])}


def policy(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    verdict = POLICY.decide(_action(state), Mode(state.get("mode", "interactive")))
    runtime.context.audit.record(
        state["run_id"], "agent:policy", "policy_verdict", kind="launch", verdict=verdict.value
    )
    node_end(runtime, state, "policy")
    return {"verdict": verdict.value}


def mark_waiting(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    if not state.get("waiting_marked"):
        runtime.context.audit.record(state["run_id"], "agent:mark_waiting", "waiting_review")
    node_end(runtime, state, "mark_waiting")
    return {"waiting_marked": True}


def build_packet(state: ToyState) -> ReviewPacket:
    return ReviewPacket(
        run_id=state["run_id"],
        kind="toy",
        title="Launch to the moon",
        actions=[_action(state)],
        plan_hash=state["plan_hash"],
    )


def act(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    decision = Decision.model_validate(state["decision"])
    runtime.context.audit.record(
        state["run_id"], f"human:{decision.reviewer}", "decision", choice=decision.choice
    )
    if decision.choice != "approve":
        node_end(runtime, state, "act")
        return {"outcome": decision.choice}
    token = issue_token(decision, state["plan_hash"], state["run_id"])
    launch(token, state["plan_hash"], runtime.context.effects)
    node_end(runtime, state, "act")
    return {"outcome": "launched"}


def launch(token: Any, plan_hash: str, effects: Path) -> None:
    """The irreversible thing. Refuses without a token for this exact plan."""
    require_token(token, plan_hash)
    with effects.open("a") as fh:
        fh.write("launched\n")


def defer(state: ToyState, runtime: Runtime[ToyContext]) -> dict[str, Any]:
    action = _action(state)
    runtime.context.audit.record(
        state["run_id"], "agent:defer", "proposal_recorded", kind=action.kind
    )
    node_end(runtime, state, "defer")
    return {"proposals": [action.model_dump(mode="json")], "outcome": "deferred"}


def after_policy(state: ToyState) -> str:
    return "mark_waiting" if state["verdict"] == Verdict.NEEDS_HUMAN else "defer"


def after_gate(state: ToyState) -> str:
    return "approval_gate" if state.get("decision") is None else "act"


def build(checkpointer: Any) -> Any:
    g = StateGraph(ToyState, context_schema=ToyContext)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_node("policy", policy)
    g.add_node("mark_waiting", mark_waiting)
    g.add_node("approval_gate", make_approval_gate(build_packet))
    g.add_node("act", act)
    g.add_node("defer", defer)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "policy")
    g.add_conditional_edges("policy", after_policy, ["mark_waiting", "defer"])
    g.add_edge("mark_waiting", "approval_gate")
    g.add_conditional_edges("approval_gate", after_gate, ["approval_gate", "act"])
    g.add_edge("act", END)
    g.add_edge("defer", END)
    return g.compile(checkpointer=checkpointer)


async def _run_to_gate_and_hang(data_dir: Path, run_id: str) -> None:
    settings = Settings(_env_file=None, data_dir=data_dir)
    context = ToyContext(AuditLog(data_dir), data_dir / "effects.txt")
    async with open_checkpointer(settings) as saver:
        outcome = await graphs.start(build(saver), {"run_id": run_id}, run_id, context=context)
        print(outcome.status.upper(), flush=True)
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(_run_to_gate_and_hang(Path(sys.argv[1]), sys.argv[2]))
