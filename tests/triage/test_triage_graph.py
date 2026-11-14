"""The full triage graph on fixtures: FakeGitHub + fake LLM + FakeNotifier."""

from pathlib import Path

import pytest

from app import cli
from app.config import Settings, get_settings
from app.runtime import graphs
from app.runtime.approval import Decision, StaleDecision
from app.runtime.audit import AuditLog
from app.runtime.notify import FakeNotifier, NotifyError
from app.runtime.runs import RunIndex, RunPaths, RunStatus
from app.triage.runner import open_triage_runner
from app.triage.state import last_state
from tests.triage.helpers import NOW, sandbox_github


def settings(tmp_path: Path, **kw) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, **kw)


async def run(tmp_path, gh, notifier=None, **options):
    notifier = notifier or FakeNotifier()
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=notifier, durable=False, now=lambda: NOW
    ) as runner:
        run_id, out = await runner.start("o/sandbox", **options)
    return run_id, out, notifier


async def test_unattended_propose_changes_nothing_on_issues(tmp_path: Path) -> None:
    gh = sandbox_github()
    run_id, out, notifier = await run(tmp_path, gh, since="60d")
    assert out.status == "done", out.error
    issue_writes = [
        w
        for w in gh.writes
        if w[0] in ("add_labels", "close")
        or (w[0] == "comment" and w[1][0] != out.values["log_issue"])
    ]
    assert issue_writes == []  # only the log issue was touched
    actions = out.values["actions"]
    assert actions and all(a["verdict"] == "defer" and not a["applied"] for a in actions)
    assert any(a["kind"] == "close_issue" for a in actions)
    assert notifier.sent and notifier.sent[0][0].value == "digest"
    md = (RunPaths.for_run(tmp_path, run_id).root / "digest.md").read_text()
    assert "Urgent" in md and "#7" in md
    assert RunIndex.in_data_dir(tmp_path).get(run_id).status is RunStatus.DONE


async def test_apply_labels_mode_applies_only_labels(tmp_path: Path) -> None:
    gh = sandbox_github()
    _, out, _ = await run(tmp_path, gh, since="60d", write_mode="apply-labels")
    kinds = {w[0] for w in gh.writes}
    assert "add_labels" in kinds and "close" not in kinds
    applied = [a for a in out.values["actions"] if a["applied"]]
    assert applied and all(a["kind"] == "add_labels" for a in applied)
    assert "triaged" in [lab["name"] for lab in gh.issues[1]["labels"]]
    # Labels were created with the rubric's colours before they were added.
    first_label = next(i for i, w in enumerate(gh.writes) if w[0] == "add_labels")
    assert any(w[0] == "ensure_labels" for w in gh.writes[:first_label])
    assert gh.labels["triaged"][0] == "5319e7" and gh.labels["priority:P0"][0] == "b60205"


async def test_bot_own_label_writes_do_not_cause_a_retriage(tmp_path: Path) -> None:
    from datetime import timedelta

    # GitHub's clock moves each labelled issue's updated_at past the first run's window_end.
    gh = sandbox_github(clock=lambda: NOW + timedelta(minutes=1))
    _, first, _ = await run(tmp_path, gh, since="60d", write_mode="apply-labels")
    labelled = {a["issue"] for a in first.values["actions"] if a["applied"]}
    assert labelled and set(first.values["touched"]) == {str(n) for n in labelled}
    _, state = await last_state(gh, "o/sandbox")
    assert state.touched == first.values["touched"]
    # A person comments on #1 after the bot's write; nothing else changes.
    gh.issues[1]["updated_at"] = (NOW + timedelta(hours=2)).isoformat()
    later = NOW + timedelta(days=1)
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=FakeNotifier(), durable=False, now=lambda: later
    ) as runner:
        _, second = await runner.start("o/sandbox", write_mode="apply-labels")
    assert second.status == "done", second.error
    by_number = {r["number"]: r for r in second.values["results"]}
    assert set(by_number) == labelled  # only the issues changed since the first run
    assert not by_number[1]["skipped"]  # a person changed it: look again
    assert all(by_number[n]["skipped"] for n in labelled - {1})  # only the bot did: skip


async def test_second_run_uses_saved_window_and_skips_already_posted(tmp_path: Path) -> None:
    gh = sandbox_github()
    notifier = FakeNotifier()
    _, first, _ = await run(tmp_path, gh, notifier, since="60d")
    _, state = await last_state(gh, "o/sandbox")
    assert state.window_end == first.values["window_end"]
    # Same "now": the window is empty, but a one-line digest still proves the schedule is alive.
    _, second, _ = await run(tmp_path, gh, notifier)
    assert second.values["window_start"] == first.values["window_end"]
    assert second.values["digest"]["total_issues"] == 0 and second.values["posted"]
    # A third run with the same window and digest posts nothing (already posted), unless forced.
    sent = len(notifier.sent)
    _, third, _ = await run(tmp_path, gh, notifier)
    assert third.values.get("skipped_post") and len(notifier.sent) == sent
    _, forced, _ = await run(tmp_path, gh, notifier, force=True)
    assert len(notifier.sent) == sent + 1


async def test_default_window_still_finds_stale_issues_once(tmp_path: Path) -> None:
    gh = sandbox_github()
    notifier = FakeNotifier()
    # No --since: the default 7-day window. #26 (45 days) and #27 (40 days) were not updated in
    # it, so only the "went stale" check can find them.
    _, first, _ = await run(tmp_path, gh, notifier)
    stale = {r["number"] for r in first.values["results"] if r["stale"]}
    assert {26, 27} <= stale
    assert {"close-stale-26", "close-stale-27"} <= {a["id"] for a in first.values["actions"]}
    # Next run starts from the saved window: they were already reported, so not again.
    _, second, _ = await run(tmp_path, gh, notifier)
    assert not [r for r in second.values["results"] if r["stale"]]


class FlakyNotifier(FakeNotifier):
    """Fails the first `fail` sends with NotifyError (Discord down or refusing)."""

    def __init__(self, fail: int) -> None:
        super().__init__()
        self.fail = fail

    async def send(self, channel, text="", *, embeds=None) -> bool:
        if self.fail:
            self.fail -= 1
            raise NotifyError("Discord answered 500: oops")
        return await super().send(channel, text, embeds=embeds)


async def test_a_failing_github_write_is_listed_and_the_rest_still_runs(tmp_path: Path) -> None:
    from app.triage.github import GitHubError

    class OneBadIssue(type(sandbox_github())):
        async def add_labels(self, repo, number, labels):
            if number == 1:
                raise GitHubError(
                    "GitHub answered 403 for POST /repos/o/sandbox/issues/1/labels", status=403
                )
            await super().add_labels(repo, number, labels)

    gh = OneBadIssue(sandbox_github().issues.values())
    _, out, notifier = await run(tmp_path, gh, since="60d", write_mode="apply-labels")
    assert out.status == "done", out.error
    failed = out.values["digest"]["failed"]
    assert [a["issue"] for a in failed] == [1] and "403" in failed[0]["error"]
    assert len(out.values["digest"]["applied"]) > 10  # the other issues were labelled
    assert notifier.sent and out.values["posted"]


async def test_digest_post_failure_keeps_the_run_and_saves_no_state(tmp_path: Path) -> None:
    gh = sandbox_github()
    run_id, out, notifier = await run(tmp_path, gh, FlakyNotifier(fail=99), since="60d")
    assert (
        out.status == "done"
        and out.values["post_failed"]
        and out.values["outcome"] == ("post_failed")
    )
    assert out.values.get("log_issue") is None and not gh.writes  # next run posts again
    assert AuditLog(tmp_path).count(run_id, "digest_post_failed") == 1


class NoDigestChannelNotifier(FakeNotifier):
    """What DiscordNotifier does when only the review webhook is set: the digest is skipped."""

    async def send(self, channel, text="", *, embeds=None) -> bool:
        return False


async def test_digest_with_no_digest_webhook_is_not_posted_and_saves_no_state(
    tmp_path: Path,
) -> None:
    gh = sandbox_github()
    _, out, _ = await run(tmp_path, gh, NoDigestChannelNotifier(), since="60d")
    assert out.status == "done" and out.values["post_failed"] and not out.values.get("posted")
    assert out.values.get("log_issue") is None and not gh.writes  # the window doesn't move


def test_cli_refuses_a_writing_run_without_a_digest_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRIAGE_GITHUB_TOKEN", "github_pat_test_only")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    try:
        # Refused before anything is opened, so no GitHub call is made.
        assert cli.main(["triage", "run", "--repo", "o/sandbox"]) == 2
    finally:
        get_settings.cache_clear()
    assert "DISCORD_WEBHOOK_URL is not set" in capsys.readouterr().err


async def test_no_token_marks_label_actions_not_applied_not_failed(tmp_path: Path) -> None:
    # What the runner builds without TRIAGE_GITHUB_TOKEN: a read-only client.
    gh = sandbox_github(read_only=True)
    _, out, _ = await run(tmp_path, gh, since="60d", write_mode="apply-labels")
    assert out.status == "done" and gh.writes == []
    labels = [a for a in out.values["actions"] if a["kind"] == "add_labels"]
    assert labels and all(a["error"] == "not applied (dry run / read-only)" for a in labels)
    assert out.values["digest"]["failed"] == []


async def test_dry_run_and_read_only_never_write(tmp_path: Path) -> None:
    gh = sandbox_github(read_only=True)
    _, out, notifier = await run(tmp_path, gh, since="60d", read_only=True)
    assert out.status == "done" and gh.writes == []
    gh2 = sandbox_github()
    _, out2, notifier2 = await run(
        tmp_path, gh2, since="60d", dry_run=True, write_mode="apply-labels"
    )
    assert gh2.writes == [] and notifier2.sent == []


async def test_interactive_proposals_wait_then_approve_runs_them(tmp_path: Path) -> None:
    gh = sandbox_github()
    s = settings(tmp_path)
    async with open_triage_runner(
        s, github=gh, notifier=FakeNotifier(), durable=False, now=lambda: NOW
    ) as runner:
        run_id, out = await runner.start("o/sandbox", since="60d", mode="interactive")
        assert out.status == "waiting" and out.packet.kind == "triage"
        assert gh.writes == [] or all(w[0] != "close" for w in gh.writes)
        assert RunIndex.in_data_dir(tmp_path).get(run_id).status is RunStatus.WAITING_REVIEW
        # Drop the duplicate-close proposal, then approve the rest.
        edited = await runner.resume(
            run_id,
            Decision(
                choice="edit",
                reviewer="boss",
                edits={"drop": "close-dup-10"},
                plan_hash=out.packet.plan_hash,
            ),
        )
        assert edited.status == "waiting" and edited.packet.plan_hash != out.packet.plan_hash
        done = await runner.resume(
            run_id, Decision(choice="approve", reviewer="boss", plan_hash=edited.packet.plan_hash)
        )
    assert done.status == "done", done.error
    closed = [w for w in gh.writes if w[0] == "close"]
    assert 10 not in [c[1][0] for c in closed] and closed  # stale ones closed, #10 kept
    assert any(w[0] == "comment" and w[1][0] == 3 for w in gh.writes)  # needs-info comment
    assert AuditLog(tmp_path).count(run_id, "decision", choice="approve") == 1


async def test_interactive_reject_runs_nothing(tmp_path: Path) -> None:
    gh = sandbox_github()
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=FakeNotifier(), durable=False, now=lambda: NOW
    ) as runner:
        run_id, out = await runner.start("o/sandbox", since="60d", mode="interactive")
        packet = await graphs.waiting_packet(runner.graph, run_id)
        done = await runner.resume(
            run_id,
            Decision(
                choice="reject", reviewer="boss", reason="not now", plan_hash=packet.plan_hash
            ),
        )
    assert done.status == "done"
    assert not [w for w in gh.writes if w[0] in ("add_labels", "close")]
    assert all(a["verdict"] in ("rejected", "defer", "auto") for a in done.values["actions"])


async def waiting_run(runner):
    run_id, out = await runner.start("o/sandbox", since="60d", mode="interactive")
    assert out.status == "waiting", out.error
    return run_id, out.packet


@pytest.mark.parametrize("choice", ["approve", "reject", "edit"])
async def test_second_of_two_racing_decisions_changes_nothing(tmp_path: Path, choice) -> None:
    gh, notifier = sandbox_github(), FakeNotifier()
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=notifier, durable=False, now=lambda: NOW
    ) as runner:
        run_id, packet = await waiting_run(runner)
        writes_before, sent_before = list(gh.writes), list(notifier.sent)
        # Another process approved the same run a moment earlier and holds the claim.
        index = RunIndex.in_data_dir(tmp_path)
        assert index.claim(run_id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING)
        extra = {"reject": {"reason": "not now"}, "edit": {"edits": {"drop": "close-dup-10"}}}
        out = await runner.resume(
            run_id,
            Decision(
                choice=choice, reviewer="boss", plan_hash=packet.plan_hash, **extra.get(choice, {})
            ),
        )
        # The graph was never touched: it still waits on the same plan.
        still = await graphs.waiting_packet(runner.graph, run_id)
    assert out.status == "failed" and out.error.startswith("ClaimLost:")
    assert gh.writes == writes_before and notifier.sent == sent_before  # no actions, no digest
    assert index.get(run_id).status is RunStatus.SUBMITTING  # still the other approval's
    assert still is not None and still.plan_hash == packet.plan_hash
    audit = AuditLog(tmp_path)
    assert audit.count(run_id, "claim_lost") == 1 and audit.count(run_id, "decision") == 0


async def test_a_stale_decision_hands_the_claim_back(tmp_path: Path) -> None:
    async with open_triage_runner(
        settings(tmp_path),
        github=sandbox_github(),
        notifier=FakeNotifier(),
        durable=False,
        now=lambda: NOW,
    ) as runner:
        run_id, packet = await waiting_run(runner)
        with pytest.raises(StaleDecision):
            await runner.resume(run_id, Decision(choice="approve", reviewer="boss", plan_hash="x"))
        assert RunIndex.in_data_dir(tmp_path).get(run_id).status is RunStatus.WAITING_REVIEW
        done = await runner.resume(
            run_id, Decision(choice="approve", reviewer="boss", plan_hash=packet.plan_hash)
        )
    assert done.status == "done", done.error


async def test_a_continued_run_ends_done_not_failed(tmp_path: Path) -> None:
    class StateWriteFailsOnce(type(sandbox_github())):
        fail = True

        async def create_issue(self, repo, title, body, labels):
            if self.fail:  # the log issue, in write_state, after the digest went out
                self.fail = False
                raise RuntimeError("process died")
            return await super().create_issue(repo, title, body, labels)

    gh = StateWriteFailsOnce(sandbox_github().issues.values())
    index = RunIndex.in_data_dir(tmp_path)
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=FakeNotifier(), durable=False, now=lambda: NOW
    ) as runner:
        run_id, out = await runner.start("o/sandbox", since="60d")
        assert out.status == "failed" and index.get(run_id).status is RunStatus.FAILED
        done = await runner.continue_run(run_id)
    assert done.status == "done", done.error
    assert index.get(run_id).status is RunStatus.DONE
    assert (await last_state(gh, "o/sandbox"))[1].run_id == run_id


@pytest.mark.parametrize("left_as", [RunStatus.FAILED, RunStatus.SUBMITTING])
async def test_runs_continue_after_a_crash_never_repeats_a_write(tmp_path: Path, left_as) -> None:
    class CrashOnce(type(sandbox_github())):
        crash = True

        async def create_comment(self, repo, number, body):
            if number == 3 and self.crash:  # the needs-info comment, mid-way through the list
                self.crash = False
                raise RuntimeError("process died")
            return await super().create_comment(repo, number, body)

    gh, notifier = CrashOnce(sandbox_github().issues.values()), FakeNotifier()
    audit, index = AuditLog(tmp_path), RunIndex.in_data_dir(tmp_path)
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=notifier, durable=False, now=lambda: NOW
    ) as runner:
        run_id, packet = await waiting_run(runner)
        approve = Decision(choice="approve", reviewer="boss", plan_hash=packet.plan_hash)
        crashed = await runner.resume(run_id, approve)
        assert crashed.status == "failed" and "process died" in crashed.error
        assert index.get(run_id).status is RunStatus.FAILED
        before = len(gh.writes)
        assert before > 0  # some actions landed before the crash
        if left_as is RunStatus.SUBMITTING:  # killed outright: nothing marked it failed
            index.set_status(run_id, RunStatus.SUBMITTING)
        again = await runner.resume(run_id, approve)  # refused: the run isn't waiting
        assert again.status == "failed" and again.error.startswith("ClaimLost:")
        assert len(gh.writes) == before
        done = await runner.continue_run(run_id)
    assert done.status == "done", done.error
    assert len(gh.writes) == len({repr(w) for w in gh.writes})  # nothing written twice
    assert not [w for w in gh.writes if w[0] == "comment" and w[1][0] == 3]  # not retried
    [unsure] = [a for a in done.values["actions"] if a["id"] == "comment-3"]
    assert not unsure["applied"] and "check the issue by hand" in unsure["error"]
    applied = [a for a in done.values["actions"] if a["verdict"] == "needs_human"]
    assert all(a["applied"] for a in applied if a["id"] != "comment-3")
    assert audit.count(run_id, "action_unsure") == 1
    assert audit.count(run_id, "decision", choice="approve") == 1
    # One write may or may not have landed: a person must look, as after a form submit crash.
    assert index.get(run_id).status is RunStatus.NEEDS_HUMAN_CHECK and done.values["posted"]
    assert [a["id"] for a in done.values["digest"]["unsure"]] == ["comment-3"]
    assert not [a for a in done.values["digest"]["failed"] if a["id"] == "comment-3"]


async def test_dropping_every_proposal_finishes_the_run(tmp_path: Path) -> None:
    gh = sandbox_github()
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=FakeNotifier(), durable=False, now=lambda: NOW
    ) as runner:
        run_id, packet = await waiting_run(runner)
        every = ",".join(a.id for a in packet.actions)
        out = await runner.resume(
            run_id,
            Decision(
                choice="edit", reviewer="boss", edits={"drop": every}, plan_hash=packet.plan_hash
            ),
        )
    assert out.status == "done", out.error
    assert RunIndex.in_data_dir(tmp_path).get(run_id).status is RunStatus.DONE
    assert not [w for w in gh.writes if w[0] in ("close", "add_labels")]


async def test_drop_ignores_ids_that_are_not_waiting(tmp_path: Path) -> None:
    gh = sandbox_github()
    async with open_triage_runner(
        settings(tmp_path), github=gh, notifier=FakeNotifier(), durable=False, now=lambda: NOW
    ) as runner:
        run_id, packet = await waiting_run(runner)
        out = await runner.resume(
            run_id,
            Decision(
                choice="edit", reviewer="boss", edits={"drop": "nope-1"}, plan_hash=packet.plan_hash
            ),
        )
    assert out.status == "waiting" and out.packet.plan_hash == packet.plan_hash  # nothing dropped
    assert AuditLog(tmp_path).count(run_id, "edit_ignored") == 1


def test_triage_cli_dry_run_with_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import app.triage.runner as runner_module

    gh = sandbox_github()
    original = runner_module.open_triage_runner

    def patched(s, **kw):
        kw.pop("read_only", None)
        return original(s, github=gh, now=lambda: NOW, **kw)

    monkeypatch.setattr(runner_module, "open_triage_runner", patched)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    out_dir = tmp_path / "out"
    assert (
        cli.main(
            [
                "triage",
                "run",
                "--repo",
                "o/sandbox",
                "--since",
                "60d",
                "--dry-run",
                "--output-dir",
                str(out_dir),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "ESCALATE" in printed and "#7" in printed
    assert (out_dir / "digest.md").exists() and (out_dir / "audit.jsonl").exists()
    get_settings.cache_clear()


def test_triage_cli_warns_when_the_token_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import app.triage.runner as runner_module

    original = runner_module.open_triage_runner

    def patched(s, **kw):
        # What the real runner does with no token: a read-only GitHub client.
        return original(s, github=sandbox_github(read_only=True), now=lambda: NOW, **kw)

    monkeypatch.setattr(runner_module, "open_triage_runner", patched)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TRIAGE_GITHUB_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        assert cli.main(["triage", "run", "--repo", "o/sandbox", "--since", "60d"]) == 0
    finally:
        get_settings.cache_clear()
    assert "TRIAGE_GITHUB_TOKEN is not set" in capsys.readouterr().err


def test_triage_cli_private_repo_writes_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import app.triage.runner as runner_module

    original = runner_module.open_triage_runner

    def patched(s, **kw):
        kw.pop("read_only", None)
        return original(s, github=sandbox_github(private=True), now=lambda: NOW, **kw)

    monkeypatch.setattr(runner_module, "open_triage_runner", patched)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    out_dir = tmp_path / "out"
    try:
        argv = ["triage", "run", "--repo", "o/secret", "--since", "60d", "--dry-run"]
        assert cli.main([*argv, "--output-dir", str(out_dir)]) == 0
    finally:
        get_settings.cache_clear()
    files = sorted(p.name for p in out_dir.iterdir())
    assert files == ["digest.md"]  # no JSON digest, no audit log
    text = (out_dir / "digest.md").read_text()
    assert "issues" in text and "o/secret" not in text and "#7" not in text
    printed = capsys.readouterr().out  # a public Actions log in the workflow
    assert "issues triaged" in printed and "API key exposed" not in printed and "#7" not in printed


async def test_unknown_repo_visibility_counts_as_private(tmp_path: Path) -> None:
    from app.triage.github import GitHubError

    class NoMetadata(type(sandbox_github())):
        async def get_repo(self, repo):
            raise GitHubError("GitHub answered 403")

    gh = NoMetadata(sandbox_github().issues.values())
    _, out, _ = await run(tmp_path, gh, since="60d", dry_run=True)
    assert out.values["repo_private"] is True


async def test_every_log_line_in_a_run_carries_the_run_id(tmp_path: Path, capsys) -> None:
    import json

    from app.runtime.logging import configure_logging

    configure_logging("DEBUG", "json")
    capsys.readouterr()
    run_id, out, _ = await run(tmp_path, sandbox_github(), since="60d")
    assert out.status == "done"
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    # Setup lines (LLM rate limit, which notifier) come before the run has an ID.
    first = next(i for i, line in enumerate(lines) if line.get("run_id"))
    assert {line["event"] for line in lines[:first]} <= {"rate limit configured", "notifier_fake"}
    in_run = lines[first:]
    assert len(in_run) > 10  # node logs, LLM calls, the fake digest message
    missing = [line["event"] for line in in_run if line.get("run_id") != run_id]
    assert not missing, missing


async def test_real_runner_without_a_token_is_read_only(tmp_path: Path) -> None:
    # The real wiring (no injected client): no TRIAGE_GITHUB_TOKEN means a read-only client.
    async with open_triage_runner(
        settings(tmp_path), notifier=FakeNotifier(), durable=False
    ) as runner:
        assert runner.context.github.read_only


async def test_a_write_that_may_have_landed_is_unsure_not_failed(tmp_path: Path) -> None:
    from app.triage.github import GitHubError

    class TimesOut(type(sandbox_github())):
        async def add_labels(self, repo, number, labels):
            if number == 1:  # the request went out, the answer never came back
                await super().add_labels(repo, number, labels)
                raise GitHubError("GitHub request failed: ReadTimeout")
            await super().add_labels(repo, number, labels)

    gh = TimesOut(sandbox_github().issues.values())
    run_id, out, _ = await run(tmp_path, gh, since="60d", write_mode="apply-labels")
    digest = out.values["digest"]
    assert [a["issue"] for a in digest["unsure"]] == [1] and not digest["failed"]
    assert RunIndex.in_data_dir(tmp_path).get(run_id).status is RunStatus.NEEDS_HUMAN_CHECK


async def test_a_failed_state_read_fails_the_run_instead_of_starting_over(tmp_path: Path) -> None:
    from app.triage.github import GitHubError

    gh = sandbox_github()
    _, first, _ = await run(tmp_path, gh, since="60d")
    assert first.values["log_issue"]

    class StateUnreadable(type(gh)):
        async def list_comments(self, repo, number):
            raise GitHubError("GitHub answered 502 for GET comments", status=502)

    flaky = StateUnreadable(gh.issues.values())
    _, out, notifier = await run(tmp_path, flaky)
    assert out.status == "failed" and "502" in out.error
    logs = [i for i in flaky.issues.values() if i["title"] == "Triage log (bot)"]
    assert len(logs) == 1 and not notifier.sent  # no second log issue, no digest


async def test_llm_down_keeps_the_window_and_fails_the_run(tmp_path: Path) -> None:
    from app.runtime.llm import make_llm
    from tests.test_failure_behaviour import DownProvider

    gh = sandbox_github()
    llm = make_llm(settings(tmp_path), provider=DownProvider())
    async with open_triage_runner(
        settings(tmp_path),
        github=gh,
        llm=llm,
        notifier=FakeNotifier(),
        durable=False,
        now=lambda: NOW,
    ) as runner:
        _, out = await runner.start("o/sandbox", since="60d")
    assert out.values["llm_down"] and out.values["outcome"] == "llm_down"
    assert out.values.get("log_issue") is None  # the window was not saved: nothing is skipped


async def test_a_partly_posted_digest_is_not_posted(tmp_path: Path) -> None:
    gh = sandbox_github()
    notifier = FlakyNotifier(fail=1)  # the first message (headline, urgent issues) is lost
    _, out, _ = await run(tmp_path, gh, notifier, since="60d")
    assert notifier.sent  # the later parts did go out
    assert out.values["post_failed"] and out.values.get("log_issue") is None
