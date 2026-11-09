"""Browser tests: every test here needs Playwright's Chromium.

`site` runs our practice site in a background thread on a free port (submissions in a temp
folder, reset allowed), and `settings` points the agent at it.
"""

from pathlib import Path

import httpx2
import pytest

from app.config import Settings
from practice_site.config import PracticeSettings
from practice_site.server import running_site


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/browser" in str(item.fspath):
            item.add_marker(pytest.mark.browser)


class Site:
    def __init__(self, base: str, app) -> None:
        self.base = base
        self.app = app

    def submissions(self) -> list[dict]:
        return httpx2.get(f"{self.base}/api/submissions").json()

    def set_variant(self, variant: str) -> None:
        httpx2.post(f"{self.base}/api/variant", json={"variant": variant}).raise_for_status()


@pytest.fixture
def site(tmp_path: Path):
    settings = PracticeSettings(_env_file=None, data_dir=tmp_path / "practice", allow_reset=True)
    with running_site(settings) as (base, app):
        yield Site(base, app)


@pytest.fixture
def settings(tmp_path: Path, site: Site) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        practice_base_url=site.base,
        allowed_hosts="127.0.0.1",
        llm_provider="fake",
    )
