"""Shared pytest setup: the `--run-live` switch for tests that call real services, and an
offline guard so no ordinary test can pick up real keys or webhooks from `.env` or the shell."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        help="also run tests marked `live` (real network and APIs; may cost money)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Live tests are skipped by default so a plain `pytest` (and CI) never calls a real API.
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test: run with --run-live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# Every setting that could make a test reach a real service.
REAL_SERVICE_KEYS = [
    "GROQ_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LLM_FALLBACK",
    "DISCORD_WEBHOOK_URL",
    "DISCORD_REVIEW_WEBHOOK_URL",
    "TRIAGE_GITHUB_TOKEN",
    "TRIAGE_REPO",
]


@pytest.fixture(autouse=True)
def offline_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Unless a test is marked `live`: no `.env` file, the fake LLM, no keys and no webhooks."""
    if "live" in request.keywords:
        yield
        return
    from app.config import Settings, get_settings
    from practice_site.config import PracticeSettings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(PracticeSettings.model_config, "env_file", None)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    for key in REAL_SERVICE_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
