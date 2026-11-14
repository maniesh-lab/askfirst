"""The triage rubric (`config/triage_rubric.yaml`) and the deterministic rules engine.

Rules run before the LLM and win over it:
- security words (vulnerability, CVE, token leak, XSS, ...) -> `security`, P0, escalate. The LLM
  can't lower that.
- a bug report missing steps, expected or actual behaviour -> `needs-info` (with what's missing)
- no update for `stale_after_days` -> `stale`
- already labelled `triaged` and not updated since, or only changed by the bot itself -> skipped

Every time a rule overrides the LLM, the merge step records it in the audit log.
"""

import fnmatch
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.triage.models import Issue, RuleResult

RUBRIC_PATH = Path("config/triage_rubric.yaml")


class PriorityDef(BaseModel):
    meaning: str
    examples: list[str] = Field(default_factory=list)


class Rubric(BaseModel):
    version: int = 1
    categories: dict[str, str]
    priorities: dict[str, PriorityDef]
    label_allowlist: list[str]
    areas: list[str] = Field(default_factory=list)
    label_colors: dict[str, str] = Field(default_factory=dict)
    stale_after_days: int = 30
    rules: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = RUBRIC_PATH) -> Rubric:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def label_allowed(self, label: str) -> bool:
        # "area:*" is on the allowlist, but only the rubric's own areas count: otherwise text in
        # a stranger's issue could steer the model into creating any `area:<anything>` label.
        if label.startswith("area:") and label.removeprefix("area:") not in self.areas:
            return False
        return any(fnmatch.fnmatchcase(label, pattern) for pattern in self.label_allowlist)

    def for_prompt(self) -> dict[str, Any]:
        return {
            "categories": self.categories,
            "priorities": {k: v.model_dump() for k, v in self.priorities.items()},
            "labels_allowed": self.label_allowlist,
            "areas": self.areas,
        }


def _word_pattern(word: str) -> str:
    """A rubric word as a whole word, plural included: "exploit" also finds "exploits",
    "vulnerability" also finds "vulnerabilities", "key" also finds "keys"."""
    w = re.escape(word.lower())
    stem = w[:-1] + "(?:y|ys|ies)" if w.endswith("y") else w + "(?:s|es)?"
    return rf"(?<![a-z]){stem}(?![a-z])"


def _has_any(text: str, words: list[str]) -> list[str]:
    low = text.lower()
    return [w for w in words if re.search(_word_pattern(w), low)]


def apply_rules(
    issue: Issue,
    rubric: Rubric,
    now: datetime,
    *,
    last_run: datetime | None = None,
    bot_touched_at: datetime | None = None,
) -> RuleResult:
    result = RuleResult(number=issue.number)
    rules = rubric.rules
    stale_days = (now - issue.updated_at).days
    if issue.state == "open" and stale_days >= rubric.stale_after_days:
        result.stale = True
        result.labels.append("stale")
        result.reasons.append(f"no update for {stale_days} days")
    # Worked out before the skip rules: a security issue must never get a close proposal, even
    # one triaged long ago that has now gone stale.
    hits = _has_any(issue.rule_text, rules.get("security_words", []))
    is_security = bool(hits) or "type:security" in issue.labels
    if "triaged" in issue.labels and last_run is not None and issue.updated_at <= last_run:
        # Nothing new to classify, but it may still have gone stale (kept on the result).
        result.skip, result.escalate = True, is_security
        result.reasons.append("already triaged and not updated since the last run")
        return result
    if bot_touched_at is not None and issue.updated_at <= bot_touched_at:
        result.skip, result.escalate = True, is_security
        result.reasons.append("only changed by the triage bot since the last run")
        return result

    if is_security:
        result.category, result.priority, result.escalate = "security", "P0", True
        result.labels += ["type:security", "priority:P0"]
        result.reasons.append(
            f"security words: {', '.join(hits[:3])}" if hits else "labelled type:security"
        )

    looks_like_bug = bool(
        _has_any(issue.rule_text, rules.get("bug_words", []))
    ) or "bug" in " ".join(issue.labels)
    if looks_like_bug and result.category != "security":
        missing = [
            part
            for part, words in rules.get("bug_needs", {}).items()
            if not _has_any(issue.full_body or issue.body, words)
        ]
        if missing:
            result.needs_info, result.missing = True, missing
            result.labels.append("needs-info")
            result.reasons.append(f"bug report missing: {', '.join(missing)}")

    return result
