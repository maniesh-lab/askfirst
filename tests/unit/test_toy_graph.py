"""The toy graph through the whole runtime: approve, reject, stale hash, unattended, crash."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.runtime import graphs
from app.runtime.approval import Decision, StaleDecision
from app.runtime.audit import AuditLog
from app.runtime.checkpoint import open_checkpointer
from tests import toy_graph

NODES = ["a", "b", "policy", "mark_waiting"]


def ctx(tmp_path: Path) -> toy_graph.ToyContext:
    return toy_graph.ToyContext(AuditLog(tmp_path), tmp_path / "effects.txt")


def effects(tmp_path: Path) -> int:
    path = tmp_path / "effects.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def decide(packet, choice: str = "approve", **kw) -> Decision:
    return Decision(choice=choice, reviewer="boss", plan_hash=packet.plan_hash, **kw)


async def test_approve_runs_act_exactly_once(tmp_path: Path) -> None:
    async with open_checkpointer(Settings(_env_file=None), durable=False) as saver:
        graph, c = toy_graph.build(saver), ctx(tmp_path)
        out = await graphs.start(graph, {"run_id": "t-1"}, "t-1", context=c)
        assert out.status == "waiting" and effects(tmp_path) == 0
        assert (await graphs.waiting_packet(graph, "t-1")).plan_hash == out.packet.plan_hash
        done = await graphs.resume(graph, "t-1", decide(out.packet), context=c)
    assert done.status == "done" and done.values["outcome"] == "launched"
    assert effects(tmp_path) == 1


async def test_reject_never_acts(tmp_path: Path) -> None:
    async with open_checkpointer(Settings(_env_file=None), durable=False) as saver:
        graph, c = toy_graph.build(saver), ctx(tmp_path)
        out = await graphs.start(graph, {"run_id": "t-2"}, "t-2", context=c)
        done = await graphs.resume(
            graph, "t-2", decide(out.packet, "reject", reason="no"), context=c
        )
    assert done.values["outcome"] == "reject" and effects(tmp_path) == 0


async def test_stale_hash_is_refused_and_run_keeps_waiting(tmp_path: Path) -> None:
    async with open_checkpointer(Settings(_env_file=None), durable=False) as saver:
        graph, c = toy_graph.build(saver), ctx(tmp_path)
        await graphs.start(graph, {"run_id": "t-3"}, "t-3", context=c)
        stale = Decision(choice="approve", reviewer="boss", plan_hash="old")
        with pytest.raises(StaleDecision):
            await graphs.resume(graph, "t-3", stale, context=c)
        # Even past the helper's check, the gate itself refuses and waits again.
        from langgraph.types import Command

        await graph.ainvoke(
            Command(resume=stale.model_dump(mode="json")),
            {"configurable": {"thread_id": "t-3"}},
            context=c,
        )
        again = await graphs.waiting_packet(graph, "t-3")
        state = await graphs.state_of(graph, "t-3")
    assert again is not None and "stale" in state["gate_error"]
    assert effects(tmp_path) == 0


async def test_unattended_defers_and_never_acts(tmp_path: Path) -> None:
    async with open_checkpointer(Settings(_env_file=None), durable=False) as saver:
        graph, c = toy_graph.build(saver), ctx(tmp_path)
        out = await graphs.start(graph, {"run_id": "t-4", "mode": "unattended"}, "t-4", context=c)
    assert out.status == "done" and out.values["outcome"] == "deferred"
    assert out.values["proposals"][0]["kind"] == "launch"
    assert AuditLog(tmp_path).count("t-4", "proposal_recorded") == 1
    assert effects(tmp_path) == 0


async def test_resume_of_a_run_that_is_not_waiting_fails(tmp_path: Path) -> None:
    async with open_checkpointer(Settings(_env_file=None), durable=False) as saver:
        graph = toy_graph.build(saver)
        with pytest.raises(LookupError):
            await graphs.resume(
                graph, "nope", Decision(choice="approve", reviewer="b", plan_hash="h")
            )


async def test_crash_at_gate_then_resume_in_new_process(tmp_path: Path) -> None:
    """kill -9 while waiting; a new process resumes; every node ran exactly once."""
    run_id = "toy-20261103-0831-dead"
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.toy_graph", str(tmp_path), run_id],
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        assert proc.stdout.readline().strip() == "WAITING"
    finally:
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
    assert proc.returncode == -signal.SIGKILL

    settings = Settings(_env_file=None, data_dir=tmp_path)
    async with open_checkpointer(settings) as saver:
        graph, c = toy_graph.build(saver), ctx(tmp_path)
        packet = await graphs.waiting_packet(graph, run_id)
        assert packet is not None
        done = await graphs.resume(graph, run_id, decide(packet), context=c)
    assert done.status == "done" and effects(tmp_path) == 1
    audit = AuditLog(tmp_path)
    for node in [*NODES, "act"]:
        assert audit.count(run_id, "node_end", node=node) == 1, node
    assert audit.count(run_id, "waiting_review") == 1
