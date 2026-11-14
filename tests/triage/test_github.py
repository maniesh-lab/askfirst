"""GitHub client: pagination, PR filtering, rate limits, read-only mode; FakeGitHub basics."""

import json
from pathlib import Path

import httpx2
import pytest

from app.triage.github import FakeGitHub, GitHubClient, GitHubError, RateLimited, ReadOnly
from app.triage.models import Issue

FIX = Path("tests/fixtures/github")


def client(handler, **kw) -> GitHubClient:
    waits = kw.pop("waits", [])

    async def sleep(s: float) -> None:
        waits.append(s)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return GitHubClient(
        http, "ghp_secret_token_value_123456", sleep=sleep, clock=lambda: 1000.0, **kw
    )


async def test_paginates_and_drops_pull_requests() -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if "page=2" in str(request.url):
            return httpx2.Response(200, json=json.loads((FIX / "issues_page2.json").read_text()))
        return httpx2.Response(
            200,
            json=json.loads((FIX / "issues_page1.json").read_text()),
            headers={"Link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'},
        )

    issues = await client(handler).list_issues("o/r")
    assert [i["number"] for i in issues] == [3, 1]
    assert seen[0].url.params["per_page"] == "100" and seen[0].headers["Authorization"].startswith(
        "Bearer "
    )
    parsed = Issue.from_github(issues[1])
    assert "[code block" in parsed.body and "long" not in parsed.body


async def test_get_repo_reads_visibility() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/repos/o/r"
        return httpx2.Response(200, json={"full_name": "o/r", "private": True})

    assert (await client(handler).get_repo("o/r"))["private"] is True


async def test_get_issue_reads_one_issue() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET" and request.url.path == "/repos/o/r/issues/12"
        return httpx2.Response(200, json={"number": 12, "updated_at": "2026-11-08T03:01:00Z"})

    assert (await client(handler).get_issue("o/r", 12))["updated_at"] == "2026-11-08T03:01:00Z"


async def test_waits_on_short_secondary_limit_then_retries() -> None:
    replies = [httpx2.Response(403, headers={"Retry-After": "2"}), httpx2.Response(200, json=[])]
    waits: list[float] = []
    assert await client(lambda _r: replies.pop(0), waits=waits).list_issues("o/r") == []
    assert waits == [2.0]


async def test_stops_cleanly_on_long_rate_limit() -> None:
    reply = httpx2.Response(
        403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"}
    )
    with pytest.raises(RateLimited):
        await client(lambda _r: reply).list_issues("o/r")


async def test_plain_403_is_an_error_and_hides_the_token() -> None:
    with pytest.raises(GitHubError) as info:
        await client(lambda _r: httpx2.Response(403, text="Resource not accessible")).list_issues(
            "o/r"
        )
    assert "ghp_secret" not in str(info.value)


async def test_read_only_refuses_writes() -> None:
    gh = client(lambda _r: httpx2.Response(200, json={}), read_only=True)
    with pytest.raises(ReadOnly):
        await gh.add_labels("o/r", 1, ["triaged"])
    with pytest.raises(ReadOnly):
        await FakeGitHub(read_only=True).create_comment("o/r", 1, "hi")


async def test_writes_send_the_right_requests() -> None:
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.url.path.endswith("/labels") and request.method == "GET":
            return httpx2.Response(200, json=[{"name": "triaged"}])
        return httpx2.Response(201, json={"number": 9})

    gh = client(handler)
    await gh.add_labels("o/r", 1, ["type:bug"])
    await gh.create_comment("o/r", 1, "hello")
    await gh.close_issue("o/r", 1)
    assert await gh.ensure_labels("o/r", {"triaged": ("5319e7", ""), "stale": ("ededed", "")}) == [
        "stale"
    ]
    assert (await gh.create_issue("o/r", "Log", "b", ["triage-log"]))["number"] == 9
    assert calls[0] == ("POST", "/repos/o/r/issues/1/labels", {"labels": ["type:bug"]})
    assert calls[2][2] == {"state": "closed", "state_reason": "not_planned"}


async def test_fake_github_round_trip() -> None:
    gh = FakeGitHub()
    created = await gh.create_issue("o/r", "Triage log (bot)", "", ["triage-log"])
    await gh.create_comment("o/r", created["number"], "first")
    assert [i["number"] for i in await gh.find_issues_with_label("o/r", "triage-log")] == [1]
    assert (await gh.list_comments("o/r", 1))[0]["body"] == "first"
    await gh.add_labels("o/r", 1, ["triaged", "triaged"])
    await gh.close_issue("o/r", 1)
    assert await gh.list_issues("o/r", state="open") == []


async def test_never_follows_a_next_link_off_githubs_api() -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(
            200,
            json=json.loads((FIX / "issues_page2.json").read_text()),
            headers={"Link": '<https://evil.example/steal?page=2>; rel="next"'},
        )

    await client(handler).list_issues("o/r")
    assert len(seen) == 1 and all("evil" not in u for u in seen)  # the token stays home
