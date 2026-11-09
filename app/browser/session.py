"""The browser session: Chromium with a host allowlist and a network guard.

Three layers keep the agent from doing anything it shouldn't:

1. **Host allowlist.** `goto()` refuses a URL whose host isn't in `ALLOWED_HOSTS`, and a
   context-wide route handler aborts every request (pages, scripts, images, XHR) to any other
   host. Each blocked request is an audit event. Service workers are off and WebSockets are
   closed, since neither goes through that handler.
2. **Network guard.** Every POST, PUT, PATCH and DELETE is aborted, so an accidental Enter key
   can't submit the form. The only exceptions are added one at a time with
   `guard.allow_write(method, url)`: the login step's one login request, and the approved
   submit step's one form request. `guard.stage` is recorded with each block for the audit.
3. **Submit blocker.** An init script runs in every page before its own scripts. It cancels
   `submit` events and turns `form.submit()` / `form.requestSubmit()` into no-ops, unless the
   approved submit step has switched them back on for this page.

The browser identifies itself honestly: the User-Agent ends with the app's name.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Route,
    WebSocketRoute,
    async_playwright,
)

from app.config import Settings
from app.runtime.audit import AuditLog

log = structlog.get_logger(__name__)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
VIEWPORT = {"width": 1280, "height": 900}

# Runs before any page script. `__askfirstSubmit` is only set by the approved submit step.
SUBMIT_BLOCKER = """
(() => {
  const allowed = () => window.__askfirstSubmit === true;
  window.addEventListener("submit", (event) => {
    if (!allowed()) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  const realSubmit = HTMLFormElement.prototype.submit;
  const realRequest = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.submit = function () {
    if (allowed()) return realSubmit.call(this);
  };
  HTMLFormElement.prototype.requestSubmit = function (b) {
    if (allowed()) return realRequest.call(this, b);
  };
})();
"""

Stage = Literal["open", "login", "fill", "submit"]
# Tests use this to serve saved fixture pages from a fake origin (after all the checks run).
FixtureServer = Callable[[Route, Request], Awaitable[bool]]


class HostNotAllowed(Exception):
    """The agent was asked to open a host that isn't in ALLOWED_HOSTS."""


@dataclass
class Guard:
    stage: Stage = "open"
    # (method, url without query) pairs allowed even while guarded.
    allowed_writes: set[tuple[str, str]] = field(default_factory=set)
    blocked: list[dict[str, str]] = field(default_factory=list)

    def allow_write(self, method: str, url: str) -> None:
        self.allowed_writes.add((method.upper(), _strip_query(url)))

    def clear_writes(self) -> None:
        self.allowed_writes.clear()

    def write_allowed(self, method: str, url: str) -> bool:
        return (method.upper(), _strip_query(url)) in self.allowed_writes


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def user_agent(settings: Settings) -> str:
    return (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"{settings.app_name}/{version('askfirst')} (human-reviewed form automation)"
    )


class BrowserSession:
    """Use as `async with BrowserSession(settings, run_id=..., audit=...) as session:`."""

    def __init__(
        self,
        settings: Settings,
        *,
        run_id: str = "",
        audit: AuditLog | None = None,
        extra_hosts: set[str] | None = None,
        fixture_server: FixtureServer | None = None,
        headless: bool | None = None,
        storage_state: str | None = None,
        record_video_dir: str | None = None,
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.audit = audit
        self.allowed_hosts = set(settings.allowed_host_set) | set(extra_hosts or ())
        self.fixture_server = fixture_server
        self.headless = settings.headless if headless is None else headless
        self.storage_state = storage_state
        self.record_video_dir = record_video_dir
        self.guard = Guard()
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserSession:
        try:
            return await self._start()
        except BaseException:
            # `async with` never calls __aexit__ when __aenter__ fails: stop what did start.
            await self.__aexit__(None, None, None)
            raise

    async def _start(self) -> BrowserSession:
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=self.headless)
        options: dict[str, Any] = {
            "viewport": VIEWPORT,
            "locale": "en-US",
            "timezone_id": "Asia/Kathmandu",
            "user_agent": user_agent(self.settings),
            # Service workers could fetch outside the route handler below; forms don't need them.
            "service_workers": "block",
        }
        if self.storage_state:
            options["storage_state"] = self.storage_state
        if self.record_video_dir:
            options["record_video_dir"] = self.record_video_dir
            options["record_video_size"] = VIEWPORT
        self.context = await self.browser.new_context(**options)
        self.context.set_default_timeout(15_000)
        if self.settings.playwright_trace_dir:
            await self.context.tracing.start(screenshots=True, snapshots=True)
        await self.context.add_init_script(SUBMIT_BLOCKER)
        await self.context.route("**/*", self._on_request)
        # WebSockets don't go through route(); no form needs one, so every one is closed.
        await self.context.route_web_socket("**/*", self._on_web_socket)
        return self

    async def __aexit__(self, *exc: object) -> None:
        try:
            if self.context and self.settings.playwright_trace_dir:
                folder = Path(self.settings.playwright_trace_dir)
                folder.mkdir(parents=True, exist_ok=True)
                name = f"trace-{self.run_id or 'session'}-{uuid.uuid4().hex[:6]}.zip"
                await self.context.tracing.stop(path=str(folder / name))
        except Exception as exc:  # noqa: BLE001 - a lost trace must not keep the browser open
            log.warning("trace_not_saved", error=str(exc))
        finally:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()

    async def new_page(self) -> Page:
        assert self.context is not None, "use BrowserSession as an async context manager"
        return await self.context.new_page()

    def check_host(self, url: str) -> None:
        if urlsplit(url).scheme not in {"http", "https"} or host_of(url) not in self.allowed_hosts:
            self._record("blocked_navigation", url=_strip_query(url))
            raise HostNotAllowed(f"{host_of(url) or url!r} is not in ALLOWED_HOSTS")

    async def goto(self, page: Page, url: str) -> None:
        self.check_host(url)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_load_state("load")

    async def allow_submit_scripts(self, page: Page, allowed: bool) -> None:
        await page.evaluate("v => { window.__askfirstSubmit = v; }", allowed)

    async def _on_request(self, route: Route, request: Request) -> None:
        url = request.url
        scheme = urlsplit(url).scheme
        if scheme in {"data", "blob", "about"}:
            await route.continue_()
            return
        if host_of(url) not in self.allowed_hosts:
            self._record("blocked_request", reason="host not allowed", url=_strip_query(url))
            await route.abort("blockedbyclient")
            return
        method = request.method.upper()
        # Writes are blocked in every stage; only the exact allowed requests get through.
        if method in WRITE_METHODS and not self.guard.write_allowed(method, url):
            self._record(
                "blocked_request",
                reason=f"{method} during {self.guard.stage}",
                url=_strip_query(url),
            )
            await route.abort("blockedbyclient")
            return
        if self.fixture_server is not None and await self.fixture_server(route, request):
            return
        await route.continue_()

    async def _on_web_socket(self, ws: WebSocketRoute) -> None:
        self._record("blocked_request", reason="websocket", url=_strip_query(ws.url))
        await ws.close()

    def _record(self, action: str, **detail: str) -> None:
        self.guard.blocked.append({"action": action, **detail})
        log.warning(action, **detail)
        if self.audit and self.run_id:
            self.audit.record(self.run_id, "system", action, outcome="blocked", **detail)


async def settle(page: Page, ms: int = 150) -> None:
    """Give page scripts a moment to react (show the next step, update a hidden input)."""
    await asyncio.sleep(ms / 1000)
