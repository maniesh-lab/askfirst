"""The triage graph up to classification, on the sandbox issues (FakeGitHub + fake LLM)."""

from pathlib import Path

from app import cli
from app.config import Settings
from app.triage.runner import open_triage_runner
from tests.triage.helpers import NOW, sandbox_github


async def test_classifies_sandbox_issues(tmp_path: Path) -> None:
    async with open_triage_runner(
        Settings(_env_file=None, data_dir=tmp_path),
        github=sandbox_github(),
        durable=False,
        now=lambda: NOW,
    ) as runner:
        run_id, out = await runner.start("o/sandbox", since="60d", dry_run=True)
    assert out.status == "done", out.error
    results = {r["number"]: r for r in out.values["results"]}
    assert len(results) == 30
    assert results[7]["category"] == "security" and results[7]["escalate"]
    assert results[21]["priority"] == "P0"
    assert results[3]["needs_info"] and results[10]["duplicate_of"] == 9
    assert results[26]["stale"]


def test_triage_cli_needs_a_repo(capsys, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.delenv("TRIAGE_REPO", raising=False)
    get_settings.cache_clear()
    assert cli.main(["triage", "run", "--dry-run"]) == 2
    get_settings.cache_clear()
