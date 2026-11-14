"""Detection on saved fixtures, the host allowlist and the network guard, in real Chromium."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    assert step.hidden_inputs == {"csrf_token": ""}  # the name only, never the token
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


@pytest.mark.parametrize("command", ["detect", "plan"])
def test_cli_refuses_hosts_not_allowed_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    from app import cli

    settings = Settings(_env_file=None, data_dir=tmp_path, allowed_hosts="127.0.0.1")
    args = cli.build_parser().parse_args([command, "https://example.org/"])
    assert args.handler(settings, args) == 2
    assert "refused: 'example.org' is not in ALLOWED_HOSTS" in capsys.readouterr().err


class _Redirector(BaseHTTPRequestHandler):
    """/off -> another host, /same -> /landed on this host, /landed -> a plain page."""

    hits: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        self.hits.append((self.headers.get("Host", ""), self.path))
        port = self.server.server_address[1]
        targets = {"/off": f"http://localhost:{port}/landed", "/same": "/landed"}
        if self.path in targets:
            self.send_response(302)
            self.send_header("Location", targets[self.path])
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>landed</p>")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def redirector():
    _Redirector.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_redirect_to_a_host_not_allowed_is_blocked(tmp_path: Path, redirector: str) -> None:
    # Playwright routes only the first URL of a redirect chain; without our own check the
    # page would follow this redirect to `localhost`, which isn't allowed.
    settings = Settings(_env_file=None, data_dir=tmp_path, allowed_hosts="127.0.0.1")
    async with BrowserSession(settings, run_id="form-test", audit=AuditLog(tmp_path)) as s:
        page = await s.new_page()
        with pytest.raises(HostNotAllowed):
            await s.goto(page, f"{redirector}/off")
        assert not page.url.startswith("http://localhost")
    assert not any(host.startswith("localhost") for host, _ in _Redirector.hits)
    assert (
        AuditLog(tmp_path).count(
            "form-test", "blocked_request", reason="redirect to a host not allowed"
        )
        == 1
    )


async def test_redirect_within_allowed_hosts_still_works(tmp_path: Path, redirector: str) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, allowed_hosts="127.0.0.1")
    async with BrowserSession(settings, run_id="form-test") as s:
        page = await s.new_page()
        await s.goto(page, f"{redirector}/same")
        assert page.url == f"{redirector}/landed"
        assert await page.locator("p").inner_text() == "landed"
    assert not s.guard.blocked


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


HIDING_TRICKS = """<!doctype html><html><body><form action="/x" method="post">
<label>Name <input name="name"></label>
<div style="opacity:0"><label>Faded <input name="faded"></label></div>
<label class="sr">Sr only <input name="sr_only" style="position:absolute;width:1px;height:1px;
  overflow:hidden;clip:rect(0 0 0 0)"></label>
<div style="height:0;overflow:hidden"><label>Website <input name="site_hp"></label></div>
<label>Far <input name="far" style="position:absolute;top:20000px"></label>
<label>Clipped <input name="clipped" style="clip-path:inset(50%)"></label>
<label>Ink <input name="ink" style="color:transparent;background:transparent;border:0"></label>
<fieldset><legend>Plan</legend>
  <label><input type="radio" name="plan" value="a"> A</label>
  <label><input type="radio" name="plan" value="b"> B</label>
  <span style="opacity:0"><input type="radio" name="plan" value="pro"></span></fieldset>
<select name="city"><option>Kathmandu</option><option hidden>Secret</option>
  <option disabled>Gone</option></select>
<button type="submit">Send</button></form></body></html>"""


async def test_common_hiding_tricks_are_never_fillable(settings: Settings) -> None:
    """Honeypots hidden by an ancestor's opacity, clipping, a zero-height box, far positioning
    or transparent ink must count as hidden (never filled), not just display:none ones."""
    async with BrowserSession(settings) as s:
        page = await s.new_page()
        await page.set_content(HIDING_TRICKS)
        step = await detect(page, step_index=0)
    by_key = {f.name or f.key: f for f in step.fields}
    assert by_key["name"].fillable
    for name in ("faded", "sr_only", "site_hp", "far", "clipped", "ink"):
        assert not by_key[name].fillable, name
    assert [o.value for o in by_key["plan"].options] == ["a", "b"]  # the hidden "pro" isn't offered
    assert [o.text for o in by_key["city"].options] == ["Kathmandu"]
