"""LLM drafting for free-text questions only. The LLM never picks selectors, never maps fields
and never clicks; it only writes text, and every draft is checked before it is used.

One Steadfast `structured()` call per form step. The validators below each return a plain
message when something is wrong, and Steadfast feeds that message back to the model for another
try:
- exactly the requested keys, no extras
- not empty, and within the field's maxlength
- no placeholders (`[Your Name]`, "As an AI", "lorem")
- **fact check**: numbers, years, emails, URLs and capitalised names must appear in the profile
  or on the page; otherwise "X is not in the profile"

After the last attempt fails, the fields stay empty and are flagged `draft_failed` for the
reviewer.
"""

import json
import re
from typing import Any

from pydantic import BaseModel

from app.browser.mapping import Profile
from app.browser.schema import Field
from app.runtime.llm import LLM, LLMFailed, load_prompt, register_fake_task, render_prompt

SYSTEM = (
    "You draft answers for web forms on behalf of a real person, who reviews them before "
    "anything is sent. Draft only. Never invent facts: use only what the profile and the page "
    "say. Reply with JSON only."
)
PROMPT_VERSION = "v1"
DEFAULT_MAXLENGTH = 1000

PLACEHOLDER_RE = re.compile(
    r"\[[^\]]{2,40}\]|\bas an ai\b|\blorem\b|\bipsum\b|\{\{|\bTODO\b|\bINSERT\b|<[^>]+>",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"https?://[^\s)\"']+")
NAME_RE = re.compile(r"\b[A-Z][a-zA-Z'&.-]*(?:\s+(?:of\s+|and\s+|&\s+)?[A-Z][a-zA-Z'&.-]*)*")
SENTENCE_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*|[\"(]\s*)$")
# Ordinary words that often start a sentence. A capitalised first word that is NOT one of these
# (and isn't an -ing/-ly/-ed word) is checked like any other name: "Initech hired me" is caught.
SENTENCE_STARTERS = {
    "a",
    "an",
    "the",
    "my",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "in",
    "at",
    "on",
    "for",
    "with",
    "as",
    "and",
    "but",
    "or",
    "so",
    "because",
    "since",
    "when",
    "while",
    "after",
    "before",
    "during",
    "over",
    "through",
    "from",
    "to",
    "by",
    "of",
    "if",
    "although",
    "though",
    "also",
    "there",
    "here",
    "what",
    "why",
    "how",
    "which",
    "who",
    "we",
    "you",
    "they",
    "he",
    "she",
    "me",
    "being",
    "having",
    "each",
    "every",
    "many",
    "most",
    "some",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "first",
    "second",
    "finally",
    "today",
    "currently",
    "now",
    "then",
    "outside",
    "beyond",
    "helping",
    "working",
    "writing",
    "answering",
    "patience",
    "clear",
    "good",
    "great",
    "thank",
    "thanks",
    "please",
    "yes",
    "no",
    "all",
    "both",
    "any",
    "more",
    "much",
    "very",
    "reach",
    "contact",
    "feel",
    "let",
    "join",
    "bring",
    "offer",
    "see",
    "find",
    "consider",
    "hope",
    "believe",
    "love",
    "enjoy",
    "like",
    "want",
    "would",
    "could",
    "can",
    "will",
    "write",
    "call",
    "email",
    "send",
    "ask",
    "keep",
    "make",
    "take",
    "give",
    "learn",
    "work",
}
# Capitalised words that are never facts on their own.
COMMON_WORDS = {
    "i",
    "i'm",
    "i've",
    "i'd",
    "i'll",
    "dear",
    "hello",
    "hi",
    "thank",
    "thanks",
    "regards",
    "sincerely",
    "best",
    "kind",
    "yours",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "english",
    "nepali",
    "team",
    "manager",
    "hiring",
    "ps",
    "ok",
}


class Drafts(BaseModel):
    answers: dict[str, str]


def profile_for_prompt(profile: Profile) -> dict[str, Any]:
    """Only the sections a free-text answer needs (no contact details or address)."""
    return {
        "name": profile.identity.get("full_name"),
        "work": profile.work,
        "education": profile.education,
        "skills": profile.skills,
    }


def page_for_prompt(title: str, headings: list[str], context: str) -> dict[str, Any]:
    return {"title": title, "headings": headings[:6], "description": context[:1200]}


def questions_for_prompt(fields: list[Field]) -> list[dict[str, Any]]:
    return [
        {"key": f.key, "question": f.label, "max_length": f.maxlength or DEFAULT_MAXLENGTH}
        for f in fields
    ]


def _in(needle: str, haystack: str) -> bool:
    """Whole-word, case-insensitive: "9" is not found inside "980", "Lab" not inside "Labs"."""
    return re.search(rf"(?<![\w]){re.escape(needle.lower())}(?![\w])", haystack) is not None


def _ordinary(word: str, text: str) -> bool:
    """Is this capitalised first word just an ordinary word (not a name)?"""
    w = word.lower().strip(".,'!?:;")
    if w in SENTENCE_STARTERS or w in COMMON_WORDS or w.endswith(("ing", "ly", "ed")):
        return True
    # Written in lower case somewhere else in the same text: an ordinary word.
    return re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", text) is not None


def invented_facts(text: str, facts: str) -> list[str]:
    """Things in `text` that look like facts but don't appear in `facts`."""
    haystack = facts.lower()
    found: list[str] = []
    for pattern in (EMAIL_RE, URL_RE):
        for m in pattern.finditer(text):
            if m.group(0).rstrip(".,").lower() not in haystack:
                found.append(m.group(0).rstrip(".,"))
    stripped = URL_RE.sub(" ", EMAIL_RE.sub(" ", text))
    for m in NUMBER_RE.finditer(stripped):
        if not _in(m.group(0), haystack):
            found.append(m.group(0))
    for m in NAME_RE.finditer(stripped):
        words = m.group(0).split()
        if SENTENCE_START_RE.search(stripped[: m.start()]) and _ordinary(words[0], stripped):
            words = words[1:]  # an ordinary word that just starts a sentence
        words = [w for w in words if w.lower().strip(".,'") not in COMMON_WORDS]
        phrase = " ".join(words).strip(".,'&- ")
        if phrase and not _in(phrase, haystack):
            found.append(phrase)
    return list(dict.fromkeys(found))


def make_validators(fields: list[Field], facts: str) -> list:
    wanted = {f.key for f in fields}
    limits = {f.key: f.maxlength or DEFAULT_MAXLENGTH for f in fields}

    def exact_keys(d: Drafts) -> str | None:
        got = set(d.answers)
        if got != wanted:
            missing, extra = sorted(wanted - got), sorted(got - wanted)
            return (
                f"answers must have exactly these keys: {sorted(wanted)} "
                f"(missing {missing}, extra {extra})"
            )
        return None

    def lengths(d: Drafts) -> str | None:
        problems = []
        for key, text in d.answers.items():
            if key in limits and not text.strip():
                problems.append(f"{key} is empty")
            elif key in limits and len(text) > limits[key]:
                problems.append(f"{key} is {len(text)} characters; the limit is {limits[key]}")
        return "; ".join(problems) or None

    def no_placeholders(d: Drafts) -> str | None:
        bad = [
            f"{k} contains {m.group(0)!r}"
            for k, t in d.answers.items()
            if (m := PLACEHOLDER_RE.search(t))
        ]
        return "; ".join(f"{b} (no placeholders)" for b in bad) or None

    def fact_check(d: Drafts) -> str | None:
        problems = []
        for key, text in d.answers.items():
            for fact in invented_facts(text, facts):
                problems.append(f"{key}: {fact!r} is not in the profile or on the page")
        return "; ".join(problems) or None

    return [exact_keys, lengths, no_placeholders, fact_check]


async def draft_answers(
    llm: LLM,
    fields: list[Field],
    profile: Profile,
    *,
    title: str,
    headings: list[str],
    context: str,
    run_id: str | None = None,
    max_attempts: int = 3,
) -> tuple[dict[str, str], list[str], str | None]:
    """Drafts for these fields. Returns (answers, failed_keys, error)."""
    if not fields:
        return {}, [], None
    page = page_for_prompt(title, headings, context)
    inputs = {
        "questions": questions_for_prompt(fields),
        "profile": profile_for_prompt(profile),
        "page": page,
    }
    facts = profile.facts_text() + "\n" + json.dumps(page, ensure_ascii=False)
    prompt = render_prompt(load_prompt("draft", PROMPT_VERSION), "draft", inputs)
    try:
        result = await llm.structured(
            prompt,
            Drafts,
            system=SYSTEM,
            validators=make_validators(fields, facts),
            max_attempts=max_attempts,
            run_id=run_id,
            purpose="draft",
            temperature=0.3,
        )
    except LLMFailed as exc:
        return {}, [f.key for f in fields], str(exc)
    return result.answers, [], None


@register_fake_task("draft")
def fake_draft(inputs: dict[str, Any]) -> str:
    """Offline drafts built only from profile facts (LLM_PROVIDER=fake, CI, demos)."""
    profile = inputs.get("profile") or {}
    work = profile.get("work") or {}
    history = work.get("history") or [{}]
    skills = profile.get("skills") or []
    years = work.get("years_experience")
    job = history[0]
    lines = []
    if job.get("title") and job.get("company"):
        lines.append(f"I work as a {job['title'].lower()} at {job['company']}.")
    if years:
        lines.append(f"I have {years} years of experience helping people.")
    if skills:
        lines.append(f"My strengths are {', '.join(skills[:3])}.")
    lines.append("I would enjoy bringing that care and patience to this work.")
    answers = {}
    for q in inputs.get("questions", []):
        text = " ".join(lines)
        answers[q["key"]] = (
            text[: int(q.get("max_length") or DEFAULT_MAXLENGTH)].rsplit(" ", 1)[0]
            if len(text) > int(q.get("max_length") or DEFAULT_MAXLENGTH)
            else text
        )
    return json.dumps({"answers": answers})
