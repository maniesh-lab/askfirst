"""Run ids, run index, audit log, hashing and log redaction."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog

from app.runtime.audit import AuditEvent, AuditLog
from app.runtime.hashing import stable_hash
from app.runtime.logging import (
    MASK,
    bind_run,
    configure_logging,
    redact,
    register_secret,
)
from app.runtime.runs import RunIndex, RunPaths, RunStatus, new_run_id, run_kind


def test_run_id_format_sorts_by_time() -> None:
    a = new_run_id("form", datetime(2026, 11, 3, 8, 31, tzinfo=UTC))
    b = new_run_id("form", datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    assert a.startswith("form-20261103-0831-") and len(a) == len("form-20261103-0831-ab12")
    assert a < b and run_kind(a) == "form"
    with pytest.raises(ValueError):
        new_run_id("bad-kind")


def test_run_paths_refuse_traversal(tmp_path: Path) -> None:
    paths = RunPaths.for_run(tmp_path, "form-1").ensure()
    assert paths.artifacts.is_dir() and paths.audit.name == "audit.jsonl"
    with pytest.raises(ValueError):
        RunPaths.for_run(tmp_path, "../etc")


def test_run_index_create_update_list(tmp_path: Path) -> None:
    index = RunIndex.in_data_dir(tmp_path)
    index.create("form-20261103-0831-aaaa", "Job form")
    index.create("triage-20261103-0900-bbbb", "Triage")
    index.set_status("form-20261103-0831-aaaa", RunStatus.WAITING_REVIEW)
    run = index.get("form-20261103-0831-aaaa")
    assert run is not None and run.status is RunStatus.WAITING_REVIEW and run.kind == "form"
    assert [r.id for r in index.list(status=RunStatus.WAITING_REVIEW)] == [run.id]
    assert len(index.list(kind="triage")) == 1
    assert index.get("nope") is None
    # Compare-and-set: only the first claim wins.
    assert index.claim(run.id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING)
    assert not index.claim(run.id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING)


def test_audit_is_append_only_and_redacts(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    audit.record("form-1", "system", "start", token="ghp_" + "a" * 30)
    audit.append(
        AuditEvent(
            run_id="form-1",
            actor="human:boss",
            action="decision",
            detail={"note": "Bearer abcdefghijklmnop"},
        )
    )
    first_bytes = audit.path("form-1").read_bytes()
    audit.record("form-1", "system", "end")
    # Earlier lines are untouched; the new one is added at the end.
    assert audit.path("form-1").read_bytes().startswith(first_bytes)
    events = audit.read("form-1")
    assert [e.action for e in events] == ["start", "decision", "end"]
    assert events[0].detail["token"] == MASK
    assert "abcdefghijklmnop" not in events[1].detail["note"]
    assert audit.count("form-1", "start") == 1
    assert audit.read("nothing") == []


def test_redact_masks_keys_values_and_registered_secrets() -> None:
    register_secret("hunter2-practice")
    data = {
        "password": "pw",
        "Authorization": "x",
        "nested": [{"api_key": "k"}, "https://discord.com/api/webhooks/1/abc"],
        "text": "my pass is hunter2-practice ok",
        "groq": "gsk_" + "x" * 30,
        "fine": "hello",
        "empty_token": "",
    }
    out = redact(data, extra_secrets=("hunter2-practice",))
    assert out["password"] == MASK and out["Authorization"] == MASK
    assert out["nested"][0]["api_key"] == MASK and out["nested"][1] == MASK
    assert "hunter2" not in out["text"] and out["groq"] == MASK
    assert out["fine"] == "hello" and out["empty_token"] == ""


def test_stable_hash_ignores_key_order_and_secrets() -> None:
    assert stable_hash({"a": 1, "b": [1, 2]}) == stable_hash({"b": [1, 2], "a": 1})
    assert stable_hash({"a": 1, "password": "x"}) == stable_hash({"a": 1, "password": "y"})
    assert stable_hash({"a": 1}) != stable_hash({"a": 2})
    # A field that only looks a bit like "password" still counts towards the hash.
    assert stable_hash({"passport_no": "A1"}) != stable_hash({"passport_no": "B2"})
    assert stable_hash({"a": 1, "password": "x"}) != stable_hash({"a": 1})


def test_secret_key_matching_is_whole_word() -> None:
    from app.runtime.logging import is_secret_key

    for key in ["password", "GITHUB_TOKEN", "api_key", "Authorization", "webhook_url", "pwd"]:
        assert is_secret_key(key), key
    for key in ["passport", "passed", "bypass", "input_tokens", "token_count", "tokenizer"]:
        assert not is_secret_key(key), key


def test_log_lines_carry_run_id_and_hide_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    with bind_run("form-20261103-0831-abcd", module="browser"):
        structlog.get_logger("t").info("hello", api_key="secret-value", note="Bearer abcdefghijkl")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["run_id"] == "form-20261103-0831-abcd"
    assert line["correlation_id"] == "form-20261103-0831-abcd"
    assert line["module"] == "browser"
    assert line["api_key"] == MASK and "abcdefghijkl" not in line["note"]


def test_settings_secrets_are_masked_even_without_a_telltale_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.config import Settings
    from app.runtime.logging import register_secrets

    # A PAT, a Groq key and a webhook in shapes the value patterns would not catch.
    s = Settings(
        _env_file=None,
        triage_github_token="plain-pat-value-123",
        groq_api_key="odd-groq-key-456",
        discord_webhook_url="https://hooks.example.test/abc-789",
    )
    assert {"plain-pat-value-123", "odd-groq-key-456", "https://hooks.example.test/abc-789"} <= set(
        s.secret_values()
    )
    register_secrets(s.secret_values())
    configure_logging("INFO", "json")
    structlog.get_logger("t").info("oops", detail="pat plain-pat-value-123 key odd-groq-key-456")
    err = capsys.readouterr().err
    assert "plain-pat-value-123" not in err and "odd-groq-key-456" not in err
