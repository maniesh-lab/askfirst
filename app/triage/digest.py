"""The triage digest: one model, three renderers.

- **Markdown** for `$GITHUB_STEP_SUMMARY` and the run artifact
- **Discord embeds**, kept inside Discord's limits: at most 10 embeds and 6000 characters per
  message, title <= 256, description <= 4096, <= 25 fields, field value <= 1024. A long section
  is split across embeds and the embeds across messages (at most `MAX_MESSAGES`; the rest is
  summed up in one line). Colour follows the worst priority.
- **JSON** for the artifact

An empty window still gets a one-line digest, so a working schedule is visibly alive.
"""

from collections import Counter
from typing import Any

from app.runtime.hashing import stable_hash
from app.triage.actions import NOT_APPLIED, WAITING_VERDICTS
from app.triage.models import Digest, DigestWindow, PlannedAction, TriageResult

MAX_EMBEDS = 10
MAX_MESSAGE_CHARS = 6000
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_FIELD_VALUE = 1024
LINE_LIMIT = 300
# A digest bigger than this many messages is cut: Discord is a heads-up, and the full digest is
# in the step summary and the artifact.
MAX_MESSAGES = 4

COLORS = {"P0": 0xB60205, "P1": 0xD93F0B, "P2": 0xFBCA04, "P3": 0x0E8A16, None: 0x5865F2}


def build_digest(
    repo: str,
    window: DigestWindow,
    run_id: str,
    results: list[TriageResult],
    actions: list[PlannedAction],
    *,
    mode: str = "propose",
    tokens: int = 0,
    duration_s: float = 0.0,
    actions_url: str | None = None,
) -> Digest:
    live = [r for r in results if not r.skipped]
    by_category: dict[str, list[TriageResult]] = {}
    for r in live:
        if r.category and r.priority not in ("P0", "P1"):
            by_category.setdefault(r.category, []).append(r)
    return Digest(
        repo=repo,
        window=window,
        run_id=run_id,
        mode=mode,
        counts_by_priority=dict(Counter(r.priority for r in live if r.priority)),
        counts_by_category=dict(Counter(r.category for r in live if r.category)),
        urgent=sorted(
            [r for r in live if r.priority in ("P0", "P1")], key=lambda r: (r.priority, r.number)
        ),
        new_by_category=dict(sorted(by_category.items())),
        needs_info=[r for r in live if r.needs_info],
        duplicates=[r for r in live if r.duplicate_of],
        stale=[r for r in results if r.stale],
        # Only what a person still has to decide: not rejected, dropped, failed or skipped.
        proposals=[
            a for a in actions if a.verdict in WAITING_VERDICTS and not a.applied and not a.error
        ],
        applied=[a for a in actions if a.applied],
        failed=[a for a in actions if a.error and not a.applied and a.error != NOT_APPLIED],
        unclassified=[r for r in live if r.unclassified],
        actions_url=actions_url,
        tokens=tokens,
        duration_s=round(duration_s, 1),
        total_issues=len(live),
    )


def digest_hash(digest: Digest) -> str:
    """What the digest says, without run-specific details (run id, timings, tokens)."""
    data = digest.model_dump(
        mode="json", exclude={"run_id", "actions_url", "tokens", "duration_s", "window"}
    )
    data["window_start"] = digest.window.start.isoformat()
    return stable_hash(data)


def _issue_line(r: TriageResult, extra: str = "") -> str:
    tail = f" {extra}" if extra else ""
    line = (
        f"[#{r.number}]({r.url}) **{r.priority or '?'}** {r.title}{tail}"
        if r.url
        else f"#{r.number} {r.title}{tail}"
    )
    return line if len(line) <= LINE_LIMIT else line[: LINE_LIMIT - 1] + "…"


def sections(d: Digest) -> list[tuple[str, list[str]]]:
    """(heading, lines) in the order people should read them. Empty sections are left out."""
    out: list[tuple[str, list[str]]] = []
    if d.urgent:
        out.append(
            (
                "🚨 Urgent (P0/P1)",
                [
                    _issue_line(r, "**(escalated)**" if r.escalate else "")
                    + (f" — {r.summary}" if r.summary else "")
                    for r in d.urgent
                ],
            )
        )
    if d.proposals:
        out.append(("🙋 Waiting for a person", [f"`{a.id}` {a.summary}" for a in d.proposals]))
    if d.needs_info:
        out.append(
            (
                "❓ Needs info",
                [
                    _issue_line(r, f"(missing: {', '.join(r.missing) or 'details'})")
                    for r in d.needs_info
                ],
            )
        )
    if d.duplicates:
        out.append(
            (
                "🔁 Possible duplicates",
                [_issue_line(r, f"→ #{r.duplicate_of}") for r in d.duplicates],
            )
        )
    if d.stale:
        out.append(("🕸️ Stale", [_issue_line(r) for r in d.stale]))
    for category, items in d.new_by_category.items():
        out.append((f"New: {category}", [_issue_line(r) for r in items]))
    if d.applied:
        out.append(("✅ Applied automatically", [a.summary for a in d.applied]))
    if d.failed:
        out.append(("⚠️ Failed (not applied)", [f"{a.summary}: {a.error}" for a in d.failed]))
    if d.unclassified:
        out.append(("🤷 Unclassified (needs a human)", [_issue_line(r) for r in d.unclassified]))
    return out


def headline(d: Digest) -> str:
    start, end = d.window.start.strftime("%b %d %H:%M"), d.window.end.strftime("%b %d %H:%M UTC")
    if d.empty:
        return f"Triage digest for {d.repo}: no new or updated issues ({start} → {end})."
    prio = ", ".join(f"{k}: {d.counts_by_priority[k]}" for k in sorted(d.counts_by_priority))
    return (
        f"Triage digest for {d.repo}: {d.total_issues} issues ({start} → {end}). {prio}. "
        f"{len(d.proposals)} proposals waiting, {len(d.applied)} applied."
    )


def footer(d: Digest) -> str:
    parts = [f"run {d.run_id}", f"mode {d.mode}", f"{d.tokens} tokens", f"{d.duration_s}s"]
    return " · ".join(parts)


def render_markdown(d: Digest) -> str:
    lines = [f"## {headline(d)}", ""]
    if d.empty:
        lines += [f"_{footer(d)}_", ""]
        return "\n".join(lines)
    lines += ["| Priority | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(d.counts_by_priority.items())]
    lines.append("")
    for heading, items in sections(d):
        lines += [f"### {heading}", "", *[f"- {i}" for i in items], ""]
    run = f"[Actions run]({d.actions_url}) · " if d.actions_url else ""
    lines += [f"_{run}{footer(d)}_", ""]
    return "\n".join(lines)


def render_private_summary(d: Digest) -> str:
    """Counts only, no repo name, titles or links: for public places (an Actions step summary
    in a public repo) when the triaged repo is private."""
    start, end = d.window.start.strftime("%b %d %H:%M"), d.window.end.strftime("%b %d %H:%M UTC")
    prio = ", ".join(f"{k}: {v}" for k, v in sorted(d.counts_by_priority.items())) or "none"
    return (
        f"## Triage digest: {d.total_issues} issues ({start} → {end})\n\n"
        f"By priority: {prio}. {len(d.proposals)} proposals waiting, {len(d.applied)} applied.\n\n"
        "_The triaged repo is private, so issue details are only in Discord and the run's own "
        f"data folder. {footer(d)}_\n"
    )


def render_json(d: Digest) -> dict[str, Any]:
    return d.model_dump(mode="json")


def _chunks(lines: list[str], limit: int) -> list[str]:
    out, current = [], ""
    for line in lines:
        line = line[: limit - 1]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def embed_size(embed: dict[str, Any]) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
    return size + len(embed.get("footer", {}).get("text", ""))


def render_discord(
    d: Digest, *, description_limit: int = MAX_DESCRIPTION
) -> list[list[dict[str, Any]]]:
    """Messages, each a list of embeds, all within Discord's limits."""
    worst = min(d.counts_by_priority, default=None) if d.counts_by_priority else None
    header: dict[str, Any] = {
        "title": headline(d)[:MAX_TITLE],
        "color": COLORS.get(worst, COLORS[None]),
        "footer": {"text": footer(d)[:2048]},
    }
    if d.actions_url:
        header["url"] = d.actions_url
    if not d.empty:
        header["fields"] = [
            {
                "name": "By priority",
                "value": (
                    ", ".join(f"{k}: {v}" for k, v in sorted(d.counts_by_priority.items())) or "-"
                )[:MAX_FIELD_VALUE],
                "inline": True,
            },
            {
                "name": "By category",
                "value": (
                    ", ".join(f"{k}: {v}" for k, v in sorted(d.counts_by_category.items())) or "-"
                )[:MAX_FIELD_VALUE],
                "inline": True,
            },
        ][:MAX_FIELDS]
    embeds = [header]
    for heading, items in sections(d):
        parts = _chunks(items, min(description_limit, MAX_DESCRIPTION))
        for n, part in enumerate(parts):
            title = heading if n == 0 else f"{heading} (cont.)"
            color = COLORS["P0"] if heading.startswith("🚨") else COLORS[None]
            embeds.append({"title": title[:MAX_TITLE], "description": part, "color": color})
    messages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for embed in embeds:
        e_size = embed_size(embed)
        if current and (len(current) == MAX_EMBEDS or size + e_size > MAX_MESSAGE_CHARS):
            messages.append(current)
            current, size = [], 0
        current.append(embed)
        size += e_size
    if current:
        messages.append(current)
    return cap_messages(messages, d)


def cap_messages(
    messages: list[list[dict[str, Any]]], d: Digest, limit: int = MAX_MESSAGES
) -> list[list[dict[str, Any]]]:
    """Keep at most `limit` messages; the last embed says how much was left out and where."""
    if len(messages) <= limit:
        return messages
    kept = [list(m) for m in messages[:limit]]
    left_out = sum(len(m) for m in messages[limit:])
    where = f" See the [Actions run]({d.actions_url})." if d.actions_url else ""
    note = {
        "title": "Digest cut short",
        "description": f"{left_out} more sections are not shown here.{where}",
        "color": COLORS[None],
    }
    last = kept[-1]
    if len(last) == MAX_EMBEDS or sum(map(embed_size, last)) + embed_size(note) > MAX_MESSAGE_CHARS:
        left_out += 1
        note["description"] = f"{left_out} more sections are not shown here.{where}"
        last.pop()
    last.append(note)
    return kept
