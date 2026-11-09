"""`runs list`, `runs show` and `audit` commands."""

from pathlib import Path

import pytest

from app import cli
from app.config import get_settings
from app.runtime.audit import AuditLog
from app.runtime.runs import RunIndex, RunPaths, RunStatus


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_runs_list_show_and_audit(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["runs", "list"]) == 0
    assert "no runs" in capsys.readouterr().out
    index = RunIndex.in_data_dir(data_dir)
    index.create("form-20261103-0831-abcd", "Job form")
    index.set_status("form-20261103-0831-abcd", RunStatus.WAITING_REVIEW)
    RunPaths.for_run(data_dir, "form-20261103-0831-abcd").ensure().review_html.write_text("x")
    AuditLog(data_dir).record("form-20261103-0831-abcd", "system", "started", note="n" * 200)
    AuditLog(data_dir).record("form-20261103-0831-abcd", "system", "oops", outcome="failed")

    assert cli.main(["runs", "list", "--status", "waiting_review"]) == 0
    assert "form-20261103-0831-abcd" in capsys.readouterr().out
    assert cli.main(["runs", "show", "form-20261103-0831-abcd"]) == 0
    assert "review:" in capsys.readouterr().out
    assert cli.main(["audit", "form-20261103-0831-abcd"]) == 0
    out = capsys.readouterr().out
    assert "started" in out and "[failed]" in out and "..." in out
    assert cli.main(["runs", "show", "nope"]) == 1
    assert cli.main(["audit", "nope"]) == 1


def test_cli_docstring_names_every_triage_run_flag() -> None:
    import argparse

    from app import cli

    parser = cli.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    triage = commands.choices["triage"]
    tsub = next(a for a in triage._actions if isinstance(a, argparse._SubParsersAction))
    flags = [
        opt
        for action in tsub.choices["run"]._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    ]
    assert flags and all(flag in cli.__doc__ for flag in flags), flags
