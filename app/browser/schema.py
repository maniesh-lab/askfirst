"""The typed description of a form: fields, buttons, steps, and a hash of their shape.

Locators are stored as data (a strategy plus its values), never as live element handles, so a
schema can be saved in a checkpoint and used again in a fresh browser after a restart.

`schema_hash` covers what a person approved: each fillable field's key, kind, label, required
flag and option values. If the page changes any of those between approval and submit, the hash
changes and the agent refuses to submit.
"""

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel
from pydantic import Field as PField

from app.runtime.hashing import stable_hash

ButtonKind = Literal["submit", "next", "back", "save_draft", "other"]

# Kinds a person types into.
TEXT_KINDS = {
    "text",
    "email",
    "tel",
    "url",
    "number",
    "search",
    "password",
    "textarea",
    "contenteditable",
    "date",
    "datetime-local",
    "month",
    "time",
    "week",
}
CHOICE_KINDS = {
    "select",
    "select_multiple",
    "radio",
    "checkbox_group",
    "aria_radio",
    "aria_checkbox_group",
    "combobox",
}
BOOL_KINDS = {"checkbox", "aria_checkbox", "aria_switch"}


class Locator(BaseModel):
    """How to find an element again: by role + accessible name, by label, or by CSS."""

    strategy: Literal["role", "label", "css"]
    role: str | None = None
    name: str | None = None
    css: str | None = None

    def describe(self) -> str:
        if self.strategy == "role":
            return f"role={self.role}[name={self.name!r}]"
        if self.strategy == "label":
            return f"label={self.name!r}"
        return f"css={self.css}"


class Option(BaseModel):
    value: str
    text: str
    selected: bool = False
    group: str | None = None
    css: str | None = None


class Field(BaseModel):
    key: str
    kind: str
    label: str
    label_source: str = "none"
    name: str = ""
    id: str = ""
    role: str | None = None
    input_type: str = ""
    autocomplete: str = ""
    required: bool = False
    options: list[Option] = PField(default_factory=list)
    constraints: dict[str, Any] = PField(default_factory=dict)
    disabled: bool = False
    readonly: bool = False
    value: Any = ""
    visible: bool = True
    hidden_reasons: list[str] = PField(default_factory=list)
    honeypot: bool = False
    form_index: int = -1
    locator: Locator | None = None

    @property
    def fillable(self) -> bool:
        """Visible, enabled, editable and not a honeypot. Nothing else is ever touched."""
        return self.visible and not self.disabled and not self.readonly and not self.honeypot

    @property
    def maxlength(self) -> int | None:
        raw = self.constraints.get("maxlength")
        return int(raw) if raw and str(raw).isdigit() else None


class Button(BaseModel):
    text: str
    kind: ButtonKind
    html_type: str = "submit"
    explicit_type: bool = True
    tag: str = "button"
    name: str = ""
    value: str = ""
    id: str = ""
    form_index: int = -1
    visible: bool = True
    locator: Locator | None = None


class FormStep(BaseModel):
    index: int = 0
    url: str
    title: str = ""
    headings: list[str] = PField(default_factory=list)
    context: str = ""
    main_form: int = -1
    form_action: str = ""
    form_method: str = "get"
    fields: list[Field] = PField(default_factory=list)
    buttons: list[Button] = PField(default_factory=list)
    hidden_inputs: dict[str, str] = PField(default_factory=dict)
    schema_hash: str = ""

    def fillable_fields(self) -> list[Field]:
        return [f for f in self.fields if f.fillable]

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)

    def buttons_of(self, kind: ButtonKind) -> list[Button]:
        return [b for b in self.buttons if b.kind == kind and b.visible]

    def compute_hash(self) -> str:
        shape = sorted(
            (
                f.key,
                f.name,  # a renamed field sends the same value under another name
                f.kind,
                f.label,
                f.required,
                [o.value for o in f.options],
            )
            for f in self.fillable_fields()
        )
        # Also what would be sent besides the fields: hidden inputs (names only; a CSRF token
        # changes on every load), the method, and the buttons (name, value, kind).
        return stable_hash(
            {
                "fields": shape,
                "action": self.form_action,
                "method": (self.form_method or "get").lower(),
                "hidden": sorted(self.hidden_inputs),
                "buttons": sorted((b.kind, b.text, b.name, b.value) for b in self.buttons),
            }
        )


class FormSchema(BaseModel):
    url: str
    steps: list[FormStep] = PField(default_factory=list)

    @property
    def schema_hashes(self) -> list[str]:
        return [s.schema_hash for s in self.steps]


def slugify(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return text[:limit].rstrip("_")


def assign_keys(fields: list[Field]) -> None:
    """Stable keys: from the label, else the name, else the id. Duplicates get `_2`, `_3`."""
    seen: dict[str, int] = {}
    for f in fields:
        base = slugify(f.label) or slugify(f.name) or slugify(f.id) or f.kind
        seen[base] = seen.get(base, 0) + 1
        f.key = base if seen[base] == 1 else f"{base}_{seen[base]}"


def dedupe_keys(step: FormStep, earlier: list[FormStep]) -> None:
    """Keys of fillable fields must be unique across all steps of a form (edits and drafts are
    keyed by them). A fillable field whose key an earlier step already filled gets the step
    number added, e.g. `email_s2`. Hidden copies of other steps' fields are left alone."""
    taken = {f.key for s in earlier for f in s.fields if f.fillable}
    changed = False
    for f in step.fields:
        if f.fillable and f.key in taken:
            f.key = f"{f.key}_s{step.index + 1}"
            changed = True
    if changed:
        step.schema_hash = step.compute_hash()
