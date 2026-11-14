"""Possible duplicates: for each issue, the 3 most similar open issues (stdlib only).

Similarity mixes `difflib`'s title ratio with word overlap (Jaccard) on title + body. The LLM sees
these candidates and may only confirm one of them; it can never name a duplicate outside the list.
"""

import re
from difflib import SequenceMatcher

from app.triage.models import Issue

STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "it",
    "when",
    "with",
    "i",
    "my",
    "this",
    "that",
    "be",
    "not",
    "does",
    "do",
    "can",
    "at",
    "from",
    "after",
    "but",
    "are",
    "was",
    "have",
    "has",
    "how",
    "what",
    "you",
    "we",
    "our",
}
THRESHOLD = 0.35
TOP = 3


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def similarity(a: Issue, b: Issue) -> float:
    title = SequenceMatcher(None, a.title.lower(), b.title.lower()).ratio()
    wa, wb = words(a.text), words(b.text)
    overlap = len(wa & wb) / len(wa | wb) if wa and wb else 0.0
    return round(0.6 * title + 0.4 * overlap, 3)


def candidates(issue: Issue, others: list[Issue], top: int = TOP) -> list[tuple[int, float]]:
    """(number, score) of the most similar *older* open issues above the threshold."""
    scored = [
        (o.number, similarity(issue, o))
        for o in others
        if o.number != issue.number
        and o.state == "open"
        # Older only. GitHub times are to the second, so a tie goes to the lower number: two
        # issues filed in the same second can't each be proposed as the other's duplicate.
        and (o.created_at, o.number) < (issue.created_at, issue.number)
    ]
    scored = [s for s in scored if s[1] >= THRESHOLD]
    return sorted(scored, key=lambda s: (-s[1], s[0]))[:top]
