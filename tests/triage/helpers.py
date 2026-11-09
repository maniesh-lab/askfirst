"""Shared triage test helpers: the sandbox issues as FakeGitHub data at a fixed 'now'."""

from datetime import UTC, datetime

from app.triage.github import FakeGitHub
from app.triage.models import Issue
from app.triage.sandbox import as_github, load_sandbox

NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)


def sandbox_github(**kw) -> FakeGitHub:
    return FakeGitHub(as_github(load_sandbox(), NOW), **kw)


def issue(number: int = 1, title: str = "t", body: str = "", age_days: int = 1, **kw) -> Issue:
    raw = as_github(
        [
            {
                "number": number,
                "title": title,
                "body": body,
                "age_days": age_days,
                "labels": kw.pop("labels", []),
            }
        ],
        NOW,
    )[0]
    return Issue.from_github({**raw, **kw})
