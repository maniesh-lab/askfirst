"""Start and resume LangGraph runs, and read what a paused run is waiting for.

Both helpers return an `Outcome` with a status of `waiting` (paused at the approval gate),
`done` or `failed`, so the CLI and tests never need to know LangGraph's internals.

Every run is one LangGraph thread; we use the run ID as the thread ID. Checkpoints are written
after every step (`durability="sync"`), so a crash loses at most the step that was running.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from langgraph.types import Command

from app.runtime.approval import Decision, ReviewPacket, StaleDecision
from app.runtime.checkpoint import DURABILITY
from app.runtime.logging import known_secrets, redact

log = structlog.get_logger(__name__)

Status = Literal["waiting", "done", "failed"]


@dataclass
class Outcome:
    status: Status
    values: dict[str, Any] = field(default_factory=dict)
    packet: ReviewPacket | None = None
    error: str | None = None


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def _outcome(graph: Any, thread_id: str) -> Outcome:
    snapshot = await graph.aget_state(_config(thread_id))
    values = dict(snapshot.values or {})
    packet = _packet_from(snapshot)
    if packet is not None:
        return Outcome("waiting", values, packet, values.get("gate_error"))
    if snapshot.next:
        return Outcome("failed", values, error=f"stopped before {list(snapshot.next)}")
    return Outcome("done", values)


def _packet_from(snapshot: Any) -> ReviewPacket | None:
    for item in getattr(snapshot, "interrupts", ()) or ():
        if isinstance(item.value, dict) and "plan_hash" in item.value:
            return ReviewPacket.model_validate(item.value)
    return None


async def _run(graph: Any, value: Any, thread_id: str, context: Any) -> Outcome:
    try:
        await graph.ainvoke(value, _config(thread_id), context=context, durability=DURABILITY)
    except Exception as exc:
        log.exception("graph_failed", thread_id=thread_id)
        outcome = await _outcome(graph, thread_id)
        outcome.status = "failed"
        # The error is printed by the CLI and saved in the audit log, so mask secrets first (an
        # HTTP error can quote a URL or a response that holds a token).
        outcome.error = redact(f"{type(exc).__name__}: {exc}", extra_secrets=known_secrets())
        return outcome
    return await _outcome(graph, thread_id)


async def start(
    graph: Any, input: dict[str, Any], thread_id: str, *, context: Any = None
) -> Outcome:
    """Run a new thread from the start until it finishes, fails or pauses at the gate."""
    return await _run(graph, input, thread_id, context)


async def resume(graph: Any, thread_id: str, decision: Decision, *, context: Any = None) -> Outcome:
    """Resume a paused thread with a person's decision.

    Checks first that the run really is waiting and that the decision is for the plan on
    screen (same plan hash). A stale decision raises `StaleDecision` and the run stays waiting.
    """
    packet = await waiting_packet(graph, thread_id)
    if packet is None:
        raise LookupError(f"{thread_id} is not waiting for a decision")
    if decision.plan_hash != packet.plan_hash:
        raise StaleDecision("decision is for a different plan than the one waiting")
    return await _run(graph, Command(resume=decision.model_dump(mode="json")), thread_id, context)


async def continue_run(graph: Any, thread_id: str, *, context: Any = None) -> Outcome:
    """Carry on a thread that stopped mid-way (a crash), from its last checkpoint."""
    return await _run(graph, None, thread_id, context)


async def waiting_packet(graph: Any, thread_id: str) -> ReviewPacket | None:
    """The review packet a paused run is waiting on, or None if it isn't paused."""
    return _packet_from(await graph.aget_state(_config(thread_id)))


async def state_of(graph: Any, thread_id: str) -> dict[str, Any]:
    snapshot = await graph.aget_state(_config(thread_id))
    return dict(snapshot.values or {})
