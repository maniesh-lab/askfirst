"""The review packet for a form run, and the files a person reads before deciding.

Writes into the run folder:
- `review.json`: the packet as data (what the CLI and the approval gate use)
- `review.html`: one self-contained page: the fields table (drafts highlighted, flags shown,
  secrets masked), what still needs a person, warnings, and the step screenshots (linked by
  relative path so the folder can be moved or zipped)

No personal data goes to Discord: the optional "review waiting" ping only has the run ID, the
site and field counts.
"""

import html
import json
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic import Field as PField

from app.browser.fill import FillPlan, masked
from app.runtime.approval import ProposedAction, ReviewPacket, Risk
from app.runtime.runs import RunPaths


class ReviewRow(BaseModel):
    step: int
    key: str
    label: str
    value: Any = None
    source: str = ""
    confidence: float = 0.0
    status: str = "filled"
    required: bool = False
    flags: list[str] = PField(default_factory=list)


def build_rows(state: dict[str, Any]) -> list[ReviewRow]:
    plan = FillPlan.model_validate(state["plan"])
    by_key = {s.key: s for s in plan.steps}
    rows = []
    for step in state.get("schema", []):
        for f in step["fields"]:
            if not (f["visible"] and not f["disabled"] and not f["readonly"] and not f["honeypot"]):
                continue
            key = f["key"]
            planned = by_key.get(key)
            mapping = next(
                (m for m in state["mappings"].get(str(step["index"]), []) if m["key"] == key), {}
            )
            flags = list(planned.flags if planned else mapping.get("flags", []))
            if key in state.get("draft_failed", []):
                flags.append("draft_failed")
            if key in state.get("fill_failed", []):
                flags.append("fill_failed")
            if planned is not None:
                rows.append(
                    ReviewRow(
                        step=step["index"],
                        key=key,
                        label=f["label"],
                        value=masked(planned),
                        source=planned.source,
                        confidence=planned.confidence,
                        status="filled",
                        required=f["required"],
                        flags=flags,
                    )
                )
            else:
                status = mapping.get("status", "needs_human")
                if f["required"]:
                    flags.append("required_missing")
                rows.append(
                    ReviewRow(
                        step=step["index"],
                        key=key,
                        label=f["label"],
                        status=status,
                        required=f["required"],
                        flags=flags,
                        source=mapping.get("reason", ""),
                    )
                )
    return rows


def build_packet(state: dict[str, Any]) -> ReviewPacket:
    """Pure: the same state always gives the same packet (the gate calls this on every resume)."""
    plan = FillPlan.model_validate(state["plan"])
    rows = build_rows(state)
    button = plan.final_button.text if plan.final_button else "(none found)"
    action = ProposedAction(
        id=f"{state['run_id']}-submit",
        kind="submit_form",
        target=plan.url,
        params={"button": button},
        risk=Risk.IRREVERSIBLE,
        summary=f"Click '{button}' on {plan.url}",
    )
    flagged = [r for r in rows if r.flags]
    details = {
        "url": plan.url,
        "target": state.get("target", ""),
        "rows": [r.model_dump() for r in rows],
        "counts": {
            "filled": sum(1 for r in rows if r.status == "filled"),
            "drafted": sum(1 for r in rows if r.source == "draft"),
            "needs_human": sum(1 for r in rows if r.status != "filled"),
            "flagged": len(flagged),
        },
        "warnings": list(state.get("warnings", [])),
        "screenshots": list(state.get("screenshots", [])),
        "schema_hashes": plan.schema_hashes,
        "final_button": button,
        "steps": len(state.get("schema", [])),
    }
    return ReviewPacket(
        run_id=state["run_id"],
        kind="form",
        title=f"Submit form: {state.get('page_title') or plan.url}",
        actions=[action],
        details=details,
        plan_hash=plan.plan_hash,
        created_at=state.get("packet_time") or state.get("started_at"),
    )


def is_clean(packet: ReviewPacket) -> bool:
    """No flags, no warnings, nothing left for a person, and nothing the LLM wrote: safe for
    `--all-clean` batch approval. A drafted answer is words a person should read before it goes
    out under their name, so a run with drafts is approved one by one."""
    d = packet.details
    return (
        not d.get("warnings")
        and d.get("counts", {}).get("flagged", 1) == 0
        and d.get("counts", {}).get("needs_human", 1) == 0
        and d.get("counts", {}).get("drafted", 1) == 0
    )


def write_review_files(paths: RunPaths, packet: ReviewPacket) -> None:
    paths.ensure()
    paths.review_json.write_text(packet.model_dump_json(indent=2), encoding="utf-8")
    paths.review_html.write_text(render_html(packet), encoding="utf-8")


CSS = """
body { font: 15px/1.5 system-ui, sans-serif; margin: 24px auto; max-width: 1000px;
       padding: 0 16px; color: #1d2433; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d7dbe3; padding: 6px 8px; vertical-align: top; text-align: left; }
tr.draft td { background: #fff8e1; }
tr.todo td { background: #fdecea; }
.flag { background: #b42318; color: #fff; border-radius: 4px; padding: 0 6px; font-size: 12px;
        margin-right: 4px; }
.box { border: 1px solid #d7dbe3; border-radius: 8px; padding: 12px 16px; margin: 16px 0; }
code { color: #5b6475; font-size: 12px; }
figure { display: inline-block; margin: 8px; }
img { max-width: 300px; border: 1px solid #d7dbe3; }
pre { background: #f4f5f7; padding: 8px; overflow-x: auto; }
"""


def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(map(str, value))
    elif isinstance(value, bool):
        value = "yes (ticked)" if value else "no"
    return html.escape("" if value is None else str(value)).replace("\n", "<br>")


def render_html(packet: ReviewPacket) -> str:
    d = packet.details
    e = html.escape
    rows = []
    for r in d["rows"]:
        cls = "draft" if r["source"] == "draft" else ("todo" if r["status"] != "filled" else "")
        if r["flags"]:
            cls += " flagged"
        flags = " ".join(f'<span class="flag">{e(f)}</span>' for f in r["flags"])
        value = _cell(r["value"]) if r["status"] == "filled" else f"<em>{e(r['status'])}</em>"
        rows.append(
            f'<tr class="{cls}"><td>{r["step"] + 1}</td><td>{e(r["label"])}'
            f"{' *' if r['required'] else ''}<br><code>{e(r['key'])}</code></td>"
            f"<td>{value}</td>"
            f"<td>{e(r['source'])}</td><td>{flags}</td></tr>"
        )
    warnings = "".join(f"<li>{e(w)}</li>" for w in d.get("warnings", [])) or "<li>none</li>"
    shots = "".join(
        f'<figure><a href="{e(s)}"><img src="{e(s)}" alt="step screenshot"></a>'
        f"<figcaption>{e(s)}</figcaption></figure>"
        for s in d.get("screenshots", [])
    )
    c = d["counts"]
    run = e(packet.run_id)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Review {run}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{CSS}</style></head><body>
<h1>Review before submitting</h1>
<p><strong>Run</strong> {run} &middot; <strong>Page</strong> {e(d["url"])}</p>
<div class="box"><strong>The agent will click:</strong> "{e(d["final_button"])}" (irreversible).
Nothing has been sent yet.<br>{c["filled"]} filled ({c["drafted"]} drafted by the LLM, yellow),
{c["needs_human"]} left for you (red), {c["flagged"]} flagged.</div>
<h2>Warnings</h2><ul>{warnings}</ul>
<h2>Fields</h2>
<table><thead><tr><th>Step</th><th>Field</th><th>Value</th><th>Source</th><th>Flags</th></tr>
</thead><tbody>
{"".join(rows)}
</tbody></table>
<h2>Decide</h2>
<pre>python -m app.cli review approve {run} --plan-hash {e(packet.plan_hash[:16])}
python -m app.cli review reject {run} --reason "..."
python -m app.cli review edit {run} --set field_key="new value"</pre>
<p><code>plan hash {e(packet.plan_hash[:16])}</code></p>
<h2>Screenshots</h2>{shots or "<p>none</p>"}
</body></html>
"""


def ping_text(packet: ReviewPacket) -> str:
    c = packet.details["counts"]
    target = str(packet.details.get("target") or "form")
    # A bare URL could carry personal data in its query string; send only the host.
    site = target if target.startswith("site:") else (urlsplit(target).hostname or "form")
    return (
        f"Review waiting: `{packet.run_id}` ({site}). "
        f"{c['filled']} filled, {c['drafted']} drafted, "
        f"{c['needs_human']} need you, {c['flagged']} flagged. "
        f"Run `python -m app.cli review show {packet.run_id}`."
    )


def summary_json(packet: ReviewPacket) -> str:
    return json.dumps(packet.details["counts"])
