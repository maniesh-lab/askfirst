"""Detection and mapping eval over every saved form fixture.

For each `tests/fixtures/forms/<name>.html` there is an `eval/forms/<name>.expected.json`
written by hand-checking the page. The eval loads each fixture in Chromium (fake origin, no
network), runs detection and mapping, and compares:

- field recall and precision (fillable fields found vs expected)
- label exact match, kind accuracy, required accuracy (on matched fields)
- mapping accuracy (the profile path, or needs_draft / needs_human)
- hidden and honeypot fields correctly left alone (must be 100%)
- submit button found (exact text)

Run: `python -m eval.run_detection_eval` -> `eval/results/detection-<date>.md` and `.json`.
"""

import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.browser.detect import detect
from app.browser.mapping import Mapping, Profile, map_step
from app.browser.schema import Field, FormStep
from app.browser.session import BrowserSession
from app.config import Settings
from tests.fixture_site import FORMS, HOST, fixture_url, serve_fixtures

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "eval" / "forms"
RESULTS = ROOT / "eval" / "results"
PROFILE = ROOT / "profiles" / "example.yaml"


def field_id(f: Field) -> str:
    return f.name or f.label


def mapping_label(m: Mapping) -> str:
    if m.status != "filled":
        return m.status
    kind, _, path = m.source.partition(":")
    return path or kind


@dataclass
class Counts:
    expected: int = 0
    detected: int = 0
    matched: int = 0
    label_ok: int = 0
    kind_ok: int = 0
    required_ok: int = 0
    mapping_ok: int = 0
    skipped_expected: int = 0
    skipped_ok: int = 0
    submit_expected: int = 0
    submit_ok: int = 0
    misses: list[str] = field(default_factory=list)

    def add(self, other: Counts) -> None:
        for k, v in asdict(other).items():
            if k == "misses":
                self.misses.extend(v)
            else:
                setattr(self, k, getattr(self, k) + v)

    def metrics(self) -> dict[str, float]:
        def ratio(a: int, b: int) -> float:
            return round(a / b, 4) if b else 1.0

        return {
            "field_recall": ratio(self.matched, self.expected),
            "field_precision": ratio(self.matched, self.detected),
            "label_exact": ratio(self.label_ok, self.matched),
            "kind_accuracy": ratio(self.kind_ok, self.matched),
            "required_accuracy": ratio(self.required_ok, self.matched),
            "mapping_accuracy": ratio(self.mapping_ok, self.matched),
            "hidden_skipped": ratio(self.skipped_ok, self.skipped_expected),
            "submit_found": ratio(self.submit_ok, self.submit_expected),
        }


def score(name: str, step: FormStep, mappings: list[Mapping], expected: dict[str, Any]) -> Counts:
    c = Counts()
    by_id = {field_id(f): (f, m) for f, m in zip(step.fields, mappings, strict=True)}
    fillable_ids = {field_id(f) for f in step.fillable_fields()}
    c.expected = len(expected["fields"])
    c.detected = len(fillable_ids)
    for exp in expected["fields"]:
        found = by_id.get(exp["id"])
        if not found or exp["id"] not in fillable_ids:
            c.misses.append(f"{name}: missing {exp['id']}")
            continue
        f, m = found
        c.matched += 1
        for attr, key in [("label_ok", "label"), ("kind_ok", "kind"), ("required_ok", "required")]:
            if getattr(f, key) == exp[key]:
                setattr(c, attr, getattr(c, attr) + 1)
            else:
                c.misses.append(f"{name}: {exp['id']} {key} {getattr(f, key)!r} != {exp[key]!r}")
        if mapping_label(m) == exp["mapping"]:
            c.mapping_ok += 1
        else:
            c.misses.append(
                f"{name}: {exp['id']} mapping {mapping_label(m)!r} != {exp['mapping']!r}"
            )
    extra = fillable_ids - {e["id"] for e in expected["fields"]}
    c.misses.extend(f"{name}: unexpected field {x}" for x in sorted(extra))
    for hidden in expected.get("must_skip", []):
        c.skipped_expected += 1
        if hidden in by_id and hidden not in fillable_ids:
            c.skipped_ok += 1
        else:
            c.misses.append(f"{name}: {hidden} should not be fillable")
    c.submit_expected = 1
    submits = [b.text for b in step.buttons_of("submit")]
    want = expected.get("submit_button")
    if (want is None and not submits) or (want is not None and submits == [want]):
        c.submit_ok = 1
    else:
        c.misses.append(f"{name}: submit buttons {submits} != {want!r}")
    return c


async def detect_all(names: list[str]) -> dict[str, FormStep]:
    steps = {}
    async with BrowserSession(
        Settings(_env_file=None), extra_hosts={HOST}, fixture_server=serve_fixtures
    ) as session:
        for name in names:
            page = await session.new_page()
            await session.goto(page, fixture_url(name))
            steps[name] = await detect(page)
            await page.close()
    return steps


async def run_eval() -> tuple[dict[str, float], Counts, dict[str, Counts]]:
    profile = Profile.load(PROFILE)
    names = sorted(p.stem for p in FORMS.glob("*.html"))
    steps = await detect_all(names)
    total, per = Counts(), {}
    for name in names:
        expected = json.loads((EXPECTED / f"{name}.expected.json").read_text())
        mappings = map_step(
            steps[name],
            profile,
            overrides=expected.get("overrides", {}),
            data=expected.get("data", {}),
        )
        per[name] = score(name, steps[name], mappings, expected)
        total.add(per[name])
    return total.metrics(), total, per


def _yes(flag: int) -> str:
    return "yes" if flag else "no"


def write_report(metrics: dict[str, float], total: Counts, per: dict[str, Counts]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    lines = [
        f"# Detection and mapping eval, {today}",
        "",
        f"{len(per)} fixture pages, {total.expected} expected fillable fields, "
        f"{total.skipped_expected} hidden or honeypot fields that must be left alone.",
        "",
        "Caveat: these are our own pages, and the expected files were drafted from the",
        "detector's output and then checked by hand. A perfect score means the agent matches",
        "those checked answers; it is not a benchmark of unseen websites.",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {k} | {v:.2%} |" for k, v in metrics.items()],
        "",
        "| Fixture | Expected | Matched | Mapping OK | Submit OK |",
        "|---|---|---|---|---|",
        *[
            f"| {n} | {c.expected} | {c.matched} | {c.mapping_ok} | {_yes(c.submit_ok)} |"
            for n, c in per.items()
        ],
        "",
        "## Misses",
        "",
        *([f"- {m}" for m in total.misses] or ["- none"]),
        "",
    ]
    path = RESULTS / f"detection-{today}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (RESULTS / f"detection-{today}.json").write_text(
        json.dumps({"date": today, "metrics": metrics, "misses": total.misses}, indent=2) + "\n"
    )
    return path


def main() -> int:
    metrics, total, per = asyncio.run(run_eval())
    path = write_report(metrics, total, per)
    for k, v in metrics.items():
        print(f"{k:20} {v:.2%}")
    print(f"report: {path.relative_to(ROOT)}")
    return 0 if not total.misses else 1


if __name__ == "__main__":
    sys.exit(main())
