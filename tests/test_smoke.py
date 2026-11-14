"""Smoke tests: the package imports, the CLI runs, and settings load."""

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import app
from app.cli import main
from app.config import Settings


def test_package_imports() -> None:
    assert app.__doc__


def test_cli_help_exits_zero() -> None:
    # A real subprocess, so this checks `python -m app.cli` the way a person runs it.
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "version" in result.stdout


def test_cli_version_prints_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith("askfirst 1.0.2")


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_name == "askfirst"
    assert settings.data_dir == Path("data")
    assert settings.llm_provider == "fake"
    assert settings.llm_model is None and settings.llm_fallback is None


def test_settings_read_env_and_ignore_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LOG_FORMAT", "console")
    monkeypatch.setenv("LLM_MODEL", "")
    settings = Settings(_env_file=None)
    assert (settings.llm_provider, settings.log_format) == ("groq", "console")
    assert settings.llm_model is None


def test_settings_reject_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_tests_never_read_a_real_env_file(tmp_path, monkeypatch) -> None:
    """The autouse guard in conftest: even a .env with a key and a webhook is ignored."""
    from app.config import REPO_ROOT_ENV, Settings, get_settings

    assert Settings.model_config["env_file"] is None  # the guard is active
    fake_env = tmp_path / ".env"
    fake_env.write_text("LLM_PROVIDER=groq\nGROQ_API_KEY=should-not-be-used\n")
    # What get_settings() would read without the guard, pointed at our temporary file:
    assert Settings(_env_file=fake_env).llm_provider == "fake"  # the guard's env var wins
    assert get_settings().groq_api_key is None
    assert str(REPO_ROOT_ENV).endswith(".env")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["detect", "site:nope/x"], "no site profile"),
        (["plan", "site:practice/nope"], "has no form 'nope'"),
        (["fill-batch", "site:practice/job_apply", "--data", "missing.csv"], "missing.csv"),
        (["runs", "continue", "form-20260101-0000-abcd"], "no run form-20260101-0000-abcd"),
        (["review", "approve", "form-1", "--plan-hash", "ab"], "at least 8 characters"),
    ],
)
def test_cli_typos_get_one_line_not_a_traceback(
    argv: list[str], message: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err


@pytest.mark.parametrize(
    ("graph_status", "run_status", "code"),
    [
        ("done", "done", 0),
        ("done", "rejected", 0),
        ("waiting", "waiting_review", 0),
        ("done", "failed", 1),  # gave up, or the submit failed: the graph still ended normally
        ("done", "needs_human_check", 1),
        ("failed", "running", 1),
    ],
)
def test_exit_code_follows_the_run_status(
    graph_status: str, run_status: str, code: int, tmp_path: Path
) -> None:
    from app.cli import _exit_code
    from app.runtime.graphs import Outcome
    from app.runtime.runs import RunIndex, RunStatus

    RunIndex.in_data_dir(tmp_path).create("form-1", status=RunStatus(run_status))
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert _exit_code(settings, "form-1", Outcome(status=graph_status)) == code


def test_cli_turns_bad_input_into_one_line(capsys) -> None:
    from app import cli

    assert cli.main(["audit", "a/b"]) == 1  # a path, not a run id
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


def test_printed_page_text_has_no_terminal_codes() -> None:
    from app.cli import plain

    shown = plain("#8 P0 security ESCALATE\x1b[2K\rP3 docs (fine)\x1b[8m")
    assert "\x1b" not in shown and "\r" not in shown and "ESCALATE" in shown
