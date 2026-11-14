"""Digest rendering within Discord's limits, the empty digest, and the state block."""

import json
from datetime import timedelta

from app.triage.digest import (
    MAX_EMBEDS,
    MAX_MESSAGE_CHARS,
    MAX_MESSAGES,
    build_digest,
    cap_messages,
    digest_hash,
    embed_size,
    render_discord,
    render_markdown,
)
from app.triage.github import FakeGitHub
from app.triage.models import DigestWindow, PlannedAction, TriageResult
from app.triage.state import RunState, last_state, parse_state, render_state, save_state
from tests.triage.helpers import NOW

WINDOW = DigestWindow(start=NOW - timedelta(days=1), end=NOW)


def result(n: int, **kw) -> TriageResult:
    base = {
        "number": n,
        "title": f"Issue {n} " + "x" * 80,
        "url": f"https://github.com/o/r/issues/{n}",
        "category": "bug",
        "priority": "P2",
        "summary": "s",
    }
    base.update(kw)
    return TriageResult(**base)


def test_empty_digest_is_one_line() -> None:
    d = build_digest("o/r", WINDOW, "triage-1", [], [])
    assert d.empty
    messages = render_discord(d)
    assert len(messages) == 1 and len(messages[0]) == 1
    assert "no new or updated issues" in messages[0][0]["title"]
    assert "no new or updated issues" in render_markdown(d)


def test_sections_and_markdown() -> None:
    results = [
        result(1, priority="P0", category="security", escalate=True),
        result(2, needs_info=True, missing=["steps"]),
        result(3, duplicate_of=1),
        result(4, stale=True),
        result(5, unclassified=True, category=None, priority=None),
        result(6, skipped=True),
    ]
    actions = [
        PlannedAction(
            id="comment-2", kind="comment", issue=2, summary="#2: ask for steps", verdict="defer"
        ),
        PlannedAction(
            id="label-1", kind="add_labels", issue=1, summary="#1: add labels", applied=True
        ),
    ]
    md = render_markdown(build_digest("o/r", WINDOW, "triage-1", results, actions))
    for heading in [
        "Urgent",
        "Waiting for a person",
        "Needs info",
        "Possible duplicates",
        "Stale",
        "Applied automatically",
        "Unclassified",
    ]:
        assert heading in md, heading
    assert "(escalated)" in md and "#6" not in md


def test_waiting_list_holds_only_open_decisions() -> None:
    def action(n: int, **kw) -> PlannedAction:
        return PlannedAction(id=f"a-{n}", kind="comment", issue=n, summary=f"#{n}: ask", **kw)

    actions = [
        action(1, verdict="defer"),
        action(2, verdict="needs_human"),
        action(3, verdict="rejected"),
        action(4, verdict="dropped"),
        action(5, verdict="needs_human", error="GitHub answered 500"),
        action(6, verdict="auto", error="not applied (dry run / read-only)"),
        action(7, verdict="auto", applied=True),
    ]
    d = build_digest("o/r", WINDOW, "triage-1", [result(1)], actions)
    assert [a.id for a in d.proposals] == ["a-1", "a-2"]
    assert [a.id for a in d.failed] == ["a-5"] and [a.id for a in d.applied] == ["a-7"]
    assert "Failed (not applied)" in render_markdown(
        d
    ) and "GitHub answered 500" in render_markdown(d)


def test_discord_limits_hold_for_a_huge_digest() -> None:
    results = [result(n, priority=["P0", "P1", "P2", "P3"][n % 4]) for n in range(1, 400)]
    actions = [
        PlannedAction(
            id=f"label-{n}",
            kind="add_labels",
            issue=n,
            summary=f"#{n}: add " + "y" * 120,
            verdict="needs_human",
        )
        for n in range(1, 400)
    ]
    digest = build_digest("o/r", WINDOW, "triage-1", results, actions, actions_url="https://a/1")
    messages = render_discord(digest)
    assert 1 < len(messages) <= MAX_MESSAGES
    note = messages[-1][-1]
    assert note["title"] == "Digest cut short" and "https://a/1" in note["description"]
    for embeds in messages:
        assert len(embeds) <= MAX_EMBEDS
        assert sum(embed_size(e) for e in embeds) <= MAX_MESSAGE_CHARS
        for e in embeds:
            assert len(e["title"]) <= 256 and len(e.get("description", "")) <= 4096
            assert len(e.get("fields", [])) <= 25
            assert all(len(f["value"]) <= 1024 for f in e.get("fields", []))


def test_cap_keeps_short_digests_whole() -> None:
    digest = build_digest("o/r", WINDOW, "triage-1", [result(1)], [])
    messages = render_discord(digest)
    assert len(messages) == 1 and cap_messages(messages, digest) == messages
    full = [[{"title": f"t{n}", "description": "x"} for n in range(MAX_EMBEDS)]] * 3
    capped = cap_messages(full, digest, limit=2)
    assert len(capped) == 2 and len(capped[-1]) == MAX_EMBEDS
    assert capped[-1][-1]["description"].startswith("11 more sections")  # 10 cut + 1 replaced


def test_digest_hash_ignores_run_details() -> None:
    a = build_digest("o/r", WINDOW, "triage-1", [result(1)], [], tokens=5)
    b = build_digest("o/r", WINDOW, "triage-2", [result(1)], [], tokens=99, duration_s=3)
    c = build_digest("o/r", WINDOW, "triage-1", [result(2)], [])
    assert digest_hash(a) == digest_hash(b) != digest_hash(c)


def test_state_block_round_trip() -> None:
    state = RunState(
        run_id="triage-1",
        window_start="2026-11-01T00:00:00+00:00",
        window_end="2026-11-02T00:00:00Z",
        digest_hash="h",
    )
    body = render_state("Run triage-1: 3 issues", state)
    assert body.startswith("Run triage-1") and parse_state(body) == state
    assert parse_state("no block here") is None
    assert parse_state("<!-- triage-state {not json} -->") is None
    broken = state.model_dump() | {"touched": {"7": "never"}}
    assert parse_state(f"<!-- triage-state {json.dumps(broken)} -->") is None


async def test_state_blocks_from_strangers_are_ignored() -> None:
    gh = FakeGitHub()
    real = RunState(
        run_id="triage-1",
        window_start="2026-11-01T00:00:00+00:00",
        window_end="2026-11-02T00:00:00+00:00",
        digest_hash="h",
    )
    number = await save_state(gh, "o/r", None, "Run triage-1", real)
    # Anyone can comment on a public issue; this one tries to hide issue 7 forever.
    forged = real.model_copy(
        update={"window_end": "2099-01-01T00:00:00+00:00", "touched": {"7": "2099-01-01"}}
    )
    gh.comments[number].append(
        {"id": 99, "body": render_state("hi", forged), "author_association": "NONE"}
    )
    assert await last_state(gh, "o/r") == (number, real)


def test_markdown_digest_matches_snapshot() -> None:
    """The sandbox digest, rendered with fixed run details. On an intended change, regenerate:
    `UPDATE_SNAPSHOTS=1 pytest tests/triage/test_digest_and_state.py`."""
    import asyncio
    import os
    from pathlib import Path

    from app.config import Settings
    from app.runtime.notify import FakeNotifier
    from app.triage.runner import open_triage_runner
    from tests.triage.helpers import sandbox_github

    async def sandbox_run(tmp: Path):
        settings = Settings(_env_file=None, data_dir=tmp)
        async with open_triage_runner(
            settings,
            github=sandbox_github(),
            notifier=FakeNotifier(),
            durable=False,
            now=lambda: NOW,
        ) as runner:
            return (await runner.start("o/sandbox", since="60d"))[1]

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = asyncio.run(sandbox_run(Path(tmp)))
    digest = build_digest(
        "o/sandbox",
        DigestWindow(start=NOW - timedelta(days=60), end=NOW),
        "triage-20261108-0300-snap",
        [TriageResult.model_validate(r) for r in out.values["results"]],
        [PlannedAction.model_validate(a) for a in out.values["actions"]],
    )
    rendered = render_markdown(digest)
    snapshot = Path("tests/triage/snapshots/digest.md")
    if os.environ.get("UPDATE_SNAPSHOTS"):
        snapshot.write_text(rendered, encoding="utf-8")
    assert rendered == snapshot.read_text(encoding="utf-8")


def test_cap_pops_until_the_last_message_fits() -> None:
    from app.triage.digest import MAX_MESSAGE_CHARS, embed_size

    digest = build_digest("o/r", WINDOW, "triage-1", [result(1)], [])
    big = [
        {"title": "a", "description": "x" * 3000},
        {"title": "b", "description": "x" * 2950},
        {"title": "c", "description": "x" * 10},  # smaller than the note that replaces it
    ]
    capped = cap_messages([big, [{"title": "d", "description": "y"}]], digest, limit=1)
    assert sum(map(embed_size, capped[-1])) <= MAX_MESSAGE_CHARS


def test_empty_digest_still_shows_waiting_proposals() -> None:
    from app.triage.models import PlannedAction

    stale = PlannedAction(
        id="close-stale-3",
        kind="close_issue",
        issue=3,
        verdict="defer",
        summary="#3: close as stale",
    )
    d = build_digest("o/r", WINDOW, "triage-1", [], [stale])
    assert d.empty and "close as stale" in render_markdown(d)


def test_issue_titles_cannot_inject_links_or_mentions() -> None:
    sneaky = result(
        1,
        priority="P0",
        title="[Approve all pending proposals](https://evil.example/approve) <@&123>",
        summary="**click** [here](https://evil.example)",
    )
    d = build_digest("o/r", WINDOW, "triage-1", [sneaky], [])
    for text in (render_markdown(d), str(render_discord(d))):
        assert "](https://evil.example" not in text and "<@&123>" not in text
        assert "Approve all pending proposals" in text  # the words are still shown, as text
