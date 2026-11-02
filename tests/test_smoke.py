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
    assert capsys.readouterr().out.startswith("askfirst 0.1.0")


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
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
