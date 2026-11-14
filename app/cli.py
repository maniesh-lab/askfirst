"""Command-line tools, run from the repo root as `python -m app.cli <command>`.

- `version`: print the app name and installed version
- `runs list [--status S] [--kind K]`, `runs show <run_id>`: the run index
- `audit <run_id>`: a run's audit timeline
- `detect <url|site:form>`: list a page's form fields and buttons (writes JSON too)
- `plan <url|site:form> [--profile]`: show how each field would be filled (fills nothing)
- `fill <url|site:form> [--profile] [--headed] [--wait]`: fill a form and stop for review
- `fill-batch site:practice/marketplace --data <csv>`: one form run per CSV row
- `review list | show <run_id>`: runs waiting for a person, and what one will do
- `review approve <run_id>... [--plan-hash H] [--all-clean [--yes]] [--note N] [--reviewer R]`,
  `review reject <run_id> --reason R [--reviewer R]`,
  `review edit <run_id> --set KEY=VALUE [--set ...] [--reviewer R]`: decide
- `runs continue <run_id>`: carry on a run that stopped mid-way (after a crash)
- `triage run --repo owner/name [--since 7d] [--dry-run] [--read-only] [--unattended |
  --interactive] [--mode propose|apply-labels] [--force] [--output-dir DIR]`: triage GitHub
  issues

The real work lives in other modules; this file only parses arguments and prints results.
"""

import argparse
import asyncio
import getpass
import json
import re
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

from app.browser.schema import FormStep
from app.config import Settings, get_settings
from app.runtime.audit import AuditLog
from app.runtime.logging import configure_logging, register_secrets
from app.runtime.runs import RunIndex, RunPaths, RunStatus


def cmd_version(settings: Settings, _args: argparse.Namespace) -> int:
    print(f"{settings.app_name} {version('askfirst')}")
    return 0


CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(value: object) -> str:
    """Text from pages, issues and the LLM, safe to print: a terminal escape code in a page title
    or an issue could otherwise rewrite or hide what the reviewer reads ("ESCALATE" -> "fine")."""
    return CONTROL_CHARS.sub(" ", str(value))


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    rows = [[plain(c) for c in row] for row in rows]
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows, strict=False)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)))


def cmd_runs_list(settings: Settings, args: argparse.Namespace) -> int:
    status = RunStatus(args.status) if args.status else None
    runs = RunIndex.in_data_dir(settings.data_dir).list(status=status, kind=args.kind)
    if not runs:
        print("no runs")
        return 0
    rows = [
        [r.id, r.status.value, r.updated_at.strftime("%Y-%m-%d %H:%M"), r.title[:60]] for r in runs
    ]
    print_table(rows, ["run", "status", "updated (UTC)", "title"])
    return 0


def cmd_runs_show(settings: Settings, args: argparse.Namespace) -> int:
    run = RunIndex.in_data_dir(settings.data_dir).get(args.run_id)
    if run is None:
        print(f"no run {args.run_id}", file=sys.stderr)
        return 1
    paths = RunPaths.for_run(settings.data_dir, run.id)
    print(json.dumps(run.model_dump(mode="json"), indent=2))
    print(f"folder: {paths.root}")
    for name, path in [("review", paths.review_html), ("result", paths.result)]:
        if path.exists():
            print(f"{name}: {path}")
    return 0


def cmd_audit(settings: Settings, args: argparse.Namespace) -> int:
    events = AuditLog(settings.data_dir).read(args.run_id)
    if not events:
        print(f"no audit events for {args.run_id}", file=sys.stderr)
        return 1
    for e in events:
        detail = json.dumps(e.detail, ensure_ascii=False, default=str)
        if len(detail) > 140:
            detail = detail[:137] + "..."
        mark = "" if e.outcome == "ok" else f" [{e.outcome}]"
        print(f"{e.ts.strftime('%H:%M:%S')}  {e.actor:<22} {e.action}{mark}  {detail}")
    return 0


async def _detect_target(settings: Settings, target_text: str) -> tuple[FormStep, object]:
    from app.browser.detect import detect
    from app.browser.session import BrowserSession
    from app.browser.sites import parse_target

    target = parse_target(target_text, settings)
    async with BrowserSession(settings) as session:
        page = await session.new_page()
        await session.goto(page, target.url)
        step = await detect(page)
    return step, target


def _detect_or_refuse(settings: Settings, target_text: str) -> tuple[FormStep, object] | None:
    """Detect, or print why the host was refused (a refusal is an answer, not a crash)."""
    from app.browser.session import HostNotAllowed

    try:
        return asyncio.run(_detect_target(settings, target_text))
    except HostNotAllowed as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return None


def _write_json(settings: Settings, folder: str, name: str, data: object) -> str:
    out = settings.data_dir / folder
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(path)


def _slug(text: str) -> str:
    from app.browser.schema import slugify

    return slugify(text.replace("site:", ""), 60) or "page"


def cmd_detect(settings: Settings, args: argparse.Namespace) -> int:
    found = _detect_or_refuse(settings, args.target)
    if found is None:
        return 2
    step, _target = found
    rows = [
        [
            f.key,
            f.kind,
            f.label[:40],
            "yes" if f.required else "",
            "yes" if f.fillable else ("honeypot" if f.honeypot else "no"),
            ", ".join(o.text or o.value for o in f.options)[:40],
        ]
        for f in step.fields
    ]
    print(plain(f"{step.title}  ({step.url})"))
    print_table(rows, ["key", "kind", "label", "required", "fillable", "options"])
    print()
    print_table(
        [[b.kind, b.text, "yes" if b.visible else "no"] for b in step.buttons],
        ["button", "text", "visible"],
    )
    path = _write_json(settings, "detect", _slug(args.target), step.model_dump(mode="json"))
    print(f"\nschema hash {step.schema_hash[:12]}, saved to {path}")
    return 0


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    from app.browser.mapping import Profile, map_step

    found = _detect_or_refuse(settings, args.target)
    if found is None:
        return 2
    step, target = found
    profile = Profile.load(args.profile)
    overrides = target.form.overrides if target.form else {}
    mappings = map_step(step, profile, overrides=overrides)
    rows = []
    for f, m in zip(step.fields, mappings, strict=True):
        shown = (
            "***"
            if f.kind == "password"
            else json.dumps(m.value, ensure_ascii=False)
            if m.value is not None
            else ""
        )
        rows.append([f.key, m.status, shown[:40], m.source or m.reason])
    print_table(rows, ["field", "status", "value", "source / reason"])
    counts = {
        s: sum(1 for m in mappings if m.status == s)
        for s in ("filled", "needs_draft", "needs_human", "skipped")
    }
    print("\n" + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    _write_json(
        settings, "detect", _slug(args.target) + ".plan", [m.model_dump() for m in mappings]
    )
    return 0


def _print_outcome(settings: Settings, run_id: str, outcome) -> None:
    paths = RunPaths.for_run(settings.data_dir, run_id)
    print(f"run {run_id}: {outcome.status}")
    if outcome.status == "waiting" and outcome.packet is not None:
        c = outcome.packet.details.get("counts", {})
        if c:
            print(
                f"  {c['filled']} filled ({c['drafted']} drafted), {c['needs_human']} need you, "
                f"{c['flagged']} flagged"
            )
        for w in outcome.packet.details.get("warnings", []):
            print(f"  warning: {w}")
        if paths.review_html.exists():
            print(f"  review: {paths.review_html}")
        print(f"  next: python -m app.cli review approve {run_id}  (or reject / edit)")
    elif outcome.status == "done":
        result = outcome.values.get("result") or {}
        print(f"  outcome: {outcome.values.get('outcome')}")
        if result.get("confirmation_id"):
            print(f"  confirmation: {result['confirmation_id']}")
    elif outcome.error:
        print(f"  error: {outcome.error}")
    run = RunIndex.in_data_dir(settings.data_dir).get(run_id)
    if run is not None and run.status is RunStatus.NEEDS_HUMAN_CHECK:
        print("  the submit may have gone through: check the site before doing anything else")


def _exit_code(settings: Settings, run_id: str, outcome) -> int:
    """1 when the run went wrong. A run that gave up, or whose submit failed or may have gone
    through, still ends the graph normally, so the run index is the one to ask."""
    if outcome.status == "failed":
        return 1
    values = outcome.values or {}
    if values.get("post_failed") or values.get("llm_down"):
        return 1  # triage after approve/continue: the digest or the window wasn't saved
    run = RunIndex.in_data_dir(settings.data_dir).get(run_id)
    bad = {RunStatus.FAILED, RunStatus.NEEDS_HUMAN_CHECK}
    return 1 if run is not None and run.status in bad else 0


def _ask_decision(packet) -> object:
    from app.runtime.approval import Decision

    print(
        f"\nThe agent will click {packet.details.get('final_button')!r}. Approve? [y/N/r=reject] ",
        end="",
    )
    answer = input().strip().lower()
    if answer == "y":
        return Decision(choice="approve", reviewer=getpass.getuser(), plan_hash=packet.plan_hash)
    if answer == "r":
        reason = input("reason: ").strip() or "rejected in the terminal"
        return Decision(
            choice="reject", reviewer=getpass.getuser(), reason=reason, plan_hash=packet.plan_hash
        )
    return None


async def _fill(settings: Settings, args: argparse.Namespace) -> int:
    from app.browser.runner import open_form_runner

    headless = False if args.headed else None
    async with open_form_runner(settings, headless=headless, keep_browser_open=args.wait) as runner:
        run_id, outcome = await runner.start(args.target, args.profile)
        _print_outcome(settings, run_id, outcome)
        if args.wait and outcome.status == "waiting":
            decision = _ask_decision(outcome.packet)
            if decision is None:
                print("left waiting; decide later with `review approve/reject`")
                return 0
            outcome = await runner.resume(run_id, decision)
            _print_outcome(settings, run_id, outcome)
    return _exit_code(settings, run_id, outcome)


def cmd_fill(settings: Settings, args: argparse.Namespace) -> int:
    return asyncio.run(_fill(settings, args))


async def _fill_batch(settings: Settings, args: argparse.Namespace) -> int:
    from app.browser.runner import load_batch_rows, open_form_runner
    from app.browser.sites import parse_target

    target = parse_target(args.target, settings)
    columns = target.form.data_columns if target.form else {}
    rows = load_batch_rows(args.data, columns)
    failed = 0
    async with open_form_runner(settings) as runner:
        for n, data in enumerate(rows, start=1):
            print(f"row {n}/{len(rows)}")
            run_id, outcome = await runner.start(args.target, args.profile, data=data)
            _print_outcome(settings, run_id, outcome)
            failed += _exit_code(settings, run_id, outcome)
    return 1 if failed else 0


def cmd_fill_batch(settings: Settings, args: argparse.Namespace) -> int:
    return asyncio.run(_fill_batch(settings, args))


def _runner_for(run_id: str):
    from app.runtime.runs import run_kind

    kind = run_kind(run_id)
    if kind == "form":
        from app.browser.runner import open_form_runner

        return open_form_runner
    if kind == "triage":
        from app.triage.runner import open_triage_runner

        return open_triage_runner
    raise SystemExit(f"don't know how to resume a {kind!r} run")


async def _waiting(settings: Settings, run_id: str):
    from app.runtime import graphs

    async with _runner_for(run_id)(settings) as runner:
        return await graphs.waiting_packet(runner.graph, run_id)


def cmd_review_list(settings: Settings, _args: argparse.Namespace) -> int:
    runs = RunIndex.in_data_dir(settings.data_dir).list(status=RunStatus.WAITING_REVIEW, limit=200)
    if not runs:
        print("nothing is waiting for review")
        return 0
    rows = []
    for r in runs:
        review = RunPaths.for_run(settings.data_dir, r.id).review_json
        flagged = ""
        if review.exists():
            counts = json.loads(review.read_text()).get("details", {}).get("counts", {})
            flagged = str(counts.get("flagged", ""))
        rows.append([r.id, r.updated_at.strftime("%Y-%m-%d %H:%M"), flagged, r.title[:60]])
    print_table(rows, ["run", "waiting since (UTC)", "flagged", "title"])
    return 0


def cmd_review_show(settings: Settings, args: argparse.Namespace) -> int:
    packet = asyncio.run(_waiting(settings, args.run_id))
    if packet is None:
        print(f"{args.run_id} is not waiting for review", file=sys.stderr)
        return 1
    print(plain(packet.title))
    for action in packet.actions:
        print(f"  will do: {action.summary} [{action.risk.value}]")
    rows = packet.details.get("rows")
    if rows:
        table = []
        for r in rows:
            value = r["value"] if r["status"] == "filled" else f"<{r['status']}>"
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            table.append(
                [
                    r["key"][:32],
                    str(value).replace("\n", " ")[:50],
                    r["source"][:28],
                    " ".join(r["flags"]),
                ]
            )
        print_table(table, ["field", "value", "source", "flags"])
    for w in packet.details.get("warnings", []):
        print(f"warning: {w}")
    review = RunPaths.for_run(settings.data_dir, args.run_id).review_html
    if review.exists():
        print(f"\nfull report: {review}")
    print(f"plan hash: {packet.plan_hash[:16]}")
    pin = packet.plan_hash[:16]
    print(f"approve exactly this: python -m app.cli review approve {args.run_id} --plan-hash {pin}")
    return 0


async def _decide(
    settings: Settings,
    run_id: str,
    make,
    *,
    expect_hash: str | None = None,
    require_clean: bool = False,
) -> int:
    """Resume a waiting run with a decision. `expect_hash` pins the plan the person saw: if the
    waiting plan's hash doesn't start with it (the run was edited or re-planned since), refuse."""
    from app.browser.review import is_clean
    from app.runtime import graphs
    from app.runtime.approval import StaleDecision

    async with _runner_for(run_id)(settings) as runner:
        packet = await graphs.waiting_packet(runner.graph, run_id)
        if packet is None:
            print(f"{run_id} is not waiting for review", file=sys.stderr)
            return 1
        if expect_hash and not packet.plan_hash.startswith(expect_hash):
            print(
                f"refused: {run_id} changed since you looked (plan hash differs); review it again",
                file=sys.stderr,
            )
            return 1
        if require_clean and not is_clean(packet):
            print(f"refused: {run_id} is no longer clean; review it by hand", file=sys.stderr)
            return 1
        try:
            outcome = await runner.resume(run_id, make(packet))
        except StaleDecision as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        _print_outcome(settings, run_id, outcome)
        return _exit_code(settings, run_id, outcome)


def cmd_review_approve(settings: Settings, args: argparse.Namespace) -> int:
    from app.browser.review import is_clean
    from app.runtime.approval import Decision

    reviewer = args.reviewer or getpass.getuser()
    pins: dict[str, str | None]
    if args.all_clean:
        candidates = RunIndex.in_data_dir(settings.data_dir).list(
            status=RunStatus.WAITING_REVIEW, limit=200
        )
        clean: dict[str, str] = {}
        for r in candidates:
            packet = asyncio.run(_waiting(settings, r.id))
            if packet is not None and packet.kind == "form" and is_clean(packet):
                clean[r.id] = packet.plan_hash
        if not clean:
            print("no clean runs (zero flags, zero warnings, nothing left for a person)")
            return 0
        print("These runs have no flags and will be submitted:")
        for rid, plan_hash in clean.items():
            print(f"  {rid}  (plan {plan_hash[:12]})")
        if not args.yes:
            print("Approve all of them? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("nothing approved")
                return 0
        # Approve exactly the plans listed above, and only if each is still clean.
        pins = dict(clean)
    else:
        if args.plan_hash and len(args.run_ids) != 1:
            print("--plan-hash goes with exactly one run id", file=sys.stderr)
            return 2
        if args.plan_hash is not None and len(args.plan_hash) < 8:
            # A prefix this short would match almost any plan.
            print("--plan-hash needs at least 8 characters", file=sys.stderr)
            return 2
        pins = {rid: args.plan_hash for rid in args.run_ids}
    if not pins:
        print("give run ids, or --all-clean", file=sys.stderr)
        return 2

    def make(packet):
        return Decision(
            choice="approve", reviewer=reviewer, reason=args.note, plan_hash=packet.plan_hash
        )

    codes = [
        asyncio.run(_decide(settings, rid, make, expect_hash=pin, require_clean=args.all_clean))
        for rid, pin in pins.items()
    ]
    return max(codes)


def cmd_review_reject(settings: Settings, args: argparse.Namespace) -> int:
    from app.runtime.approval import Decision

    reviewer = args.reviewer or getpass.getuser()
    return asyncio.run(
        _decide(
            settings,
            args.run_id,
            lambda p: Decision(
                choice="reject", reviewer=reviewer, reason=args.reason, plan_hash=p.plan_hash
            ),
        )
    )


def cmd_review_edit(settings: Settings, args: argparse.Namespace) -> int:
    from app.runtime.approval import Decision

    edits = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            print(f"--set needs key=value, got {item!r}", file=sys.stderr)
            return 2
        edits[key.strip()] = value
    reviewer = args.reviewer or getpass.getuser()
    return asyncio.run(
        _decide(
            settings,
            args.run_id,
            lambda p: Decision(
                choice="edit", reviewer=reviewer, edits=edits, plan_hash=p.plan_hash
            ),
        )
    )


def cmd_runs_continue(settings: Settings, args: argparse.Namespace) -> int:
    if RunIndex.in_data_dir(settings.data_dir).get(args.run_id) is None:
        print(f"no run {args.run_id}", file=sys.stderr)
        return 2

    async def go() -> int:
        async with _runner_for(args.run_id)(settings) as runner:
            outcome = await runner.continue_run(args.run_id)
            _print_outcome(settings, args.run_id, outcome)
            return _exit_code(settings, args.run_id, outcome)

    return asyncio.run(go())


def print_triage_table(results: list[dict]) -> None:
    rows = []
    for r in results:
        flags = []
        if r.get("escalate"):
            flags.append("ESCALATE")
        if r.get("needs_info"):
            flags.append("needs-info")
        if r.get("duplicate_of"):
            flags.append(f"dup of #{r['duplicate_of']}")
        if r.get("stale"):
            flags.append("stale")
        if r.get("unclassified"):
            flags.append("unclassified")
        if r.get("skipped"):
            flags.append("skipped")
        rows.append(
            [
                f"#{r['number']}",
                r.get("priority") or "-",
                r.get("category") or "-",
                " ".join(flags),
                r["title"][:50],
            ]
        )
    print_table(rows, ["issue", "prio", "category", "flags", "title"])


def _actions_url() -> str | None:
    """Link to this GitHub Actions run, when running in one."""
    import os

    server, repo, run = (
        os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


def _write_outputs(out: Path, run_root: Path, values: dict) -> None:
    """Copy the digest and audit log for the workflow's step summary and artifact.

    Both are public in a public repo. When the triaged repo is private (or its visibility is
    unknown), only a counts-only summary is written, so no titles or links leak.
    """
    import shutil

    from app.triage.digest import render_private_summary
    from app.triage.models import Digest

    out.mkdir(parents=True, exist_ok=True)
    if values.get("repo_private", True):
        if values.get("digest"):
            summary = render_private_summary(Digest.model_validate(values["digest"]))
            (out / "digest.md").write_text(summary, encoding="utf-8")
        print("the triaged repo is private (or unknown): output dir gets a counts-only summary")
        return
    for name in ("digest.md", "digest.json", "audit.jsonl"):
        if (run_root / name).exists():
            shutil.copy(run_root / name, out / name)


async def _triage(settings: Settings, args: argparse.Namespace) -> int:
    from app.triage.runner import open_triage_runner

    repo = args.repo or settings.triage_repo
    if not repo:
        print("give --repo owner/name or set TRIAGE_REPO", file=sys.stderr)
        return 2
    read_only = args.read_only or args.dry_run
    if not read_only and not settings.triage_github_token:
        print(
            "warning: TRIAGE_GITHUB_TOKEN is not set, so this run is read-only: no labels, no "
            "log issue and no saved window (the next run starts from the default window again)",
            file=sys.stderr,
        )
    if not read_only and settings.triage_github_token and not settings.discord_webhook_url:
        # Without a webhook the digest is only logged, but the run would still save its window
        # in the log issue, so the next run would skip these issues and nobody sees the digest.
        # The workflow's "Check configuration" step refuses this too.
        print(
            "DISCORD_WEBHOOK_URL is not set: the digest would only be logged, yet this run would "
            "save its window in the repo and the next run would skip these issues. Set the "
            "webhook, or add --dry-run to see the digest without writing anything.",
            file=sys.stderr,
        )
        return 2
    async with open_triage_runner(
        settings, read_only=read_only, actions_url=_actions_url()
    ) as runner:
        run_id, outcome = await runner.start(
            repo,
            since=args.since,
            dry_run=args.dry_run,
            read_only=read_only,
            force=args.force,
            mode="interactive" if args.interactive else "unattended",
            write_mode=args.mode or settings.triage_mode,
        )
    print(f"run {run_id}: {outcome.status}")
    paths = RunPaths.for_run(settings.data_dir, run_id)
    if args.output_dir:
        _write_outputs(Path(args.output_dir), paths.root, outcome.values)
    if outcome.status == "failed":
        print(f"  error: {outcome.error}")
        return 1
    results = outcome.values.get("results", [])
    if outcome.values.get("repo_private", True):
        # This output is a public Actions log when the workflow runs in a public repo.
        print(f"{len(results)} issues triaged (private repo: titles are not printed)")
    else:
        print_triage_table(results)
    if outcome.values.get("post_failed"):
        print("\nthe digest could not be posted to Discord (see the log); no state was saved")
        return 1
    if outcome.values.get("llm_down"):
        print("\nno issue could be classified (is the LLM key valid?); no state was saved")
        return 1
    if outcome.status == "waiting":
        print(
            f"\n{len(outcome.packet.actions)} proposals wait for you: "
            f"python -m app.cli review show {run_id}"
        )
    elif (paths.root / "digest.md").exists():
        print(f"\ndigest: {paths.root / 'digest.md'}")
    return 0


def cmd_triage_run(settings: Settings, args: argparse.Namespace) -> int:
    return asyncio.run(_triage(settings, args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("version", help="print the app name and version").set_defaults(
        handler=cmd_version
    )

    runs = commands.add_parser("runs", help="list and show runs").add_subparsers(
        dest="runs_command", required=True
    )
    runs_list = runs.add_parser("list", help="list recent runs")
    runs_list.add_argument("--status", choices=[s.value for s in RunStatus])
    runs_list.add_argument("--kind", help="form or triage")
    runs_list.set_defaults(handler=cmd_runs_list)
    runs_show = runs.add_parser("show", help="show one run")
    runs_show.add_argument("run_id")
    runs_show.set_defaults(handler=cmd_runs_show)
    runs_cont = runs.add_parser("continue", help="carry on a run that stopped mid-way")
    runs_cont.add_argument("run_id")
    runs_cont.set_defaults(handler=cmd_runs_continue)

    audit = commands.add_parser("audit", help="print a run's audit timeline")
    audit.add_argument("run_id")
    audit.set_defaults(handler=cmd_audit)

    detect = commands.add_parser("detect", help="list a page's form fields and buttons")
    detect.add_argument("target", help="a URL or site:<site>/<form>, e.g. site:practice/job_apply")
    detect.set_defaults(handler=cmd_detect)

    plan = commands.add_parser("plan", help="show how each field would be filled (dry run)")
    plan.add_argument("target")
    plan.add_argument("--profile", default="profiles/example.yaml")
    plan.set_defaults(handler=cmd_plan)

    fill = commands.add_parser("fill", help="fill a form and stop for review before submitting")
    fill.add_argument("target")
    fill.add_argument("--profile", default="profiles/example.yaml")
    fill.add_argument("--headed", action="store_true", help="show the browser window")
    fill.add_argument("--wait", action="store_true", help="ask approve/reject in this terminal")
    fill.set_defaults(handler=cmd_fill)

    batch = commands.add_parser("fill-batch", help="one form run per row of a CSV file")
    batch.add_argument("target")
    batch.add_argument("--data", required=True, help="CSV file, e.g. config/products.example.csv")
    batch.add_argument("--profile", default="profiles/example.yaml")
    batch.set_defaults(handler=cmd_fill_batch)

    review = commands.add_parser("review", help="decide on runs waiting for a person")
    rsub = review.add_subparsers(dest="review_command", required=True)
    rsub.add_parser("list", help="runs waiting for review").set_defaults(handler=cmd_review_list)
    show = rsub.add_parser("show", help="what a run will do")
    show.add_argument("run_id")
    show.set_defaults(handler=cmd_review_show)
    appr = rsub.add_parser("approve", help="approve one or more runs (they submit now)")
    appr.add_argument("run_ids", nargs="*")
    appr.add_argument("--all-clean", action="store_true", help="every form run with zero flags")
    appr.add_argument("--yes", action="store_true", help="skip the y/N question for --all-clean")
    appr.add_argument(
        "--plan-hash",
        help="the plan hash (or its start) shown by `review show`; refuses if it changed",
    )
    appr.add_argument("--note")
    appr.add_argument("--reviewer")
    appr.set_defaults(handler=cmd_review_approve)
    rej = rsub.add_parser("reject", help="reject a run (nothing is submitted)")
    rej.add_argument("run_id")
    rej.add_argument("--reason", required=True)
    rej.add_argument("--reviewer")
    rej.set_defaults(handler=cmd_review_reject)
    edit = rsub.add_parser("edit", help="change values, then review again")
    edit.add_argument("run_id")
    edit.add_argument("--set", action="append", required=True, metavar="KEY=VALUE")
    edit.add_argument("--reviewer")
    edit.set_defaults(handler=cmd_review_edit)

    triage = commands.add_parser("triage", help="the GitHub issue triage bot")
    tsub = triage.add_subparsers(dest="triage_command", required=True)
    trun = tsub.add_parser("run", help="triage issues updated in a time window")
    trun.add_argument("--repo", help="owner/name; default TRIAGE_REPO")
    trun.add_argument(
        "--since", help="window start: 7d, 36h or an ISO date (default: last run or 7d)"
    )
    trun.add_argument(
        "--dry-run", action="store_true", help="read and classify only; change and post nothing"
    )
    trun.add_argument(
        "--read-only", action="store_true", help="never write to GitHub (any public repo)"
    )
    mode = trun.add_mutually_exclusive_group()
    mode.add_argument(
        "--unattended",
        action="store_true",
        help="cron mode (default): irreversible actions are only proposed",
    )
    mode.add_argument(
        "--interactive", action="store_true", help="proposals wait for `review approve`"
    )
    trun.add_argument(
        "--mode",
        choices=["propose", "apply-labels"],
        help="propose: issues unchanged (only the bot's log issue gets a comment); "
        "apply-labels: also add allowlisted labels. Default TRIAGE_MODE",
    )
    trun.add_argument("--force", action="store_true", help="post the digest even if already posted")
    trun.add_argument("--output-dir", help="copy digest.md, digest.json and audit.jsonl here")
    trun.set_defaults(handler=cmd_triage_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # API keys, the GitHub token, webhook URLs and passwords: masked wherever they turn up.
    register_secrets(settings.secret_values())
    handler: Callable[[Settings, argparse.Namespace], int] = args.handler
    import yaml
    from playwright.async_api import Error as PlaywrightError

    from app.browser.sites import UnknownForm

    # Typos in a path or form name, or a closed prompt, get one line instead of a traceback.
    try:
        return handler(settings, args)
    except (FileNotFoundError, UnknownForm) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EOFError:
        print("\nno answer (input closed): nothing was decided", file=sys.stderr)
        return 1
    except (ValueError, LookupError, yaml.YAMLError, PlaywrightError) as exc:
        # Bad input (a blank --reason, a broken site YAML), a run decided elsewhere in the
        # meantime, or a site that isn't running: one line, never a traceback. ValueError covers
        # pydantic's validation errors.
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"error: {first}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
