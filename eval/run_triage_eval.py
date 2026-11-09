"""Triage eval: the bot's answers on the 30 sandbox issues against hand-written gold labels.

Metrics: category accuracy, priority exact and within one level, label F1 (type + priority
labels), needs-info precision/recall, duplicate precision/recall, security escalation recall
(must be 100%), unclassified rate.

Offline by default (LLM_PROVIDER=fake: a keyword classifier, a floor to beat, not a claim about a
real model). `--live` uses your configured provider (Groq) and makes about 6 LLM calls (about
12 more with `--steadfast`).

Steadfast before/after (`--steadfast`): the same run with `max_attempts=1` vs `3`, reporting how
often the model's output was invalid and how many issues ended up unclassified. Offline it uses a
"flaky" provider that breaks every Nth reply on purpose, to show the mechanism; only `--live`
numbers say anything about a real model.

Run: `python -m eval.run_triage_eval [--live] [--steadfast]` -> eval/results/triage-<date>.md/json
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from steadfast import LLMResponse

from app.config import Settings, get_settings
from app.runtime.llm import FakeProvider, make_llm
from app.triage.classify import classify_all, merge
from app.triage.duplicates import candidates
from app.triage.models import Issue, TriageResult
from app.triage.rubric import Rubric, apply_rules
from app.triage.sandbox import as_github, load_sandbox
from tests.triage.helpers import NOW

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "eval" / "triage" / "gold.yaml"
RESULTS = ROOT / "eval" / "results"
PRIORITY_ORDER = ["P0", "P1", "P2", "P3"]


class FlakyProvider(FakeProvider):
    """Breaks every `every`-th reply (cuts the JSON in half), to exercise Steadfast's retries."""

    def __init__(self, every: int = 3) -> None:
        super().__init__()
        self.every = every
        self.count = 0
        self.invalid = 0

    async def chat(self, messages, tools=None, tool_choice=None, **params) -> LLMResponse:
        response = await super().chat(messages, tools=tools, tool_choice=tool_choice, **params)
        self.count += 1
        if self.count % self.every == 1:
            self.invalid += 1
            response.text = response.text[: len(response.text) // 2]
        return response


async def triage_once(llm, max_attempts: int = 3) -> tuple[list[TriageResult], list[int]]:
    rubric = Rubric.load()
    issues = [Issue.from_github(i) for i in as_github(load_sandbox(), NOW)]
    # Issue 27 was triaged at the last run and not updated since; the rules skip it.
    last_run = NOW
    rules = {
        i.number: apply_rules(i, rubric, NOW, last_run=last_run if "triaged" in i.labels else None)
        for i in issues
    }
    todo = [i for i in issues if not rules[i.number].skip]
    cands = {i.number: candidates(i, issues) for i in issues}
    llm_results, failed = await classify_all(llm, todo, cands, rubric, max_attempts=max_attempts)
    merged = [merge(i, rules[i.number], llm_results.get(i.number), rubric) for i in issues]
    return merged, failed


def _ratio(a: int, b: int) -> float:
    return round(a / b, 4) if b else 1.0


def score(results: list[TriageResult], gold: dict[int, dict[str, Any]]) -> dict[str, Any]:
    by = {r.number: r for r in results}
    graded = [n for n, g in gold.items() if not g.get("skip")]
    cat_ok = sum(by[n].category == gold[n]["category"] for n in graded)
    pri_ok = sum(by[n].priority == gold[n]["priority"] for n in graded)
    pri_near = sum(
        by[n].priority is not None
        and abs(PRIORITY_ORDER.index(by[n].priority) - PRIORITY_ORDER.index(gold[n]["priority"]))
        <= 1
        for n in graded
    )
    tp = fp = fn = 0
    for n in graded:
        want = {f"type:{gold[n]['category']}", f"priority:{gold[n]['priority']}"}
        got = {lab for lab in by[n].labels if lab.startswith(("type:", "priority:"))}
        tp, fp, fn = tp + len(want & got), fp + len(got - want), fn + len(want - got)
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    ni_tp = sum(by[n].needs_info and gold[n]["needs_info"] for n in graded)
    ni_fp = sum(by[n].needs_info and not gold[n]["needs_info"] for n in graded)
    ni_fn = sum(not by[n].needs_info and gold[n]["needs_info"] for n in graded)
    d_tp = sum(
        by[n].duplicate_of is not None and by[n].duplicate_of == gold[n]["duplicate_of"]
        for n in graded
    )
    d_fp = sum(
        by[n].duplicate_of is not None and by[n].duplicate_of != gold[n]["duplicate_of"]
        for n in graded
    )
    d_fn = sum(
        gold[n]["duplicate_of"] is not None and by[n].duplicate_of != gold[n]["duplicate_of"]
        for n in graded
    )
    security = [n for n in graded if gold[n]["category"] == "security"]
    skipped_ok = sum(by[n].skipped for n, g in gold.items() if g.get("skip"))
    return {
        "issues": len(graded),
        "category_accuracy": _ratio(cat_ok, len(graded)),
        "priority_exact": _ratio(pri_ok, len(graded)),
        "priority_within_one": _ratio(pri_near, len(graded)),
        "label_f1": round(2 * precision * recall / (precision + recall), 4)
        if precision + recall
        else 0.0,
        "needs_info_precision": _ratio(ni_tp, ni_tp + ni_fp),
        "needs_info_recall": _ratio(ni_tp, ni_tp + ni_fn),
        "duplicate_precision": _ratio(d_tp, d_tp + d_fp),
        "duplicate_recall": _ratio(d_tp, d_tp + d_fn),
        "security_escalation_recall": _ratio(sum(by[n].escalate for n in security), len(security)),
        "unclassified_rate": _ratio(sum(by[n].unclassified for n in graded), len(graded)),
        "already_triaged_skipped": _ratio(
            skipped_ok, sum(1 for g in gold.values() if g.get("skip"))
        ),
    }


def load_gold() -> dict[int, dict[str, Any]]:
    return {int(k): v for k, v in yaml.safe_load(GOLD.read_text())["gold"].items()}


async def run(live: bool, steadfast: bool) -> dict[str, Any]:
    settings = get_settings() if live else Settings(_env_file=None)
    if live and settings.llm_provider == "fake":
        raise SystemExit("--live needs a real model: set LLM_PROVIDER=groq and GROQ_API_KEY")
    llm = make_llm(settings)
    results, _ = await triage_once(llm)
    report: dict[str, Any] = {
        "date": date.today().isoformat(),
        "provider": llm.provider,
        "model": llm.model,
        "tokens": llm.usage.total_tokens,
        "metrics": score(results, load_gold()),
    }
    if steadfast:
        report["steadfast"] = {}
        for attempts in (1, 3):
            if live:
                llm_n = make_llm(settings)
            else:
                provider = FlakyProvider(every=3)
                llm_n = make_llm(Settings(_env_file=None), provider=provider)
            res, failed = await triage_once(llm_n, max_attempts=attempts)
            report["steadfast"][f"max_attempts_{attempts}"] = {
                "unclassified_issues": len(failed),
                "unclassified_rate": score(res, load_gold())["unclassified_rate"],
                "category_accuracy": score(res, load_gold())["category_accuracy"],
                "simulated_invalid_replies": None if live else provider.invalid,
            }
    return report


def write(report: dict[str, Any], live: bool) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = "live" if live else "offline"
    base = RESULTS / f"triage-{tag}-{report['date']}"
    m = report["metrics"]
    lines = [
        f"# Triage eval ({tag}), {report['date']}",
        "",
        f"Provider `{report['provider']}`, model `{report['model']}`, {m['issues']} graded issues "
        f"(30 sandbox issues, one already triaged), {report['tokens']} tokens.",
        "",
    ]
    if not live:
        lines += [
            "Offline means the fake keyword classifier, not a real model: it is the floor any real",
            "model should beat, and it proves the pipeline end to end. The deterministic rules",
            "(security, needs-info, stale) run for real either way.",
            "",
        ]
    lines += [
        "| Metric | Value |",
        "|---|---|",
        *[f"| {k} | {v:.2%} |" if isinstance(v, float) else f"| {k} | {v} |" for k, v in m.items()],
        "",
    ]
    if "steadfast" in report:
        lines += [
            "## Steadfast before/after",
            "",
            "| | unclassified issues | category accuracy |",
            "|---|---|---|",
        ]
        for k, v in report["steadfast"].items():
            lines.append(f"| {k} | {v['unclassified_issues']} | {v['category_accuracy']:.2%} |")
        if not live:
            lines += [
                "",
                "Offline: a provider that breaks every 3rd reply on purpose. With one attempt,",
                "each broken reply loses issues; with three, Steadfast feeds the error back and",
                "the retry succeeds. This shows the mechanism, not a real model's error rate.",
            ]
        lines.append("")
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    return base.with_suffix(".md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Triage eval against gold labels")
    parser.add_argument(
        "--live",
        action="store_true",
        help="use the configured real provider (about 6 calls; about 12 more with --steadfast)",
    )
    parser.add_argument("--steadfast", action="store_true", help="also compare max_attempts 1 vs 3")
    args = parser.parse_args()
    report = asyncio.run(run(args.live, args.steadfast))
    path = write(report, args.live)
    for k, v in report["metrics"].items():
        print(f"{k:28} {v:.2%}" if isinstance(v, float) else f"{k:28} {v}")
    print(f"report: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
