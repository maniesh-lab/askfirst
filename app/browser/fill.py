"""Build a fill plan and carry it out by rule, reading back every value.

A `FillPlan` is the exact list of things the agent will type, pick and tick, step by step, plus
the one final button it will click after approval. It is plain data, so it can be checkpointed,
shown to a reviewer, hashed, and replayed in a fresh browser after a restart.

`plan_hash` covers the page shapes (schema hashes), every step's key, kind and value (secret
values replaced by a marker), the final button and the profile version. Change any of those and
an old approval no longer matches.
"""

import re
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import Page
from pydantic import BaseModel
from pydantic import Field as PField

from app.browser.detect import resolve
from app.browser.mapping import Mapping, format_value, match_option, safe_file
from app.browser.schema import CHOICE_KINDS, Button, Field, FormStep, Locator, Option
from app.runtime.hashing import stable_hash

log = structlog.get_logger(__name__)

TEXT_FILL_KINDS = {
    "text",
    "email",
    "tel",
    "url",
    "number",
    "search",
    "password",
    "textarea",
    "date",
    "datetime-local",
    "month",
    "time",
    "week",
}


class FillStep(BaseModel):
    step_index: int
    key: str
    label: str
    kind: str
    value: Any
    source: str
    confidence: float = 0.0
    locator: Locator
    options: list[Option] = PField(default_factory=list)
    secret: bool = False
    flags: list[str] = PField(default_factory=list)


class FinalButton(BaseModel):
    step_index: int
    text: str
    locator: Locator
    form_action: str = ""
    form_method: str = "post"


class FillPlan(BaseModel):
    url: str
    steps: list[FillStep] = PField(default_factory=list)
    # Text of the "next" buttons clicked after each step, in order.
    next_buttons: list[str] = PField(default_factory=list)
    final_button: FinalButton | None = None
    schema_hashes: list[str] = PField(default_factory=list)
    profile_version: str = ""
    plan_hash: str = ""

    def for_step(self, index: int) -> list[FillStep]:
        return [s for s in self.steps if s.step_index == index]

    def compute_hash(self) -> str:
        return stable_hash(
            {
                "schemas": self.schema_hashes,
                "steps": [
                    [s.step_index, s.key, s.kind, "<secret>" if s.secret else s.value]
                    for s in self.steps
                ],
                "next": self.next_buttons,
                "final": self.final_button.text if self.final_button else None,
                "profile": self.profile_version,
            }
        )


def masked(step: FillStep) -> Any:
    return "********" if step.secret else step.value


def plan_steps(step: FormStep, mappings: list[Mapping], drafts: dict[str, str]) -> list[FillStep]:
    """Fill steps for one form step: every filled mapping plus every accepted draft."""
    out = []
    for f, m in zip(step.fields, mappings, strict=True):
        if not f.fillable or f.locator is None:
            continue
        if m.status == "filled":
            value, source, conf = m.value, m.source, m.confidence
        elif m.status == "needs_draft" and drafts.get(f.key):
            value, source, conf = drafts[f.key], "draft", 0.6
        else:
            continue
        out.append(
            FillStep(
                step_index=step.index,
                key=f.key,
                label=f.label,
                kind=f.kind,
                value=value,
                source=source,
                confidence=conf,
                locator=f.locator,
                options=f.options,
                secret=f.kind == "password",
                flags=list(m.flags),
            )
        )
    return out


def final_button_for(step: FormStep, wanted_text: str | None) -> FinalButton | None:
    """The one irreversible button. A site profile's exact name wins when it is on the page."""
    submits = step.buttons_of("submit")
    chosen: Button | None = None
    if wanted_text:
        chosen = next((b for b in submits if b.text == wanted_text), None)
    if chosen is None and len(submits) == 1:
        chosen = submits[0]
    if chosen is None or chosen.locator is None:
        return None
    return FinalButton(
        step_index=step.index,
        text=chosen.text,
        locator=chosen.locator,
        form_action=step.form_action,
        form_method=step.form_method or "post",
    )


# Carrying out the plan ------------------------------------------------------------------


class FillResult(BaseModel):
    key: str
    ok: bool
    attempts: int
    readback: Any = None
    error: str | None = None


def _option(step: FillStep, value: str) -> Option | None:
    return next((o for o in step.options if o.value == value), None)


async def _do(page: Page, step: FillStep) -> None:
    loc = resolve(page, step.locator)
    kind, value = step.kind, step.value
    if kind in TEXT_FILL_KINDS:
        await loc.fill(str(value))
    elif kind == "select":
        await loc.select_option(value=str(value))
    elif kind == "select_multiple":
        await loc.select_option(value=[str(v) for v in value])
    elif kind == "checkbox":
        await loc.set_checked(bool(value))
    elif kind == "radio":
        opt = _option(step, str(value))
        if not opt or not opt.css:
            raise ValueError(f"no option {value!r}")
        await page.locator(opt.css).check()
    elif kind == "checkbox_group":
        for opt in step.options:
            if opt.css:
                await page.locator(opt.css).set_checked(opt.value in value)
    elif kind == "file":
        await loc.set_input_files([str(Path(v)) for v in value])
    elif kind == "aria_radio":
        opt = _option(step, str(value))
        text = opt.text if opt else value
        radio = f'[role=radio][data-value="{value}"], [role=radio]:text-is("{text}")'
        await loc.locator(radio).first.click()
    elif kind == "aria_checkbox_group":
        for opt in step.options:
            box = loc.locator(f'[role=checkbox][data-value="{opt.value}"]').first
            checked = await box.get_attribute("aria-checked") == "true"
            if checked != (opt.value in value):
                await box.click()
    elif kind in {"aria_checkbox", "aria_switch"}:
        if (await loc.get_attribute("aria-checked") == "true") != bool(value):
            await loc.click()
    elif kind == "combobox":
        await loc.click()
        await loc.fill(str(value))
        controls = await loc.get_attribute("aria-controls")
        scope = page.locator(f"#{controls}") if controls else page
        await scope.get_by_role("option", name=str(value), exact=True).click()
    elif kind == "contenteditable":
        await loc.click()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Delete")
        await page.keyboard.insert_text(str(value))
    else:
        raise ValueError(f"don't know how to fill a {kind!r}")


async def read_back(page: Page, step: FillStep) -> Any:
    loc = resolve(page, step.locator)
    kind = step.kind
    if kind in TEXT_FILL_KINDS or kind in {"select", "combobox"}:
        return await loc.input_value()
    if kind == "select_multiple":
        return await loc.evaluate("el => Array.from(el.selectedOptions).map(o => o.value)")
    if kind == "checkbox":
        return await loc.is_checked()
    if kind == "radio":
        for opt in step.options:
            if opt.css and await page.locator(opt.css).is_checked():
                return opt.value
        return ""
    if kind == "checkbox_group":
        return [o.value for o in step.options if o.css and await page.locator(o.css).is_checked()]
    if kind == "file":
        return await loc.evaluate("el => Array.from(el.files).map(f => f.name)")
    if kind == "aria_radio":
        return await loc.evaluate(
            "el => (el.querySelector('[role=radio][aria-checked=true]') || {dataset: {}})"
            ".dataset.value || ''"
        )
    if kind == "aria_checkbox_group":
        return await loc.evaluate(
            "el => Array.from(el.querySelectorAll('[role=checkbox][aria-checked=true]'))"
            ".map(b => b.dataset.value)"
        )
    if kind in {"aria_checkbox", "aria_switch"}:
        return await loc.get_attribute("aria-checked") == "true"
    if kind == "contenteditable":
        return (await loc.inner_text()).strip()
    return None


def matches(step: FillStep, got: Any) -> bool:
    want = step.value
    if step.kind == "file":
        return sorted(got or []) == sorted(Path(v).name for v in want)
    if isinstance(want, list):
        return sorted(map(str, got or [])) == sorted(map(str, want))
    if isinstance(want, bool):
        return bool(got) is want
    if step.kind == "number":
        return _num(got) == _num(want)
    return str(got).strip() == str(want).strip()


def _num(v: Any) -> str:
    return re.sub(r"\.0+$", "", str(v).strip())


async def fill_step(page: Page, steps: list[FillStep]) -> list[FillResult]:
    """Fill each value, read it back, and retry a mismatch once before flagging it."""
    results = []
    for step in steps:
        error, got, attempts = None, None, 0
        for attempt in (1, 2):
            attempts = attempt
            try:
                await _do(page, step)
                got = await read_back(page, step)
                error = None
                if matches(step, got):
                    break
                error = "value did not read back"
            except Exception as exc:  # noqa: BLE001 - any Playwright failure is a fill failure
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        ok = error is None
        results.append(
            FillResult(
                key=step.key,
                ok=ok,
                attempts=attempts,
                readback="********" if step.secret else got,
                error=error,
            )
        )
        log.info(
            "field_filled" if ok else "field_fill_failed",
            key=step.key,
            attempts=attempts,
            error=error,
        )
    return results


async def verify_all(page: Page, steps: list[FillStep]) -> list[str]:
    """Keys whose current value doesn't match the plan."""
    bad = []
    for step in steps:
        try:
            if not matches(step, await read_back(page, step)):
                bad.append(step.key)
        except Exception:  # noqa: BLE001
            bad.append(step.key)
    return bad


def apply_edits(
    plan: FillPlan, edits: dict[str, Any], fields: dict[str, tuple[int, Field]], reviewer: str
) -> list[str]:
    """Apply a reviewer's `key -> value` edits to the plan, by the same rules as mapping (choices
    must match an option, files must be under profiles/files/). Returns the problems."""
    problems = []
    for key, raw in edits.items():
        if key not in fields:
            problems.append(f"{key}: no such field")
            continue
        step_index, field = fields[key]
        if not field.fillable or field.locator is None:
            problems.append(f"{key}: field can't be filled")
            continue
        if field.kind == "password":
            problems.append(f"{key}: passwords can't be edited here")
            continue
        value: Any = raw
        if field.kind in CHOICE_KINDS:
            wanted = raw if isinstance(raw, list) else str(raw).split(";")
            matched = [match_option(field, w) for w in wanted]
            if any(m is None for m in matched):
                problems.append(f"{key}: {raw!r} matches none of the options")
                continue
            value = (
                matched
                if field.kind.endswith("group") or field.kind == "select_multiple"
                else matched[0]
            )
        else:
            value = format_value(field, raw)
            if field.kind == "file" and not all(safe_file(v) for v in value):
                problems.append(f"{key}: file must be under profiles/files/")
                continue
            if isinstance(value, str) and field.maxlength and len(value) > field.maxlength:
                problems.append(f"{key}: longer than {field.maxlength} characters")
                continue
        existing = next((s for s in plan.steps if s.key == key), None)
        if existing is not None:
            existing.value, existing.source, existing.confidence = value, f"human:{reviewer}", 1.0
            existing.flags = [f for f in existing.flags if f != "draft_failed"]
        else:
            plan.steps.append(
                FillStep(
                    step_index=step_index,
                    key=key,
                    label=field.label,
                    kind=field.kind,
                    value=value,
                    source=f"human:{reviewer}",
                    confidence=1.0,
                    locator=field.locator,
                    options=field.options,
                )
            )
    plan.steps.sort(key=lambda s: s.step_index)
    return problems
