"""Save every practice-site page (and each listing step) as an HTML fixture.

Starts the practice site on a free port, opens each page in Chromium, and writes the page's
current HTML to `tests/fixtures/forms/<name>.html`. Listing steps are saved with the form's
`data-start-step` set, so the page opens on that step when a test loads it.

This is the one script that drives Playwright directly instead of through `BrowserSession`
(no allowlist or network guard): it only opens our own practice site, started here on
127.0.0.1, and clicks login and "Next", never submit or publish.

Run: `python -m scripts.snapshot_fixtures`. Fixtures are committed; run it again after changing
the practice site's templates.
"""

import asyncio
import re
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from practice_site.config import PracticeSettings
from practice_site.server import running_site

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "forms"
# Tokens change on every render; a fixed value keeps fixture diffs quiet.
TOKEN_RE = re.compile(r'(name="csrf_token" value=")[^"]+(")')


def clean(html: str, base_url: str) -> str:
    html = TOKEN_RE.sub(r"\g<1>fixture-token\g<2>", html)
    return html.replace(base_url, "")


async def snapshot() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    with tempfile.TemporaryDirectory() as tmp:
        settings = PracticeSettings(_env_file=None, data_dir=Path(tmp))
        with running_site(settings) as (base, _app):
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page()

                async def save(name: str) -> None:
                    path = OUT / f"{name}.html"
                    path.write_text(clean(await page.content(), base) + "\n", encoding="utf-8")
                    written.append(path)

                for name, path in [
                    ("job_apply", "/jobs/apply"),
                    ("job_apply_variant_b", "/jobs/apply?variant=b"),
                    ("contact", "/contact"),
                    ("survey", "/survey"),
                    ("login", "/login"),
                ]:
                    await page.goto(base + path)
                    await save(name)

                await page.fill("#login-email", settings.seller_email)
                await page.fill("#login-password", settings.seller_password.get_secret_value())
                await page.click("#login button[type=submit]")
                await page.wait_for_url("**/listings/new")
                for step in range(1, 5):
                    await page.evaluate(
                        "n => document.getElementById('listing').dataset.startStep = String(n)",
                        step,
                    )
                    await save(f"listing_step{step}")
                    if step < 4:
                        await page.locator(f"fieldset[data-step='{step}'] button.next").click()
                await browser.close()
    return written


def main() -> None:
    for path in asyncio.run(snapshot()):
        print(f"wrote {path.relative_to(OUT.parents[2])}")


if __name__ == "__main__":
    main()
