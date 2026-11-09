"""Seller login for sites that need one, using credentials from the environment.

The email and password come from the settings the site profile names (for the practice site:
PRACTICE_SELLER_EMAIL and PRACTICE_SELLER_PASSWORD). They are typed by rule, never sent to the
LLM, and the password is registered as a secret so logs and the audit mask it. The network guard
lets exactly one request through: the login form's own POST.

After a good login the browser's cookies are saved under `data/storage/` (gitignored), so the
next run can skip the login while the cookie is still valid.
"""

from pathlib import Path

import structlog
from playwright.async_api import Page
from pydantic import SecretStr

from app.browser.session import BrowserSession, settle
from app.browser.sites import Target
from app.config import Settings
from app.runtime.audit import AuditLog
from app.runtime.logging import register_secret

log = structlog.get_logger(__name__)


class LoginFailed(Exception):
    """The site didn't accept the login (or the credentials aren't set)."""


def storage_path(settings: Settings, target: Target) -> Path:
    name = target.site.name if target.site else "default"
    return Path(settings.data_dir) / "storage" / f"{name}.json"


def credentials(settings: Settings, target: Target) -> tuple[str, str]:
    spec = target.form.login if target.form else None
    if spec is None:
        raise LoginFailed("this form has no login settings")
    email = getattr(settings, spec.email_setting, None)
    password = getattr(settings, spec.password_setting, None)
    if isinstance(password, SecretStr):
        password = password.get_secret_value()
    if not email or not password:
        raise LoginFailed(f"set {spec.email_setting.upper()} and {spec.password_setting.upper()}")
    register_secret(password)
    return str(email), str(password)


def on_login_page(page: Page, target: Target) -> bool:
    spec = target.form.login if target.form else None
    return bool(spec) and page.url.split("?")[0].endswith(spec.url)


async def login(
    session: BrowserSession,
    page: Page,
    target: Target,
    settings: Settings,
    *,
    run_id: str = "",
    audit: AuditLog | None = None,
) -> None:
    spec = target.form.login if target.form else None
    if spec is None:
        return
    email, password = credentials(settings, target)
    login_url = target.absolute(spec.url)
    if not on_login_page(page, target):
        await session.goto(page, login_url)
    form = page.locator("form").filter(has=page.get_by_label(spec.password_field, exact=True))
    await form.get_by_label(spec.email_field, exact=True).fill(email)
    await form.get_by_label(spec.password_field, exact=True).fill(password)
    session.guard.stage = "login"
    session.guard.allow_write("POST", login_url)
    # The submit blocker would stop the login form too; lift it for this one click.
    await session.allow_submit_scripts(page, True)
    try:
        async with page.expect_navigation():
            await form.get_by_role("button", name=spec.submit_button, exact=True).click()
        await settle(page)
    finally:
        session.guard.clear_writes()
        session.guard.stage = "fill"
    if on_login_page(page, target):
        if audit and run_id:
            audit.record(run_id, "agent:login", "login", outcome="failed")
        raise LoginFailed("the site rejected the login")
    path = storage_path(settings, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    await page.context.storage_state(path=str(path))
    log.info("logged_in", site=target.site.name if target.site else None)
    if audit and run_id:
        audit.record(run_id, "agent:login", "login", site=target.site.name if target.site else "")
