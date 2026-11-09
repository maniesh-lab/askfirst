"""Rules, duplicates and LLM classification with the fake provider (rules beat the LLM)."""

import json

from app.config import Settings
from app.runtime.llm import FakeProvider, make_llm
from app.triage.classify import IssueClass, classify_all, merge
from app.triage.duplicates import candidates, similarity
from app.triage.models import RuleResult
from app.triage.rubric import Rubric, apply_rules
from tests.triage.helpers import NOW, issue

RUBRIC = Rubric.load()


def test_security_words_force_p0_and_escalate() -> None:
    r = apply_rules(issue(title="Possible XSS in previews", body="script runs"), RUBRIC, NOW)
    assert (r.category, r.priority, r.escalate) == ("security", "P0", True)
    assert "type:security" in r.labels


def test_bug_missing_parts_needs_info() -> None:
    r = apply_rules(issue(title="Search is broken", body="It doesn't work."), RUBRIC, NOW)
    assert r.needs_info and set(r.missing) == {"steps", "expected", "actual"}
    full = "Steps to reproduce: 1. open. Expected: works. Actual: error."
    assert not apply_rules(issue(title="Crash on save", body=full), RUBRIC, NOW).needs_info


def test_stale_and_skip_already_triaged() -> None:
    assert apply_rules(issue(title="Old idea", age_days=31), RUBRIC, NOW).stale
    triaged = issue(title="x", labels=["triaged"], age_days=10)
    assert apply_rules(triaged, RUBRIC, NOW, last_run=NOW).skip
    assert not apply_rules(triaged, RUBRIC, NOW).skip  # no last run: look again
    old_triaged = issue(title="x", labels=["triaged"], age_days=40)
    r = apply_rules(old_triaged, RUBRIC, NOW, last_run=NOW)
    assert r.skip and r.stale  # skipped from the LLM, but still flagged stale


def test_duplicate_candidates_only_older_open_issues() -> None:
    a = issue(1, "CSV export crashes on big notebooks", "out of memory on export", age_days=5)
    b = issue(2, "Export to CSV crashes for large notebooks", "out of memory error", age_days=1)
    c = issue(3, "Add dark mode", "please", age_days=3)
    assert similarity(a, b) > 0.5
    assert [n for n, _ in candidates(b, [a, b, c])] == [1]
    assert candidates(a, [a, b, c]) == []  # b is newer than a


def llm_reply(**over) -> str:
    base = {
        "number": 1,
        "category": "bug",
        "priority": "P2",
        "labels": ["type:bug", "priority:P2"],
        "needs_info": False,
        "missing": [],
        "duplicate_of": None,
        "summary": "Save crashes.",
        "rationale": 'The report says "crashes when saving".',
    }
    base.update(over)
    return json.dumps({"results": [base]})


async def test_invalid_label_and_bad_duplicate_are_retried() -> None:
    one = issue(1, "App crashes when saving", "It crashes when saving a note.")
    provider = FakeProvider(
        replies=[llm_reply(labels=["wontfix"]), llm_reply(duplicate_of=99), llm_reply()]
    )
    llm = make_llm(Settings(_env_file=None), provider=provider)
    results, failed = await classify_all(llm, [one], {1: []}, RUBRIC)
    assert failed == [] and results[1].category == "bug"
    assert "not allowed" in provider.calls[1][-1]["content"]
    assert "not in its candidates" in provider.calls[2][-1]["content"]


async def test_missing_must_be_steps_expected_or_actual() -> None:
    # The info comment can only ask for these three; free text like "logs" is sent back.
    one = issue(1, "App crashes when saving", "It crashes when saving a note.")
    provider = FakeProvider(
        replies=[llm_reply(needs_info=True, missing=["logs"]), llm_reply(missing=["steps"])]
    )
    results, failed = await classify_all(
        make_llm(Settings(_env_file=None), provider=provider), [one], {1: []}, RUBRIC
    )
    assert failed == [] and results[1].missing == ["steps"]
    assert (
        len(provider.calls) == 2
        and "'steps', 'expected' or 'actual'" in provider.calls[1][-1]["content"]
    )


async def test_rationale_must_quote_the_issue() -> None:
    one = issue(1, "App crashes when saving", "It crashes when saving a note.")
    provider = FakeProvider(
        replies=[llm_reply(rationale='It says "something else entirely".'), llm_reply()]
    )
    results, _ = await classify_all(
        make_llm(Settings(_env_file=None), provider=provider), [one], {1: []}, RUBRIC
    )
    assert "quote an exact phrase" in provider.calls[1][-1]["content"] and 1 in results


async def test_failing_batch_splits_then_marks_unclassified() -> None:
    issues = [issue(n, f"Issue number {n}", f"text {n}") for n in (1, 2)]
    good_two = json.dumps(
        {
            "results": [
                json.loads(llm_reply(number=2, rationale='says "Issue number 2"'))["results"][0]
            ]
        }
    )
    # Batch of two fails 3 times (wrong numbers), issue 1 alone fails 3 times, issue 2 alone works.
    replies = [llm_reply()] * 3 + [llm_reply(number=7)] * 3 + [good_two]
    provider = FakeProvider(replies=replies)
    results, failed = await classify_all(
        make_llm(Settings(_env_file=None), provider=provider), issues, {}, RUBRIC
    )
    assert failed == [1] and list(results) == [2]


def test_merge_rules_win_and_are_recorded(tmp_path) -> None:
    from app.runtime.audit import AuditLog

    audit = AuditLog(tmp_path)
    sec = issue(7, "API key exposed in console", "token leak")
    rules = apply_rules(sec, RUBRIC, NOW)
    llm = IssueClass(
        number=7,
        category="bug",
        priority="P3",
        labels=["type:bug", "priority:P3", "area:ui", "bogus"],
        summary="s",
        rationale='"API key exposed"',
    )
    merged = merge(sec, rules, llm, RUBRIC, audit=audit, run_id="triage-1")
    assert (merged.category, merged.priority, merged.escalate) == ("security", "P0", True)
    assert merged.labels == ["area:ui", "priority:P0", "type:security"]
    assert len(merged.overrides) == 2 and audit.count("triage-1", "override") == 2
    unclassified = merge(issue(3, "Hmm"), RuleResult(number=3), None, RUBRIC)
    assert unclassified.unclassified and unclassified.source == "none"
    skipped = merge(issue(4, "x"), RuleResult(number=4, skip=True, reasons=["done"]), None, RUBRIC)
    assert skipped.skipped
