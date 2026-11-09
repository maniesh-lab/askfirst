"""LLM classification of issues, checked hard, then merged with the rules (rules win).

Issues go to the LLM in batches of about 5. Validators (each message is fed back to the model):
- the numbers match the batch exactly
- every label is on the rubric's allowlist
- `duplicate_of` is one of that issue's candidates
- the rationale quotes (in double quotes) a phrase that really appears in the issue
- summary <= 160 and rationale <= 300 characters

A batch that still fails is split into single issues; an issue that still fails is
`unclassified` and shows in the digest as "needs a human".
"""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.runtime.audit import AuditLog
from app.runtime.llm import LLM, LLMFailed, load_prompt, register_fake_task, render_prompt
from app.triage.models import Category, Issue, Priority, RuleResult, TriageResult
from app.triage.rubric import Rubric

SYSTEM = (
    "You are a careful issue triager. Follow the rubric exactly, never invent issue numbers or "
    "labels, and reply with JSON only."
)
BATCH_SIZE = 5
# The only things a "please add info" comment knows how to ask for (see actions.MISSING_TEXT).
# Anything else from the model is a validation error, fed back so it can fix its answer.
Missing = Literal["steps", "expected", "actual"]
QUOTE_RE = re.compile(r"[\"“]([^\"”]{3,120})[\"”]")


class IssueClass(BaseModel):
    number: int
    category: Category
    priority: Priority
    labels: list[str] = Field(default_factory=list)
    needs_info: bool = False
    missing: list[Missing] = Field(default_factory=list)
    duplicate_of: int | None = None
    summary: str
    rationale: str


class ClassifyBatch(BaseModel):
    results: list[IssueClass]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def make_validators(batch: list[Issue], cands: dict[int, list[int]], rubric: Rubric) -> list:
    numbers = sorted(i.number for i in batch)
    texts = {i.number: _norm(i.text) for i in batch}

    def exact_numbers(r: ClassifyBatch) -> str | None:
        got = sorted(x.number for x in r.results)
        return None if got == numbers else f"results must cover exactly issues {numbers}, got {got}"

    def allowed_labels(r: ClassifyBatch) -> str | None:
        bad = sorted({lab for x in r.results for lab in x.labels if not rubric.label_allowed(lab)})
        return f"labels not allowed by the rubric: {bad}" if bad else None

    def duplicates(r: ClassifyBatch) -> str | None:
        bad = [
            f"#{x.number} duplicate_of {x.duplicate_of} is not in its candidates "
            f"{cands.get(x.number, [])}"
            for x in r.results
            if x.duplicate_of is not None and x.duplicate_of not in cands.get(x.number, [])
        ]
        return "; ".join(bad) or None

    def quotes(r: ClassifyBatch) -> str | None:
        bad = []
        for x in r.results:
            found = QUOTE_RE.findall(x.rationale)
            if not found or not any(_norm(q) in texts.get(x.number, "") for q in found):
                bad.append(
                    f"#{x.number} rationale must quote an exact phrase from the issue "
                    "in double quotes"
                )
        return "; ".join(bad) or None

    def lengths(r: ClassifyBatch) -> str | None:
        bad = []
        for x in r.results:
            if len(x.summary) > 160:
                bad.append(f"#{x.number} summary is {len(x.summary)} characters (max 160)")
            if len(x.rationale) > 300:
                bad.append(f"#{x.number} rationale is {len(x.rationale)} characters (max 300)")
        return "; ".join(bad) or None

    return [exact_numbers, allowed_labels, duplicates, quotes, lengths]


def issue_for_prompt(issue: Issue, cands: list[tuple[int, float]]) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "labels": issue.labels,
        "duplicate_candidates": [{"number": n, "similarity": s} for n, s in cands],
    }


async def classify_batch(
    llm: LLM,
    batch: list[Issue],
    cands: dict[int, list[tuple[int, float]]],
    rubric: Rubric,
    *,
    run_id: str | None = None,
    max_attempts: int = 3,
) -> dict[int, IssueClass]:
    inputs = {
        "rubric": rubric.for_prompt(),
        "issues": [issue_for_prompt(i, cands.get(i.number, [])) for i in batch],
    }
    prompt = render_prompt(load_prompt("triage", "v1"), "triage", inputs)
    numbers_only = {k: [n for n, _ in v] for k, v in cands.items()}
    result = await llm.structured(
        prompt,
        ClassifyBatch,
        system=SYSTEM,
        validators=make_validators(batch, numbers_only, rubric),
        max_attempts=max_attempts,
        run_id=run_id,
        purpose="triage",
        temperature=0,
    )
    return {x.number: x for x in result.results}


async def classify_all(
    llm: LLM,
    issues: list[Issue],
    cands: dict[int, list[tuple[int, float]]],
    rubric: Rubric,
    *,
    run_id: str | None = None,
    max_attempts: int = 3,
) -> tuple[dict[int, IssueClass], list[int]]:
    """Classify in batches; split a failing batch into singles. Returns (results, unclassified)."""
    out: dict[int, IssueClass] = {}
    failed: list[int] = []
    for start in range(0, len(issues), BATCH_SIZE):
        batch = issues[start : start + BATCH_SIZE]
        try:
            out.update(
                await classify_batch(
                    llm, batch, cands, rubric, run_id=run_id, max_attempts=max_attempts
                )
            )
            continue
        except LLMFailed:
            if len(batch) == 1:
                failed.append(batch[0].number)
                continue
        for issue in batch:
            try:
                out.update(
                    await classify_batch(
                        llm, [issue], cands, rubric, run_id=run_id, max_attempts=max_attempts
                    )
                )
            except LLMFailed:
                failed.append(issue.number)
    return out, failed


def merge(
    issue: Issue,
    rules: RuleResult,
    llm: IssueClass | None,
    rubric: Rubric,
    *,
    audit: AuditLog | None = None,
    run_id: str | None = None,
) -> TriageResult:
    """One result per issue. Rules win; each time a rule changes the LLM's answer, it is audited."""
    if rules.skip:
        return TriageResult(
            number=issue.number,
            title=issue.title,
            url=issue.url,
            skipped=True,
            stale=rules.stale,
            source="rules",
            rationale="; ".join(rules.reasons),
        )
    overrides = []
    category = llm.category if llm else None
    priority = llm.priority if llm else None
    if rules.category and rules.category != category:
        overrides.append(f"category {category} -> {rules.category} (rule)")
        category = rules.category
    if rules.priority and rules.priority != priority:
        overrides.append(f"priority {priority} -> {rules.priority} (rule)")
        priority = rules.priority
    if rules.needs_info and llm and not llm.needs_info:
        overrides.append("needs_info false -> true (rule)")
    labels = {lab for lab in (llm.labels if llm else []) if rubric.label_allowed(lab)}
    labels = {lab for lab in labels if not lab.startswith(("type:", "priority:"))}
    labels |= set(rules.labels)
    if category:
        labels.add(f"type:{category}")
    if priority:
        labels.add(f"priority:{priority}")
    labels = {
        lab
        for lab in labels
        if not lab.startswith(("type:", "priority:"))
        or lab in {f"type:{category}", f"priority:{priority}"}
    }
    needs_info = rules.needs_info or bool(llm and llm.needs_info)
    if needs_info:
        labels.add("needs-info")
    duplicate_of = llm.duplicate_of if llm else None
    if duplicate_of:
        labels.add("possible-duplicate")
    if audit and run_id:
        for change in overrides:
            audit.record(run_id, "agent:merge", "override", issue=issue.number, change=change)
    return TriageResult(
        number=issue.number,
        title=issue.title,
        url=issue.url,
        category=category,
        priority=priority,
        labels=sorted(labels),
        needs_info=needs_info,
        missing=sorted(set(rules.missing) | set(llm.missing if llm else [])),
        duplicate_of=duplicate_of,
        summary=(llm.summary if llm else issue.title)[:160],
        rationale=(llm.rationale if llm else "; ".join(rules.reasons))[:300],
        stale=rules.stale,
        escalate=rules.escalate,
        unclassified=llm is None and rules.category is None,
        overrides=overrides,
        source="llm" if llm else ("rules" if rules.category else "none"),
    )


# Offline classifier for LLM_PROVIDER=fake (CI, demos): keywords only, always valid output. ----

KEYWORDS: list[tuple[str, list[str]]] = [
    ("security", ["vulnerab", "cve", "exploit", "xss", "token leak", "leaked", "injection"]),
    ("docs", ["docs", "documentation", "readme", "typo", "tutorial", "guide"]),
    ("chore", ["bump", "dependency", "dependencies", "refactor", "ci ", "lint", "upgrade"]),
    ("question", ["how do i", "how can i", "is it possible", "question", "how to"]),
    (
        "feature",
        ["add ", "support for", "feature", "would be nice", "allow ", "request", "option to"],
    ),
    (
        "bug",
        [
            "bug",
            "crash",
            "error",
            "broken",
            "fails",
            "exception",
            "doesn't",
            "not working",
            "wrong",
        ],
    ),
]
AREAS = {
    "ui": ["button", "page", "screen", "dark mode", "layout", "ui"],
    "api": ["api", "endpoint", "webhook"],
    "auth": ["login", "password", "auth", "token"],
    "docs": ["docs", "readme"],
    "build": ["ci", "build", "dependency", "docker"],
    "data": ["export", "import", "csv", "database"],
}


@register_fake_task("triage")
def fake_triage(inputs: dict[str, Any]) -> str:
    results = []
    for issue in inputs.get("issues", []):
        text = f"{issue['title']} {issue['body']}".lower()
        category = next((c for c, words in KEYWORDS if any(w in text for w in words)), "question")
        if category == "security":
            priority = "P0"
        elif category == "bug":
            priority = (
                "P1"
                if any(w in text for w in ["everyone", "all users", "data loss", "every "])
                else "P2"
            )
        elif category == "feature":
            priority = "P2"
        else:
            priority = "P3"
        areas = [a for a, words in AREAS.items() if any(w in text for w in words)][:1]
        cands = [c for c in issue.get("duplicate_candidates", []) if c["similarity"] >= 0.6]
        quote = " ".join(issue["title"].split()[:5])
        results.append(
            {
                "number": issue["number"],
                "category": category,
                "priority": priority,
                "labels": [f"type:{category}", f"priority:{priority}"]
                + [f"area:{a}" for a in areas],
                "needs_info": False,
                "missing": [],
                "duplicate_of": cands[0]["number"] if cands else None,
                "summary": issue["title"][:160],
                "rationale": f'Keyword match; the title says "{quote}".',
            }
        )
    return json.dumps({"results": results})
