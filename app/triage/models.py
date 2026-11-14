"""The triage bot's data: issues as we read them, what we decided about each, the actions we
plan, and the digest we post."""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Category = Literal["bug", "feature", "question", "docs", "chore", "security"]
Priority = Literal["P0", "P1", "P2", "P3"]

BODY_LIMIT = 3000
FULL_BODY_LIMIT = 65536  # GitHub's own limit for an issue body
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def shorten_body(body: str | None, limit: int = BODY_LIMIT) -> str:
    """Code blocks become a short marker (stack traces eat tokens and say little), then cap."""
    text = CODE_BLOCK_RE.sub(
        lambda m: f"[code block, {m.group(0).count(chr(10))} lines]", body or ""
    )
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " [cut]"


class Issue(BaseModel):
    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    created_at: datetime
    updated_at: datetime
    comments: int = 0
    url: str = ""
    state: Literal["open", "closed"] = "open"
    # The body as written (capped at 64 KB), for the rules only. `body` is the shortened copy the
    # LLM sees: code blocks become a marker and it stops at 3000 characters, so a security word
    # in pasted scanner output or late in a long report would be missed if the rules read it.
    full_body: str = ""

    @classmethod
    def from_github(cls, raw: dict[str, Any]) -> Issue:
        return cls(
            number=raw["number"],
            title=raw.get("title") or "",
            body=shorten_body(raw.get("body")),
            full_body=(raw.get("body") or "")[:FULL_BODY_LIMIT],
            labels=[
                lab["name"] if isinstance(lab, dict) else str(lab) for lab in raw.get("labels", [])
            ],
            author=(raw.get("user") or {}).get("login", ""),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            comments=raw.get("comments", 0),
            url=raw.get("html_url", ""),
            state=raw.get("state", "open"),
        )

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"

    @property
    def rule_text(self) -> str:
        """Title and the full body: what the keyword rules read."""
        return f"{self.title}\n{self.full_body or self.body}"


class RuleResult(BaseModel):
    """What the deterministic rules decided. Anything set here beats the LLM."""

    number: int
    category: Category | None = None
    priority: Priority | None = None
    labels: list[str] = Field(default_factory=list)
    needs_info: bool = False
    missing: list[str] = Field(default_factory=list)
    stale: bool = False
    skip: bool = False
    escalate: bool = False
    reasons: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    number: int
    title: str
    url: str = ""
    category: Category | None = None
    priority: Priority | None = None
    labels: list[str] = Field(default_factory=list)
    needs_info: bool = False
    missing: list[str] = Field(default_factory=list)
    duplicate_of: int | None = None
    summary: str = ""
    rationale: str = ""
    stale: bool = False
    escalate: bool = False
    unclassified: bool = False
    skipped: bool = False
    overrides: list[str] = Field(default_factory=list)
    source: Literal["llm", "rules", "none"] = "none"


class PlannedAction(BaseModel):
    id: str
    kind: Literal["add_labels", "comment", "close_issue"]
    issue: int
    params: dict[str, Any] = Field(default_factory=dict)
    summary: str
    verdict: str = ""
    applied: bool = False
    error: str | None = None


class DigestWindow(BaseModel):
    start: datetime
    end: datetime


class Digest(BaseModel):
    repo: str
    window: DigestWindow
    run_id: str
    counts_by_priority: dict[str, int] = Field(default_factory=dict)
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    urgent: list[TriageResult] = Field(default_factory=list)
    new_by_category: dict[str, list[TriageResult]] = Field(default_factory=dict)
    needs_info: list[TriageResult] = Field(default_factory=list)
    duplicates: list[TriageResult] = Field(default_factory=list)
    stale: list[TriageResult] = Field(default_factory=list)
    proposals: list[PlannedAction] = Field(default_factory=list)
    applied: list[PlannedAction] = Field(default_factory=list)
    failed: list[PlannedAction] = Field(default_factory=list)
    unsure: list[PlannedAction] = Field(default_factory=list)
    unclassified: list[TriageResult] = Field(default_factory=list)
    actions_url: str | None = None
    tokens: int = 0
    duration_s: float = 0.0
    total_issues: int = 0
    mode: str = "propose"

    @property
    def empty(self) -> bool:
        return self.total_issues == 0
