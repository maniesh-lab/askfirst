"""Serve saved HTML fixtures from a fake origin, `https://practice.test`, inside the browser.

Requests still go through the session's allowlist and network guard first; only then does this
answer them from disk. `/forms/<name>` serves `tests/fixtures/forms/<name>.html`, `/static/...`
serves the practice site's CSS and JS, and anything else gets a 404.
"""

from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Request, Route

ORIGIN = "https://practice.test"
HOST = "practice.test"
ROOT = Path(__file__).resolve().parents[1]
FORMS = ROOT / "tests" / "fixtures" / "forms"
STATIC = ROOT / "practice_site" / "static"
TYPES = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}


def fixture_url(name: str) -> str:
    return f"{ORIGIN}/forms/{name}"


async def serve_fixtures(route: Route, request: Request) -> bool:
    parts = urlsplit(request.url)
    if parts.hostname != HOST:
        return False
    path = parts.path
    if path.startswith("/forms/"):
        file = FORMS / f"{path.removeprefix('/forms/')}.html"
    elif path.startswith("/static/"):
        file = STATIC / path.removeprefix("/static/")
    else:
        file = None
    if file is None or not file.is_file() or ".." in path:
        await route.fulfill(status=404, body="not found")
        return True
    content_type = TYPES.get(file.suffix, "text/plain")
    await route.fulfill(status=200, body=file.read_bytes(), content_type=content_type)
    return True
