"""Detection on saved fixtures, the host allowlist and the network guard, in real Chromium."""

from pathlib import Path

import pytest

from app.browser.detect import detect
from app.browser.session import BrowserSession, HostNotAllowed
from app.config import Settings
from app.runtime.audit import AuditLog
from tests.fixture_site import HOST, fixture_url, serve_fixtures


def session(tmp_path: Path, run_id: str = "form-test") -> BrowserSession:
    return BrowserSession(
        Settings(_env_file=None, data_dir=tmp_path),
        run_id=run_id,
        audit=AuditLog(tmp_path),
        extra_hosts={HOST},
        fixture_server=serve_fixtures,
    )


async def open_fixture(s: BrowserSession, name: str):
    page = await s.new_page()
    await s.goto(page, fixture_url(name))
    return page


async def test_label_resolution_order(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        step = await detect(await open_fixture(s, "edge_aria_labelledby"))
    got = {f.name: (f.label, f.label_source, f.required) for f in step.fields}
    assert got["fname"] == ("First name", "aria-labelledby", True)
    assert got["lname"] == ("Last name", "label-wrap", False)
    assert got["city"] == ("City (where you live now)", "aria-labelledby", False)
    assert got["nickname"] == ("Nickname", "aria-label", False)
    assert got["zip"] == ("Postal code", "preceding-text", True)
    assert got["contact_email"] == ("Email address", "label-for", True)


async def test_job_form_fields_honeypot_and_submit(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        step = await detect(await open_fixture(s, "job_apply"))
    honeypot = next(f for f in step.fields if f.name == "website")
    assert honeypot.honeypot and not honeypot.fillable
    assert step.hidden_inputs == {"csrf_token": "fixture-token"}
    assert [b.text for b in step.buttons_of("submit")] == ["Submit application"]
    radio = next(f for f in step.fields if f.kind == "radio")
    assert radio.label == "Are you allowed to work in Nepal?" and len(radio.options) == 3
    # The search form in the header is never the main form.
    assert all(f.name != "q" for f in step.fields)
    assert all(f.locator is not None for f in step.fillable_fields())


async def test_hidden_disabled_readonly_not_fillable(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        step = await detect(await open_fixture(s, "edge_disabled_readonly"))
    fillable = [f.name for f in step.fillable_fields()]
    assert fillable == ["display_name"]


async def test_aria_widgets_detected(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        step = await detect(await open_fixture(s, "survey"))
    kinds = [f.kind for f in step.fields]
    assert kinds == [
        "aria_radio",
        "combobox",
        "aria_checkbox_group",
        "contenteditable",
        "contenteditable",
    ]


async def test_variant_b_changes_schema_hash(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        a = await detect(await open_fixture(s, "job_apply"))
        b = await detect(await open_fixture(s, "job_apply_variant_b"))
    assert a.schema_hash != b.schema_hash


async def test_goto_refuses_hosts_not_allowed(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        page = await s.new_page()
        with pytest.raises(HostNotAllowed):
            await s.goto(page, "https://example.org/")
        with pytest.raises(HostNotAllowed):
            await s.goto(page, "file:///etc/passwd")
    assert AuditLog(tmp_path).count("form-test", "blocked_navigation") == 2


async def test_requests_to_other_hosts_are_aborted_and_audited(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        page = await open_fixture(s, "contact")
        result = await page.evaluate(
            "() => fetch('https://tracker.example.org/pixel').then(() => 'ok', () => 'blocked')"
        )
    assert result == "blocked"
    events = AuditLog(tmp_path).read("form-test")
    assert any(
        e.action == "blocked_request" and "tracker.example.org" in e.detail["url"] for e in events
    )


async def test_enter_key_on_contact_trap_does_not_submit(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        page = await open_fixture(s, "contact")
        s.guard.stage = "fill"
        await page.get_by_placeholder("Your name").fill("Riya")
        await page.get_by_placeholder("Your name").press("Enter")
        await page.get_by_role("button", name="Send message").click()
        await page.evaluate("() => document.getElementById('contact').submit()")
        await page.wait_for_timeout(300)
        assert page.url == fixture_url("contact")
    # The init script stopped the submits before any request; nothing reached the network.
    assert not [b for b in s.guard.blocked if b["action"] == "blocked_request"]


async def test_guard_blocks_scripted_post_unless_allowed(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        page = await open_fixture(s, "contact")
        s.guard.stage = "fill"
        post = "u => fetch(u, {method: 'POST', body: 'x'}).then(r => r.status, () => 'blocked')"
        assert await page.evaluate(post, "https://practice.test/contact") == "blocked"
        s.guard.allow_write("POST", "https://practice.test/contact")
        assert await page.evaluate(post, "https://practice.test/contact?x=1") == 404
    events = AuditLog(tmp_path).read("form-test")
    assert any(
        e.action == "blocked_request" and e.detail["reason"] == "POST during fill" for e in events
    )


async def test_trace_saved_when_trace_dir_is_set(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, playwright_trace_dir=tmp_path / "tr")
    async with BrowserSession(
        settings, run_id="form-trace", extra_hosts={HOST}, fixture_server=serve_fixtures
    ) as s:
        await open_fixture(s, "contact")
    traces = list((tmp_path / "tr").glob("trace-form-trace-*.zip"))
    assert len(traces) == 1 and traces[0].stat().st_size > 0


async def test_fill_timeout_is_flagged_not_fatal(tmp_path: Path) -> None:
    from app.browser.fill import FillStep, fill_step
    from app.browser.schema import Locator

    async with session(tmp_path) as s:
        page = await open_fixture(s, "contact")
        page.set_default_timeout(300)
        steps = [
            FillStep(
                step_index=0,
                key="gone",
                label="Removed field",
                kind="text",
                value="x",
                source="profile:identity.full_name",
                locator=Locator(strategy="css", css="#field-that-is-not-there"),
            )
        ]
        [result] = await fill_step(page, steps)
    # Tried twice, then flagged for the reviewer; the run carries on.
    assert not result.ok and result.attempts == 2 and result.error.startswith("TimeoutError")


async def test_websockets_are_closed_and_audited(tmp_path: Path) -> None:
    async with session(tmp_path) as s:
        page = await open_fixture(s, "contact")
        opened = await page.evaluate(
            """() => new Promise(done => {
                const ws = new WebSocket("ws://example.org/socket");
                ws.onopen = () => done("open");
                ws.onclose = () => done("closed");
                ws.onerror = () => done("closed");
            })"""
        )
    assert opened == "closed"
    assert AuditLog(tmp_path).count("form-test", "blocked_request", reason="websocket") == 1
