"""Run-to-run memory, kept in the target repo itself: a "Triage log (bot)" issue.

Each run adds one comment: a short human summary plus a hidden machine block:

    <!-- triage-state {"run_id": "...", "window_start": "...", "window_end": "...",
                       "digest_hash": "...", "touched": {"12": "<updated_at>"}} -->

The next run reads the latest block: its window starts at the last `window_end` (or 7 days back
the first time). `touched` lists the issues the run changed itself, so the next run can tell
the bot's own edits from a person's. A run whose window start and digest hash match the last
block posts nothing (it already did), unless `--force`. Read-only targets keep no state and
use a fixed window.

Why an issue: the bot runs on GitHub Actions, which has no disk that survives between runs. The
repo it already has access to is the simplest place that does.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from app.triage.github import IssuesAPI

LOG_TITLE = "Triage log (bot)"
LOG_LABEL = "triage-log"
STATE_RE = re.compile(r"<!--\s*triage-state\s+(\{.*?\})\s*-->", re.DOTALL)


class RunState(BaseModel):
    run_id: str
    window_start: str
    window_end: str
    digest_hash: str
    # Issues this run changed itself -> their `updated_at` right after its last write. Our own
    # writes land after `window_end`, so without this the next run would see "updated" issues
    # and triage them again.
    touched: dict[str, str] = Field(default_factory=dict)


def parse_state(body: str) -> RunState | None:
    match = STATE_RE.search(body or "")
    if not match:
        return None
    try:
        return RunState.model_validate(json.loads(match.group(1)))
    except ValueError:
        return None


def render_state(summary: str, state: RunState) -> str:
    return f"{summary}\n\n<!-- triage-state {state.model_dump_json()} -->"


async def find_log_issue(github: IssuesAPI, repo: str) -> dict[str, Any] | None:
    found = await github.find_issues_with_label(repo, LOG_LABEL)
    found = [i for i in found if i.get("title") == LOG_TITLE] or found
    return sorted(found, key=lambda i: i["number"])[0] if found else None


async def last_state(github: IssuesAPI, repo: str) -> tuple[int | None, RunState | None]:
    log_issue = await find_log_issue(github, repo)
    if log_issue is None:
        return None, None
    for comment in reversed(await github.list_comments(repo, log_issue["number"])):
        parsed = parse_state(comment.get("body", ""))
        if parsed is not None:
            return log_issue["number"], parsed
    return log_issue["number"], None


async def save_state(
    github: IssuesAPI, repo: str, log_number: int | None, summary: str, state: RunState
) -> int:
    if log_number is None:
        await github.ensure_labels(repo, {LOG_LABEL: ("bfdadc", "The triage bot's run log")})
        created = await github.create_issue(
            repo,
            LOG_TITLE,
            "The triage bot adds one comment here per run. Please leave this issue open.",
            [LOG_LABEL],
        )
        log_number = created["number"]
    await github.create_comment(repo, log_number, render_state(summary, state))
    return log_number
