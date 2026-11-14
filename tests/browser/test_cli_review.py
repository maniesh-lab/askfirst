"""The fill and review commands, end to end on the practice site (in-process CLI)."""

import pytest

from app import cli
from app.config import Settings, get_settings
from app.runtime.runs import RunIndex, RunStatus


@pytest.fixture
def cli_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("PRACTICE_BASE_URL", settings.practice_base_url)
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


def waiting(settings: Settings) -> list[str]:
    return [
        r.id for r in RunIndex.in_data_dir(settings.data_dir).list(status=RunStatus.WAITING_REVIEW)
    ]


def test_fill_review_edit_approve(cli_env: Settings, site, capsys) -> None:
    assert cli.main(["fill", "site:practice/job_apply"]) == 0
    out = capsys.readouterr().out
    assert "waiting" in out and "review.html" in out
    [run_id] = waiting(cli_env)
    assert cli.main(["review", "list"]) == 0
    assert run_id in capsys.readouterr().out
    assert cli.main(["review", "show", run_id]) == 0
    shown = capsys.readouterr().out
    assert "Submit application" in shown and "why_do_you_want_this_role" in shown
    assert cli.main(["review", "edit", run_id, "--set", "cover_letter=Short and true."]) == 0
    assert cli.main(["review", "edit", run_id, "--set", "broken"]) == 2
    # A pin from before the edit is refused: the plan changed since the person looked.
    assert cli.main(["review", "approve", run_id, "--plan-hash", "00000000"]) == 1
    assert "changed since you looked" in capsys.readouterr().err
    assert cli.main(["review", "approve", run_id, "--reviewer", "boss"]) == 0
    assert "confirmation: JOB-" in capsys.readouterr().out
    assert site.submissions()[0]["data"]["cover_letter"] == "Short and true."
    assert cli.main(["review", "approve", run_id]) == 1  # no longer waiting


def test_reject_and_all_clean(cli_env: Settings, site, capsys, monkeypatch) -> None:
    assert cli.main(["fill", "site:practice/contact"]) == 0
    assert (
        cli.main(
            ["fill-batch", "site:practice/marketplace", "--data", "config/products.example.csv"]
        )
        == 0
    )
    runs = waiting(cli_env)
    assert len(runs) == 6
    contact = [
        r for r in runs if "contact" in RunIndex.in_data_dir(cli_env.data_dir).get(r).title or True
    ][-1]
    assert cli.main(["review", "reject", contact, "--reason", "not needed"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert cli.main(["review", "approve", "--all-clean"]) == 0
    assert "nothing approved" in capsys.readouterr().out
    assert cli.main(["review", "approve", "--all-clean", "--yes"]) == 0
    # The cookbook row leaves its photo blank: it waits for a person instead of quietly getting
    # the persona's stock photo, so 4 of the 5 listings are clean and published.
    assert len(site.submissions()) == 4
    [left] = waiting(cli_env)
    assert cli.main(["review", "show", left]) == 0
    assert "the data row leaves" in capsys.readouterr().out
    assert cli.main(["review", "approve"]) == 2  # no run ids and no --all-clean
