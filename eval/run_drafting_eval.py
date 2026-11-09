"""Drafting eval: does the fact checker stop invented facts, and how often do drafts pass?

The free-text questions come from the form fixtures (`eval/forms/*.expected.json`, fields mapped
to `needs_draft`), grouped per form the way the agent drafts them (one LLM call per step).

Offline (default, no network, no LLM):
1. **Planted inventions**: each clean draft gets one made-up fact added (a company, a person, a
   year, a number, an email, a URL, a certificate, a school, a city, a product...). The draft
   validators must catch every one: the **catch rate must be 100%**.
2. **Clean drafts**: drafts that only use profile facts, in different wording. Any flag here is
   a false alarm (it would make the agent leave a good answer empty).
3. **Through Steadfast**: a scripted fake model that keeps inventing must end with the field
   left empty and flagged (never used), and one that invents once and then fixes it must pass
   after the retry.

`--live` also drafts every form with your configured provider (Groq, about 6 to 18 calls) and
reports the share that passed on the first try, after retries, or not at all.

Run: `python -m eval.run_drafting_eval [--live]` -> eval/results/drafting-<date>.md and .json
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.browser.draft import (
    Drafts,
    draft_answers,
    fake_draft,
    make_validators,
    page_for_prompt,
    profile_for_prompt,
    questions_for_prompt,
)
from app.browser.mapping import Profile
from app.browser.schema import Field
from app.config import Settings, get_settings
from app.runtime.llm import FakeProvider, make_llm
from app.runtime.logging import configure_logging

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = ROOT / "eval" / "forms"
FIXTURES = ROOT / "tests" / "fixtures" / "forms"
RESULTS = ROOT / "eval" / "results"
PROFILE = ROOT / "profiles" / "example.yaml"
TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)

# One made-up fact each; none of these is in the fake persona's profile or on the pages.
PLANTED: dict[str, str] = {
    "company": "Before that I spent a year at Globex Corporation.",
    "person": "My referee is Dr. Hana Kovac.",
    "year": "I started in customer service in 2015.",
    "count": "I handled 40 tickets a day.",
    "experience": "I have 8 years of experience.",
    "email": "You can also reach me at riya.t@mailbox.example.",
    "url": "My portfolio is at https://portfolio.example.net/riya.",
    "certificate": "I hold the ITIL Foundation certificate.",
    "school": "I studied at Kathmandu University.",
    "city": "I am moving to Pokhara next month.",
    "product": "I am fluent in Zendesk and Salesforce.",
    "sentence_start_name": "Initech hired me as a team lead.",
    "percent": "I cut response times by 35%.",
    "phone": "Call me on 555-0199.",
}

# Only profile facts, worded differently from the fake drafts. A flag here is a false alarm.
CLEAN: list[str] = [
    "At Example Labs I answer customer emails and chats.",
    "Since 2023 I have worked as a Support Specialist.",
    "Before that I was an Office Assistant at Sample Traders.",
    "I studied for a Bachelor of Business Studies at Example College.",
    "I have 3 years of experience, and I enjoy writing help articles.",
    "Turning customer problems into clear bug reports is a big part of my work.",
]


@dataclass
class FormQuestions:
    form: str
    title: str
    fields: list[Field]

    @property
    def facts(self) -> str:
        return facts_for(self)


def load_questions() -> list[FormQuestions]:
    out = []
    for path in sorted(EXPECTED.glob("*.expected.json")):
        expected = json.loads(path.read_text())
        fields = [
            Field(key=f["id"], kind=f["kind"], label=f["label"], name=f["id"])
            for f in expected["fields"]
            if f["mapping"] == "needs_draft"
        ]
        if not fields:
            continue
        name = expected["fixture"]
        match = TITLE_RE.search((FIXTURES / f"{name}.html").read_text())
        out.append(FormQuestions(name, match.group(1).strip() if match else name, fields))
    return out


PROFILE_OBJ = Profile.load(PROFILE)


def facts_for(form: FormQuestions) -> str:
    """The same facts the agent checks against: the profile plus what the page says."""
    page = page_for_prompt(form.title, [], "")
    return PROFILE_OBJ.facts_text() + "\n" + json.dumps(page, ensure_ascii=False)


def clean_drafts(form: FormQuestions) -> dict[str, str]:
    """What the offline fake model writes: profile facts only."""
    inputs = {
        "questions": questions_for_prompt(form.fields),
        "profile": profile_for_prompt(PROFILE_OBJ),
    }
    return json.loads(fake_draft(inputs))["answers"]


def problems(form: FormQuestions, answers: dict[str, str]) -> list[str]:
    out = []
    for check in make_validators(form.fields, form.facts):
        message = check(Drafts(answers=answers))
        if message:
            out.append(message)
    return out


def planted_eval(forms: list[FormQuestions]) -> dict[str, Any]:
    caught, missed, per_kind = 0, [], {k: [0, 0] for k in PLANTED}
    for form in forms:
        base = clean_drafts(form)
        for key in base:
            for kind, sentence in PLANTED.items():
                answers = dict(base)
                answers[key] = f"{base[key]} {sentence}"
                per_kind[kind][1] += 1
                if problems(form, answers):
                    caught += 1
                    per_kind[kind][0] += 1
                else:
                    missed.append(f"{form.form}/{key}: {kind}")
    total = sum(n for _, n in per_kind.values())
    return {
        "planted": total,
        "caught": caught,
        "catch_rate": round(caught / total, 4) if total else 1.0,
        "per_kind": {k: f"{c}/{n}" for k, (c, n) in per_kind.items()},
        "missed": missed,
    }


def clean_eval(forms: list[FormQuestions]) -> dict[str, Any]:
    checked, false_alarms = 0, []
    for form in forms:
        base = clean_drafts(form)
        candidates = [base] + [dict.fromkeys(base, sentence) for sentence in CLEAN]
        for answers in candidates:
            checked += 1
            found = problems(form, answers)
            if found:
                false_alarms.append(f"{form.form}: {found[0][:160]}")
    return {
        "clean_drafts": checked,
        "false_alarms": len(false_alarms),
        "false_alarm_rate": round(len(false_alarms) / checked, 4) if checked else 0.0,
        "examples": false_alarms[:10],
    }


async def steadfast_eval(forms: list[FormQuestions]) -> dict[str, Any]:
    """Scripted fake models through the real draft path (Steadfast validation and retries)."""
    always_invents_blocked = recovered = 0
    for form in forms:
        base = clean_drafts(form)
        tainted = json.dumps({"answers": {k: f"{v} {PLANTED['company']}" for k, v in base.items()}})
        clean = json.dumps({"answers": base})
        for replies, expect_failed in (([tainted], True), ([tainted, clean], False)):
            llm = make_llm(Settings(_env_file=None), provider=FakeProvider(replies=replies))
            answers, failed, _ = await draft_answers(
                llm, form.fields, PROFILE_OBJ, title=form.title, headings=[], context=""
            )
            if expect_failed and not answers and failed:
                always_invents_blocked += 1
            if not expect_failed and answers == base and not failed:
                recovered += 1
    return {
        "forms": len(forms),
        "always_invents_left_empty": always_invents_blocked,
        "invents_once_recovered_after_retry": recovered,
    }


async def live_eval(forms: list[FormQuestions]) -> dict[str, Any]:
    settings = get_settings()
    if settings.llm_provider == "fake":
        raise SystemExit("--live needs a real model: set LLM_PROVIDER=groq and GROQ_API_KEY")
    llm = make_llm(settings)
    rows = []
    for form in forms:
        before = llm.calls
        answers, failed, error = await draft_answers(
            llm, form.fields, PROFILE_OBJ, title=form.title, headings=[], context=""
        )
        attempts = llm.calls - before
        outcome = "failed" if failed else ("first_try" if attempts == 1 else "after_retry")
        rows.append(
            {
                "form": form.form,
                "fields": len(form.fields),
                "attempts": attempts,
                "outcome": outcome,
                "error": error,
                # The fake persona only, so it is safe to keep the drafts for a person to read.
                "answers": answers,
            }
        )
    n = len(rows)
    return {
        "provider": llm.provider,
        "model": llm.model,
        "tokens": llm.usage.total_tokens,
        "calls": llm.calls,
        "forms": n,
        "first_try_pass_rate": round(sum(r["outcome"] == "first_try" for r in rows) / n, 4),
        "pass_rate_after_retries": round(sum(r["outcome"] != "failed" for r in rows) / n, 4),
        "rows": rows,
    }


async def run(live: bool) -> dict[str, Any]:
    forms = load_questions()
    report: dict[str, Any] = {
        "date": date.today().isoformat(),
        "forms": [f.form for f in forms],
        "questions": sum(len(f.fields) for f in forms),
        "planted": planted_eval(forms),
        "clean": clean_eval(forms),
        "steadfast": await steadfast_eval(forms),
    }
    if live:
        report["live"] = await live_eval(forms)
    return report


def write(report: dict[str, Any]) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p, c, s = report["planted"], report["clean"], report["steadfast"]
    lines = [
        f"# Drafting eval, {report['date']}",
        "",
        f"{len(report['forms'])} forms with free-text questions, {report['questions']} questions "
        f"({', '.join(report['forms'])}). Persona: the fake profile in `profiles/example.yaml`.",
        "",
        "## Planted inventions (offline)",
        "",
        f"Each clean draft got one made-up fact added; {len(PLANTED)} kinds x "
        f"{report['questions']} questions = {p['planted']} cases.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| catch rate (must be 100%) | {p['catch_rate']:.2%} ({p['caught']}/{p['planted']}) |",
        f"| false alarms on clean drafts | {c['false_alarm_rate']:.2%} "
        f"({c['false_alarms']}/{c['clean_drafts']}) |",
        f"| always-inventing model: field left empty | {s['always_invents_left_empty']}/"
        f"{s['forms']} |",
        f"| invents once, fixed after retry | {s['invents_once_recovered_after_retry']}/"
        f"{s['forms']} |",
        "",
        "| Kind | Caught |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in p["per_kind"].items()],
        "",
        "Missed: " + (", ".join(p["missed"]) or "none") + ".",
        "",
        "Caveat: the planted facts are our own list. The checker is a strict, rule-based net",
        "(names, numbers, emails, URLs must appear in the profile or on the page), so it is built",
        "to catch these; the false-alarm line shows the price of that strictness.",
        "",
    ]
    if c["examples"]:
        lines += ["False alarms:", "", *[f"- {e}" for e in c["examples"]], ""]
    if "live" in report:
        lv = report["live"]
        lines += [
            f"## Live drafting ({lv['provider']}, `{lv['model']}`)",
            "",
            f"{lv['forms']} forms, {lv['calls']} calls, {lv['tokens']} tokens.",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| passed on the first try | {lv['first_try_pass_rate']:.2%} |",
            f"| passed after Steadfast retries | {lv['pass_rate_after_retries']:.2%} |",
            "",
            "| Form | Attempts | Outcome |",
            "|---|---|---|",
            *[f"| {r['form']} | {r['attempts']} | {r['outcome']} |" for r in lv["rows"]],
            "",
        ]
    base = RESULTS / f"drafting-{report['date']}"
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    base.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    return base.with_suffix(".md")


def main() -> int:
    parser = argparse.ArgumentParser(description="Drafting eval: fact checker and pass rates")
    parser.add_argument(
        "--live", action="store_true", help="also draft with the real provider (6 to 18 calls)"
    )
    args = parser.parse_args()
    # The scripted retries log a validation warning each; only real errors matter here.
    configure_logging("ERROR", "console")
    report = asyncio.run(run(args.live))
    path = write(report)
    p, c = report["planted"], report["clean"]
    print(f"planted catch rate   {p['catch_rate']:.2%} ({p['caught']}/{p['planted']})")
    print(
        f"clean false alarms   {c['false_alarm_rate']:.2%} "
        f"({c['false_alarms']}/{c['clean_drafts']})"
    )
    if "live" in report:
        print(f"live first try       {report['live']['first_try_pass_rate']:.2%}")
        print(f"live after retries   {report['live']['pass_rate_after_retries']:.2%}")
    print(f"report: {path.relative_to(ROOT)}")
    return 0 if p["catch_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
