"""The whole Module A loop on the practice site, with the fake LLM."""

import json
from pathlib import Path

import pytest

from app.browser.runner import open_form_runner
from app.config import Settings
from app.runtime import graphs
from app.runtime.approval import Decision, StaleDecision
from app.runtime.audit import AuditLog
from app.runtime.llm import FakeProvider, make_llm
from app.runtime.notify import FakeNotifier
from app.runtime.runs import RunIndex, RunPaths, RunStatus

PROFILE = "profiles/example.yaml"


def approve(packet, choice: str = "approve", **kw) -> Decision:
    return Decision(choice=choice, reviewer="boss", plan_hash=packet.plan_hash, **kw)


async def test_job_form_waits_then_submits_exactly_once(settings: Settings, site) -> None:
    notifier = FakeNotifier()
    async with open_form_runner(settings, notifier=notifier, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        assert out.status == "waiting", out.error
        assert site.submissions() == []
        details = out.packet.details
        assert details["final_button"] == "Submit application"
        assert details["counts"]["drafted"] == 2 and details["counts"]["needs_human"] == 0
        paths = RunPaths.for_run(settings.data_dir, run_id)
        assert paths.review_html.exists() and paths.review_json.exists()
        assert (paths.root / "artifacts" / "step-1.png").exists()
        assert (
            RunIndex.in_data_dir(settings.data_dir).get(run_id).status is RunStatus.WAITING_REVIEW
        )
        assert len(notifier.sent) == 1 and "riya" not in notifier.sent[0][1].lower()

        done = await runner.resume(run_id, approve(out.packet))
    assert done.status == "done", done.error
    assert done.values["result"]["status"] == "submitted"
    [row] = site.submissions()
    assert row["form"] == "job_apply" and not row["flagged_bot"]
    data = row["data"]
    assert data["full_name"] == "Riya Testwell" and data["location"] == "Lalitpur"
    assert data["work_authorization"] == "yes" and data["consent"] == "yes"
    assert data["resume"][0]["name"] == "resume.pdf"
    assert 0 < len(data["why_role"]) <= 500
    result = json.loads(paths.result.read_text())
    assert result["confirmation_id"].startswith("JOB-")
    assert RunIndex.in_data_dir(settings.data_dir).get(run_id).status is RunStatus.DONE


async def test_reject_submits_nothing(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        done = await runner.resume(run_id, approve(out.packet, "reject", reason="wrong job"))
    assert done.values["outcome"] == "rejected" and site.submissions() == []
    assert RunIndex.in_data_dir(settings.data_dir).get(run_id).status is RunStatus.REJECTED
    assert AuditLog(settings.data_dir).count(run_id, "decision", choice="reject") == 1


async def test_edit_goes_back_to_gate_with_new_hash(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        old = out.packet
        edited = await runner.resume(
            run_id,
            approve(old, "edit", edits={"cover_letter": "A short letter.", "location": "Pokhara"}),
        )
        assert edited.status == "waiting" and edited.packet.plan_hash != old.plan_hash
        with pytest.raises(StaleDecision):
            await runner.resume(run_id, approve(old))
        done = await runner.resume(run_id, approve(edited.packet))
    assert done.status == "done"
    [row] = site.submissions()
    assert row["data"]["cover_letter"] == "A short letter." and row["data"]["location"] == "Pokhara"


async def test_bad_edit_is_reported_not_applied(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        edited = await runner.resume(
            run_id, approve(out.packet, "edit", edits={"location": "Mars", "nope": "x"})
        )
    warnings = edited.packet.details["warnings"]
    assert any("Mars" in w for w in warnings) and any("nope: no such field" in w for w in warnings)
    # Nothing was applied, so the review must not claim the reviewer changed values.
    assert not any("changed by the reviewer" in w for w in warnings)
    assert edited.packet.plan_hash == out.packet.plan_hash


async def test_edit_reaches_fields_on_earlier_steps(settings: Settings, site) -> None:
    """A multi-step form keeps every step in the page, so each key is seen once per step but is
    only fillable on its own step. An edit must find that one, not the last (hidden) copy."""
    from app.browser.runner import load_batch_rows
    from app.browser.sites import parse_target

    spec = parse_target("site:practice/marketplace", settings).form
    [row] = load_batch_rows("config/products.example.csv", spec.data_columns)[:1]
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/marketplace", PROFILE, data=row)
        edited = await runner.resume(
            run_id,
            approve(out.packet, "edit", edits={"listing_title": "Edited lamp", "price": "2222"}),
        )
        assert not [w for w in edited.packet.details["warnings"] if "can't be filled" in w]
        assert edited.packet.plan_hash != out.packet.plan_hash
        done = await runner.resume(run_id, approve(edited.packet))
    assert done.status == "done"
    [sub] = site.submissions()
    assert sub["data"]["title"] == "Edited lamp" and sub["data"]["price"] == "2222"


async def test_page_changed_after_approval_goes_back_to_review(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        site.set_variant("b")
        again = await runner.resume(run_id, approve(out.packet))
        assert again.status == "waiting", again.error
        assert again.packet.plan_hash != out.packet.plan_hash
        assert any("page changed" in w for w in again.packet.details["warnings"])
        assert site.submissions() == []
        keys = [r["key"] for r in again.packet.details["rows"]]
        assert "mobile_number" in keys and "phone" not in keys
        done = await runner.resume(run_id, approve(again.packet))
    assert done.status == "done" and len(site.submissions()) == 1
    assert site.submissions()[0]["data"]["mobile"] == "+977 980-000-0123"


async def test_crash_right_after_click_never_clicks_again(settings: Settings, site) -> None:
    async with open_form_runner(settings, crash_after_click=True) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        crashed = await runner.resume(run_id, approve(out.packet))
    assert crashed.status == "failed" and "SimulatedCrash" in crashed.error
    assert len(site.submissions()) == 1
    # A new process (fresh context, no live browser) carries on from the checkpoint.
    async with open_form_runner(settings) as runner:
        done = await runner.continue_run(run_id)
    assert done.status == "done" and done.values["outcome"] == "needs_human_check"
    assert len(site.submissions()) == 1
    assert RunIndex.in_data_dir(settings.data_dir).get(run_id).status is RunStatus.NEEDS_HUMAN_CHECK


async def test_restart_while_waiting_then_approve_in_new_process(settings: Settings, site) -> None:
    """Fill in one OS process (the CLI), approve in another: replay, then exactly one submit."""
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "DATA_DIR": str(settings.data_dir),
        "PRACTICE_BASE_URL": site.base,
        "ALLOWED_HOSTS": "127.0.0.1",
        "LLM_PROVIDER": "fake",
    }
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "fill", "site:practice/job_apply"],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    run_id = proc.stdout.split()[1].rstrip(":")
    assert site.submissions() == []
    async with open_form_runner(settings) as runner:
        packet = await graphs.waiting_packet(runner.graph, run_id)
        done = await runner.resume(run_id, approve(packet))
    assert done.status == "done", done.error
    assert len(site.submissions()) == 1
    audit = AuditLog(settings.data_dir)
    for node in [
        "open",
        "detect",
        "map",
        "draft",
        "fill",
        "verify",
        "mark_waiting",
        "submit",
        "confirm",
    ]:
        assert audit.count(run_id, "node_end", node=node) == 1, node


async def test_honeypot_and_hidden_token_untouched(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        assert "website" not in [r["key"] for r in out.packet.details["rows"]]
        await runner.resume(run_id, approve(out.packet))
    [row] = site.submissions()
    # The server checked the hidden token (or it would have refused) and saw no honeypot value.
    assert not row["flagged_bot"]


async def test_fact_check_catches_invented_employer_then_retries(settings: Settings, site) -> None:
    import json as _json

    bad = _json.dumps(
        {
            "answers": {
                "why_do_you_want_this_role": "I led support at Google for 9 years.",
                "cover_letter": "Dear team, I am a support specialist at Example Labs.",
            }
        }
    )
    good = _json.dumps(
        {
            "answers": {
                "why_do_you_want_this_role": "I enjoy helping customers at Example Labs.",
                "cover_letter": "Dear team, I am a support specialist at Example Labs.",
            }
        }
    )
    provider = FakeProvider(replies=[bad, good])
    llm = make_llm(settings, provider=provider)
    async with open_form_runner(settings, llm=llm, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
    assert len(provider.calls) == 2
    feedback = provider.calls[1][-1]["content"]
    assert "'Google' is not in the profile" in feedback and "'9' is not in the profile" in feedback
    rows = {r["key"]: r for r in out.packet.details["rows"]}
    assert (
        rows["why_do_you_want_this_role"]["value"] == "I enjoy helping customers at Example Labs."
    )


async def test_draft_failure_leaves_field_empty_and_flagged(settings: Settings, site) -> None:
    import json as _json

    always_bad = _json.dumps(
        {"answers": {"why_do_you_want_this_role": "[Your Name] here", "cover_letter": "x"}}
    )
    llm = make_llm(settings, provider=FakeProvider(replies=[always_bad]))
    async with open_form_runner(settings, llm=llm, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
    rows = {r["key"]: r for r in out.packet.details["rows"]}
    assert rows["why_do_you_want_this_role"]["status"] == "needs_draft"
    assert "draft_failed" in rows["why_do_you_want_this_role"]["flags"]
    assert any("drafting failed" in w for w in out.packet.details["warnings"])


async def test_contact_trap_form_waits_without_sending(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/contact", PROFILE)
        assert out.status == "waiting" and out.packet.details["final_button"] == "Send message"
        assert site.submissions() == []
        done = await runner.resume(run_id, approve(out.packet))
    assert done.status == "done" and site.submissions()[0]["form"] == "contact"


async def test_survey_aria_widgets_filled_and_submitted(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/survey", PROFILE)
        edited = await runner.resume(
            run_id,
            approve(
                out.packet,
                "edit",
                edits={
                    "how_happy_are_you_with_your_current_tool": "4",
                    "what_is_your_role": "Developer",
                    "which_tools_do_you_use_every_day": "Email;Chat apps",
                },
            ),
        )
        done = await runner.resume(run_id, approve(edited.packet))
    assert done.status == "done", done.error
    data = site.submissions()[0]["data"]
    assert data["satisfaction"] == "4" and data["role"] == "Developer"
    assert data["tools"] == ["Email", "Chat apps"] and data["heard_from"] == "Through a friend."
    assert data["feedback"]


async def test_marketplace_login_three_steps_stops_before_publish(settings: Settings, site) -> None:
    from app.browser.runner import load_batch_rows
    from app.browser.sites import parse_target

    spec = parse_target("site:practice/marketplace", settings).form
    rows = load_batch_rows("config/products.example.csv", spec.data_columns)[:3]
    async with open_form_runner(settings, durable=False) as runner:
        outcomes = [
            await runner.start("site:practice/marketplace", PROFILE, data=row) for row in rows
        ]
        assert [o.status for _, o in outcomes] == ["waiting"] * 3
        assert site.submissions() == []  # not even a draft was saved
        run_id, first = outcomes[0]
        assert first.packet.details["final_button"] == "Publish listing"
        assert first.packet.details["steps"] == 4
        done = await runner.resume(run_id, approve(first.packet))
    assert done.status == "done"
    [row] = site.submissions()
    assert row["form"] == "listing" and row["data"]["title"] == "Practice desk lamp"
    assert row["data"]["photos"][0]["name"] == "product.png"
    events = AuditLog(settings.data_dir).read(run_id)
    assert any(e.action == "login" and e.outcome == "ok" for e in events)
    assert "practice-only-pass" not in AuditLog(settings.data_dir).path(run_id).read_text()


async def test_unattended_form_run_only_proposes(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE, mode="unattended")
    assert out.status == "done" and out.values["outcome"] == "deferred"
    assert site.submissions() == []


async def test_host_not_allowed_fails_run_and_is_audited(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("https://example.org/form", PROFILE)
    assert out.status == "failed" and "HostNotAllowed" in out.error
    assert AuditLog(settings.data_dir).count(run_id, "blocked_navigation") == 1


async def test_second_approval_that_loses_the_claim_never_clicks(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        # Another process already claimed the run for submitting.
        assert runner.context.index.claim(run_id, RunStatus.WAITING_REVIEW, RunStatus.SUBMITTING)
        done = await runner.resume(run_id, approve(out.packet))
    assert done.values["outcome"] == "duplicate_approval"
    assert site.submissions() == []
    assert RunIndex.in_data_dir(settings.data_dir).get(run_id).status is RunStatus.SUBMITTING


async def test_failure_after_click_is_needs_human_check(settings: Settings, site) -> None:
    async with open_form_runner(settings, durable=False, crash_after_click=True) as runner:
        run_id, out = await runner.start("site:practice/job_apply", PROFILE)
        crashed = await runner.resume(run_id, approve(out.packet))
        assert crashed.status == "failed"
        assert runner.context.browser.ready_for is None
        # Not "failed": the click happened, so a person must check the site first.
        index = RunIndex.in_data_dir(settings.data_dir)
        assert index.get(run_id).status is RunStatus.NEEDS_HUMAN_CHECK
        # Carrying on in the same process must not click again either.
        again = await runner.continue_run(run_id)
    assert again.values["outcome"] == "needs_human_check"
    assert len(site.submissions()) == 1


async def test_seller_password_leaks_nowhere(settings: Settings, site, capsys) -> None:
    from app.runtime.logging import configure_logging

    configure_logging("DEBUG", "json")
    # durable=True: the SQLite checkpoint is on disk and gets checked too.
    async with open_form_runner(settings, durable=True) as runner:
        run_id, out = await runner.start("site:practice/marketplace", PROFILE)
    assert out.status == "waiting"
    paths = RunPaths.for_run(settings.data_dir, run_id)
    for path in [paths.review_html, paths.review_json, paths.audit]:
        assert "practice-only-pass" not in path.read_text(), path
    # Every file the run left: checkpoints, run index, screenshots, saved cookies, review files.
    leaked = [
        str(p.relative_to(settings.data_dir))
        for p in Path(settings.data_dir).rglob("*")
        if p.is_file() and b"practice-only-pass" in p.read_bytes()
    ]
    assert not leaked, leaked
    assert any(p.suffix == ".png" for p in paths.root.rglob("*"))  # screenshots were checked
    logs = capsys.readouterr()
    assert "practice-only-pass" not in logs.err + logs.out
