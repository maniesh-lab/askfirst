"""The one irreversible step: click the recorded final button, with an approval token.

`submit()` checks the token first (no token, no click). Then it lifts the network guard for this
form's own request only, switches the page's submit blocker off, clicks exactly the button that
was approved (by its stored locator, which must match one element), and checks for success: the
site profile's rule, or a generic "thank you / confirmation ID" check.

If the server sends the form back with errors, the result is `failed_after_submit`, and the agent
never tries again by itself: that needs a new run and a new approval.
"""

import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import structlog
from playwright.async_api import Page
from pydantic import BaseModel

from app.browser.detect import resolve
from app.browser.fill import FillPlan
from app.browser.session import BrowserSession, settle
from app.browser.sites import SuccessRule
from app.runtime.approval import ApprovalToken, require_token

log = structlog.get_logger(__name__)

SUCCESS_TEXT = re.compile(
    r"\b(thank you|thanks|received|success|submitted|published|confirmation)\b", re.I
)
CONFIRMATION_RE = re.compile(r"\b([A-Z]{2,6}-[A-Z0-9]{4,12})\b")
ERROR_TEXT = re.compile(r"\b(please fix|is required|invalid|error)\b", re.I)


class SubmitResult(BaseModel):
    status: str  # "submitted" | "failed_after_submit"
    url: str
    confirmation_id: str | None = None
    message: str = ""


class SubmitRefused(Exception):
    """The final button isn't on the page exactly once, so nothing was clicked."""


async def submit(
    session: BrowserSession,
    page: Page,
    plan: FillPlan,
    token: ApprovalToken | None,
    success: SuccessRule | None = None,
    *,
    after_click: Callable[[], Awaitable[None]] | None = None,
) -> SubmitResult:
    require_token(token, plan.plan_hash)
    button = plan.final_button
    if button is None:
        raise SubmitRefused("the plan has no final button")
    loc = resolve(page, button.locator)
    if await loc.count() != 1:
        raise SubmitRefused(f"final button {button.text!r} not found exactly once")
    action = urljoin(page.url, button.form_action or page.url)
    method = (button.form_method or "post").upper()
    form_url = page.url
    session.guard.stage = "submit"
    session.guard.allow_write(method, action)
    await session.allow_submit_scripts(page, True)
    try:
        async with page.expect_navigation(timeout=20_000):
            await loc.click()
        if after_click is not None:
            await after_click()
        await settle(page, 200)
    finally:
        session.guard.clear_writes()
        session.guard.stage = "fill"
    still_there = await resolve(page, button.locator).count() > 0
    return await check_success(page, success, form_url=form_url, button_still_there=still_there)


async def check_success(
    page: Page,
    rule: SuccessRule | None,
    *,
    form_url: str | None = None,
    button_still_there: bool = False,
) -> SubmitResult:
    """With a site rule: its URL and text must match. Without one (generic): the page must have
    moved on (new URL or the button gone), show no error text, and show thank-you text or a
    confirmation ID."""
    text = await page.locator("body").inner_text()
    found = CONFIRMATION_RE.search(text)
    confirmation = found.group(1) if found else None
    if rule is not None and (rule.url_contains or rule.text):
        ok_url = rule.url_contains is None or rule.url_contains in page.url
        ok_text = rule.text is None or rule.text.lower() in text.lower()
        ok = ok_url and ok_text
    else:
        moved_on = (form_url is not None and page.url != form_url) or not button_still_there
        ok = (
            moved_on
            and not ERROR_TEXT.search(text)
            and bool(SUCCESS_TEXT.search(text) or confirmation)
        )
    if ok:
        return SubmitResult(status="submitted", url=page.url, confirmation_id=confirmation)
    message = "the site showed errors" if ERROR_TEXT.search(text) else "no success page found"
    return SubmitResult(
        status="failed_after_submit", url=page.url, confirmation_id=confirmation, message=message
    )
