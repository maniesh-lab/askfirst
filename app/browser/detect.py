"""Turn a live page into a `FormStep`: every control, its label, kind and constraints, the main
form's buttons, and a locator for each that is checked to match exactly one element.

The page-side work is in `detect.js` (run with `page.evaluate`). This module adds what needs
Python: picking the main form, classifying buttons, and checking locators are unique.
"""

import re
from importlib import resources
from typing import Any

from playwright.async_api import Page

from app.browser.schema import Button, ButtonKind, Field, FormStep, Locator, Option, assign_keys

_JS = resources.files("app.browser").joinpath("detect.js").read_text("utf-8")

# Words that mean "this button does the irreversible thing".
SUBMIT_WORDS = re.compile(
    r"\b(submit|send|apply|publish|post|place order|order|pay|confirm|sign ?up|subscribe|"
    r"book|request|register|save changes|finish|complete|log ?in|sign ?in|purchase|buy)\b",
    re.IGNORECASE,
)
NEXT_WORDS = re.compile(r"\b(next|continue|review|proceed|go on)\b", re.IGNORECASE)
BACK_WORDS = re.compile(r"\b(back|previous|prev)\b", re.IGNORECASE)
DRAFT_WORDS = re.compile(r"\b(save (as )?draft|draft)\b", re.IGNORECASE)

# Our kinds -> the ARIA role Playwright's get_by_role uses.
ROLE_OF_KIND = {
    "text": "textbox",
    "email": "textbox",
    "tel": "textbox",
    "url": "textbox",
    "textarea": "textbox",
    "search": "searchbox",
    "number": "spinbutton",
    "select": "combobox",
    "select_multiple": "listbox",
    "checkbox": "checkbox",
    "aria_radio": "radiogroup",
    "combobox": "combobox",
    "aria_checkbox": "checkbox",
    "aria_switch": "switch",
    "contenteditable": "textbox",
}


def classify_button(
    text: str, html_type: str, overrides: dict[str, ButtonKind] | None = None
) -> ButtonKind:
    """`submit` (irreversible), `next`, `back`, `save_draft` or `other`. A site profile may
    override by exact button text."""
    if overrides and text in overrides:
        return overrides[text]
    if html_type == "reset":
        return "other"
    if DRAFT_WORDS.search(text):
        return "save_draft"
    if html_type == "submit":
        # A submit-type button sends the form, whatever it says, unless it is clearly navigation.
        if BACK_WORDS.search(text) and not SUBMIT_WORDS.search(text):
            return "back"
        if NEXT_WORDS.search(text) and not SUBMIT_WORDS.search(text):
            return "next"
        return "submit"
    if BACK_WORDS.search(text):
        return "back"
    if NEXT_WORDS.search(text) and not re.search(r"\bpreview\b", text, re.IGNORECASE):
        return "next"
    return "other"


def pick_main_form(raw: dict[str, Any]) -> int:
    """The form with the most visible fillable fields, skipping search forms. Ties (and steps
    where nothing is visible, like a final review step) go to the form with more fields. -1 =
    no form on the page."""
    searches = {f["index"] for f in raw["forms"] if f["is_search"]}
    scores: dict[int, list[int]] = {
        f["index"]: [0, 0] for f in raw["forms"] if f["index"] not in searches
    }
    for f in raw["fields"]:
        if f["form_index"] in scores:
            scores[f["form_index"]][1] += 1
            if f["visible"] and not f["disabled"]:
                scores[f["form_index"]][0] += 1
    if scores:
        return max(scores, key=lambda k: (scores[k][0], scores[k][1], -k))
    return -1


async def _unique(page: Page, loc: Locator) -> bool:
    try:
        return await resolve(page, loc).count() == 1
    except Exception:
        return False


def resolve(page: Page, loc: Locator):
    """A Playwright locator from our stored description."""
    if loc.strategy == "role":
        return page.get_by_role(loc.role, name=loc.name, exact=True)  # type: ignore[arg-type]
    if loc.strategy == "label":
        return page.get_by_label(loc.name or "", exact=True)
    return page.locator(loc.css or "")


async def best_locator(
    page: Page, role: str | None, name: str, css: str | None, raw_name: str | None = None
) -> Locator | None:
    """Role + name first, then label, then CSS: the first that matches exactly one element.
    `raw_name` is the label before the "*" was stripped (often the real accessible name)."""
    names = list(dict.fromkeys(n for n in (name, raw_name) if n))
    candidates = []
    if role:
        candidates += [Locator(strategy="role", role=role, name=n) for n in names]
    candidates += [Locator(strategy="label", name=n) for n in names]
    if css:
        candidates.append(Locator(strategy="css", css=css))
    for loc in candidates:
        if await _unique(page, loc):
            return loc
    return candidates[-1] if candidates else None


async def detect(
    page: Page, *, step_index: int = 0, button_overrides: dict[str, ButtonKind] | None = None
) -> FormStep:
    raw = await page.evaluate(_JS)
    main = pick_main_form(raw)
    # Document order is the order a person tabs through, which is what "visual order" means
    # for forms (pixel positions mislead when inline controls have different heights).
    raw_fields = [f for f in raw["fields"] if f["form_index"] == main]
    fields = [
        Field(
            key="",
            kind=f["kind"],
            label=f["label"],
            label_source=f["label_source"],
            name=f["name"],
            id=f["id"],
            role=f["role"],
            input_type=f["input_type"],
            autocomplete=f["autocomplete"],
            required=f["required"],
            options=[Option(**o) for o in f["options"]],
            constraints=f["constraints"],
            disabled=f["disabled"],
            readonly=f["readonly"],
            value=f["value"],
            visible=f["visible"],
            hidden_reasons=f["hidden_reasons"],
            honeypot=f["honeypot"],
            form_index=f["form_index"],
        )
        for f in raw_fields
    ]
    assign_keys(fields)
    for f, rf in zip(fields, raw_fields, strict=True):
        if f.fillable:
            css = rf["css"]
            f.locator = await best_locator(
                page, ROLE_OF_KIND.get(f.kind), f.label, css, rf.get("aria_name")
            )

    buttons = []
    for b in raw["buttons"]:
        if b["form_index"] != main:
            continue
        kind = classify_button(b["text"], b["html_type"], button_overrides)
        btn = Button(
            text=b["text"],
            kind=kind,
            html_type=b["html_type"],
            explicit_type=b["explicit_type"],
            tag=b["tag"],
            name=b["name"],
            value=b["value"],
            id=b["id"],
            form_index=b["form_index"],
            visible=b["visible"],
        )
        if btn.visible:
            btn.locator = await best_locator(page, "button", btn.text, b["css"])
        buttons.append(btn)

    form = next((f for f in raw["forms"] if f["index"] == main), None)
    step = FormStep(
        index=step_index,
        url=raw["url"],
        title=raw["title"],
        headings=raw["headings"],
        context=raw["context"],
        main_form=main,
        form_action=form["action"] if form else "",
        form_method=form["method"] if form else "get",
        fields=fields,
        buttons=buttons,
        hidden_inputs={
            h["name"]: h["value"]
            for h in raw["hidden_inputs"]
            if h["form_index"] == main and h["name"]
        },
    )
    step.schema_hash = step.compute_hash()
    return step
