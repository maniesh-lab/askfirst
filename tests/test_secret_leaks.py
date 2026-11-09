"""Secrets never leak: not into logs, the audit log, the checkpoint database, error messages or
the run's output files.

The secrets here are made up and in plain shapes the pattern-based redaction would not spot, so
these tests prove the exact-value masking (every SecretStr setting is registered) too.
"""

import json
from pathlib import Path

import httpx2
import pytest

from app.config import Settings
from app.runtime.logging import configure_logging
from app.runtime.notify import Channel, DiscordNotifier, NotifyError
from app.runtime.runs import RunPaths
from app.triage.github import GitHubClient
from app.triage.runner import open_triage_runner

PAT = "plain-pat-for-tests-6f1c"
WEBHOOK = "https://hooks.example.test/webhook/plain-hook-for-tests-93ab"
GROQ = "plain-groq-key-for-tests-2d7e"


def all_text(data_dir: Path) -> str:
    """Every file the run left behind (SQLite files are read as bytes)."""
    chunks = []
    for path in sorted(data_dir.rglob("*")):
        if path.is_file():
            chunks.append(path.read_bytes().decode("utf-8", errors="replace"))
    return "\n".join(chunks)


async def test_triage_failure_with_an_echoed_token_leaks_nowhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def github(request: httpx2.Request) -> httpx2.Response:
        # A badly behaved proxy that quotes the request's Authorization header back.
        return httpx2.Response(
            500, text=f"upstream error, auth was {request.headers['Authorization']}"
        )

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        triage_github_token=PAT,
        discord_webhook_url=WEBHOOK,
        groq_api_key=GROQ,
    )
    configure_logging("DEBUG", "json")
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(github))
    async with open_triage_runner(settings, github=GitHubClient(http, PAT), durable=True) as runner:
        run_id, out = await runner.start("o/r", since="7d")
    await http.aclose()
    assert out.status == "failed" and "GitHub answered 500" in out.error
    logs = capsys.readouterr()
    for secret in (PAT, WEBHOOK, GROQ):
        assert secret not in out.error
        assert secret not in logs.err + logs.out
        leaked = [
            p.name for p in tmp_path.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()
        ]
        assert not leaked, f"{secret} found in {leaked}"
    assert RunPaths.for_run(tmp_path, run_id).audit.exists()


async def test_discord_error_text_never_holds_the_webhook() -> None:
    def discord(request: httpx2.Request) -> httpx2.Response:
        # Not registered as a secret, and not a discord.com URL: only the notifier knows it.
        return httpx2.Response(404, text=f"Unknown Webhook {request.url}")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(discord)) as http:
        notifier = DiscordNotifier(http, {Channel.DIGEST: "https://hooks.example.test/w/unseen"})
        with pytest.raises(NotifyError) as info:
            await notifier.send(Channel.DIGEST, "hello")
    assert "404" in str(info.value) and "unseen" not in str(info.value)


def test_settings_repr_and_json_hide_secrets() -> None:
    settings = Settings(_env_file=None, triage_github_token=PAT, groq_api_key=GROQ)
    text = repr(settings) + json.dumps(settings.model_dump(mode="json"), default=str)
    assert PAT not in text and GROQ not in text
