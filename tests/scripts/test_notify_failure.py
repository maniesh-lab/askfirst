"""The workflow's failure ping: a User-Agent Discord accepts, and the webhook never printed."""

import urllib.error

import pytest

from scripts import notify_failure


def test_request_names_its_own_user_agent() -> None:
    req = notify_failure.build_request("https://discord.test/api/webhooks/1/secret", "https://r/1")
    assert req.get_header("User-agent") == notify_failure.USER_AGENT
    assert "Python-urllib" not in notify_failure.USER_AGENT
    assert b"https://r/1" in req.data and b'"parse": []' in req.data


def test_failure_does_not_print_the_webhook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/api/webhooks/1/s3cret-token")

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError("https://discord.test", 403, "Forbidden", {}, None)

    monkeypatch.setattr(notify_failure.urllib.request, "urlopen", refuse)
    assert notify_failure.main() == 1
    out = capsys.readouterr()
    assert "403" in out.err and "s3cret-token" not in out.err + out.out


def test_no_webhook_is_a_quiet_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert notify_failure.main() == 0
