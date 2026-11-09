"""Time a form run and a triage run, offline, and print a Markdown table.

- Form run: the job form on our practice site (started here on a free port), fill to the review
  gate, approve, submit. Headless Chromium, fake LLM.
- Triage run: the 30 sandbox issues (in memory, no GitHub), fake LLM, digest to a fake notifier.

Each is run `--repeat` times (default 3); the table shows the median. The LLM comes from the
environment (LLM_PROVIDER, default fake; the .env file is not read). With the fake LLM the token
counts are word counts, a rough size of the prompts, not a real model's bill.

Run: `python -m scripts.measure_runs [--repeat 3]`
"""

import argparse
import asyncio
import statistics
import tempfile
import time
from pathlib import Path

from app.browser.runner import open_form_runner
from app.config import Settings
from app.runtime.approval import Decision
from app.runtime.logging import configure_logging
from app.runtime.notify import FakeNotifier
from app.triage.runner import open_triage_runner
from practice_site.config import PracticeSettings
from practice_site.server import running_site
from tests.triage.helpers import NOW, sandbox_github


async def form_run(tmp: Path) -> dict[str, float]:
    site = PracticeSettings(_env_file=None, data_dir=tmp / "practice", allow_reset=True)
    with running_site(site) as (base, _app):
        settings = Settings(
            _env_file=None,
            data_dir=tmp / "data",
            practice_base_url=base,
            allowed_hosts="127.0.0.1",
        )
        async with open_form_runner(settings, notifier=FakeNotifier()) as runner:
            started = time.perf_counter()
            run_id, out = await runner.start("site:practice/job_apply", "profiles/example.yaml")
            to_gate = time.perf_counter() - started
            assert out.status == "waiting", out.error
            started = time.perf_counter()
            decision = Decision(choice="approve", reviewer="timing", plan_hash=out.packet.plan_hash)
            done = await runner.resume(run_id, decision)
            to_submit = time.perf_counter() - started
            assert done.values["result"]["status"] == "submitted"
            tokens = runner.context.llm.usage.total_tokens
    return {"to_gate": to_gate, "approve_to_submit": to_submit, "tokens": tokens}


async def triage_run(tmp: Path) -> dict[str, float]:
    settings = Settings(_env_file=None, data_dir=tmp)
    async with open_triage_runner(
        settings, github=sandbox_github(), notifier=FakeNotifier(), now=lambda: NOW
    ) as runner:
        started = time.perf_counter()
        _, out = await runner.start("o/sandbox", since="60d")
        took = time.perf_counter() - started
        assert out.status == "done", out.error
        tokens = runner.context.llm.usage.total_tokens
    return {"total": took, "tokens": tokens, "issues": len(out.values["results"])}


def median(rows: list[dict[str, float]], key: str) -> float:
    return statistics.median(r[key] for r in rows)


async def main_async(repeat: int) -> None:
    forms, triages = [], []
    for _ in range(repeat):
        with tempfile.TemporaryDirectory() as tmp:
            forms.append(await form_run(Path(tmp)))
        with tempfile.TemporaryDirectory() as tmp:
            triages.append(await triage_run(Path(tmp)))
    print(f"| Run | Median of {repeat} | Tokens (fake LLM) |")
    print("|---|---|---|")
    print(
        f"| Form: start to review gate (job form, 12 fields, 2 drafted) | "
        f"{median(forms, 'to_gate'):.1f} s | {median(forms, 'tokens'):.0f} |"
    )
    print(
        f"| Form: approve to submitted (replay + submit) | "
        f"{median(forms, 'approve_to_submit'):.1f} s | (in the row above: whole run) |"
    )
    print(
        f"| Triage: {triages[0]['issues']:.0f} issues, fetch to digest | "
        f"{median(triages, 'total'):.2f} s | {median(triages, 'tokens'):.0f} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Time a form run and a triage run (offline)")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    configure_logging("ERROR", "console")
    asyncio.run(main_async(args.repeat))


if __name__ == "__main__":
    main()
