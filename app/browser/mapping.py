"""Fill everything possible from the profile by rule. No AI in this file.

Each field gets one `Mapping`: a value (or nothing), where it came from (`source`), how sure the
rule is (`confidence`), and a status:
- `filled`: a rule found the value
- `needs_draft`: a free-text question with no fixed answer; the LLM drafts it in the next step
- `needs_human`: no rule knows this field, so it is left for the reviewer. **Never guessed.**
- `skipped`: not fillable (hidden, disabled, readonly, honeypot), or optional and empty on purpose

Rules, in order (the first that gives a value wins):
1. the run's data row (marketplace batch mode), matched by field key or name
2. the site profile's override for this field (an explicit choice beats any heuristic)
3. the `autocomplete` attribute (`given-name`, `email`, `tel`, ...)
4. the label / name / id synonym table (English)
5. the input type (`email`, `tel`)
6. a canned answer whose pattern matches the question
Choices (select, radio, checkboxes) must then match one of the field's own options; a value that
matches none becomes `needs_human`. Values are formatted for the input type.
"""

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PField

from app.browser.schema import BOOL_KINDS, CHOICE_KINDS, Field, FormStep
from app.runtime.hashing import stable_hash

Status = Literal["filled", "needs_draft", "needs_human", "skipped"]
FILES_ROOT = Path("profiles/files")


class CannedAnswer(BaseModel):
    pattern: str
    answer: str


class Profile(BaseModel):
    """The persona the agent fills forms for. Loaded from YAML; extra sections are kept."""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    identity: dict[str, Any] = PField(default_factory=dict)
    contact: dict[str, Any] = PField(default_factory=dict)
    address: dict[str, Any] = PField(default_factory=dict)
    links: dict[str, Any] = PField(default_factory=dict)
    work: dict[str, Any] = PField(default_factory=dict)
    education: list[dict[str, Any]] = PField(default_factory=list)
    skills: list[str] = PField(default_factory=list)
    answers: list[CannedAnswer] = PField(default_factory=list)
    files: dict[str, Any] = PField(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def get(self, path: str) -> Any:
        """`profile.get("contact.email")`; None when any part is missing."""
        node: Any = self.model_dump()
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return None
        return node

    def version_hash(self) -> str:
        return stable_hash(self.model_dump())[:12]

    def facts_text(self) -> str:
        """Every value in the profile as one string (the fact checker looks things up here)."""
        out: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif node is not None:
                out.append(str(node))

        walk(self.model_dump())
        return "\n".join(out)


class Mapping(BaseModel):
    key: str
    status: Status
    value: Any = None
    source: str = ""
    confidence: float = 0.0
    reason: str = ""
    flags: list[str] = PField(default_factory=list)


# Only specific autocomplete tokens. A generic one like "url" says nothing about *which* link
# (LinkedIn? portfolio?), so those fields go to the synonym table instead.
AUTOCOMPLETE = {
    "given-name": "identity.first_name",
    "family-name": "identity.last_name",
    "name": "identity.full_name",
    "email": "contact.email",
    "username": "contact.email",
    "tel": "contact.phone",
    "tel-national": "contact.phone",
    "street-address": "address.street",
    "address-line1": "address.street",
    "address-level2": "address.city",
    "address-level1": "address.region",
    "postal-code": "address.postal_code",
    "country": "address.country_code",
    "country-name": "address.country",
    "organization-title": "work.current_title",
}

# (pattern over the normalised label + name + id, profile path). First match wins.
SYNONYMS: list[tuple[str, str]] = [
    (r"\b(first name|given name|fname|forename)\b", "identity.first_name"),
    (r"\b(last name|surname|family name|lname)\b", "identity.last_name"),
    # Not a bare "name" anywhere: "Company name" or "Reference name" is not the person's name.
    # Only a field that is just "Name" (label, name and id all "name") counts.
    (r"\b(full name|your name|applicant name|display name)\b|^(name ?)+$", "identity.full_name"),
    (r"\b(e ?mail|email address)\b", "contact.email"),
    (r"\b(phone|mobile|telephone|cell)\b", "contact.phone"),
    (r"\b(street|address line|address)\b", "address.street"),
    (r"\b(city|town|location)\b", "address.city"),
    (r"\b(postal|zip|post ?code)\b", "address.postal_code"),
    (r"\b(state|province|region)\b", "address.region"),
    (r"\bcountry\b", "address.country"),
    (r"\blinkedin\b", "links.linkedin"),
    (r"\bgithub\b", "links.github"),
    (r"\b(website|portfolio|homepage)\b", "links.website"),
    (r"\byears? of experience|experience years?\b", "work.years_experience"),
    (r"\b(start date|available from|earliest start|availability date)\b", "work.earliest_start"),
    (
        r"\b(allowed|authori[sz]ed|eligible|permitted) to work|work authori[sz]ation\b",
        "work.authorization",
    ),
    (r"\b(job title|current title|current role)\b", "work.current_title"),
    (r"\b(resume|cv|curriculum vitae)\b", "files.resume"),
    (r"\b(photos?|images?|pictures?)\b", "files.photos"),
]

# Option synonyms, both ways ("Nepal" matches an option "NP" and the reverse).
OPTION_SYNONYMS = [
    {"nepal", "np", "npl"},
    {"india", "in", "ind"},
    {"united states", "us", "usa", "united states of america"},
    {"yes", "y", "true"},
    {"no", "n", "false"},
]

QUESTION_START = re.compile(
    r"^(why|how|what|describe|tell|explain|which|share|briefly|anything)\b", re.I
)
BOOL_TRUE = {"yes", "true", "1", "on", "y", "checked"}


def norm(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _haystack(field: Field) -> str:
    return " ".join(
        norm(x)
        for x in (field.label, field.name.replace("_", " "), field.id.replace("_", " "))
        if x
    )


def match_option(field: Field, value: Any) -> str | None:
    """The option value that matches `value`, or None. Exact value, exact text, normalised
    text, synonyms, then "option text starts with the value" (e.g. "yes" -> "Yes, I can...")."""
    want = norm(value)
    if not want:
        return None
    options = [o for o in field.options if o.value != "" or o.text]
    for o in options:
        if str(value) == o.value or str(value) == o.text:
            return o.value
    for o in options:
        if want in (norm(o.value), norm(o.text)):
            return o.value
    for group in OPTION_SYNONYMS:
        if want in group:
            for o in options:
                if norm(o.value) in group or norm(o.text) in group:
                    return o.value
    for o in options:
        if o.value and re.match(rf"{re.escape(want)}\b", norm(o.text)):
            return o.value
    return None


def format_value(field: Field, value: Any) -> Any:
    """Shape a profile value for the input type."""
    if value is None:
        return None
    if field.kind == "date":
        return value.isoformat() if isinstance(value, date) else str(value)[:10]
    if field.kind == "month":
        return str(value)[:7]
    if field.kind == "number":
        return re.sub(r"[^0-9.\-]", "", str(value))
    if field.kind in BOOL_KINDS:
        return norm(value) in BOOL_TRUE if not isinstance(value, bool) else value
    if field.kind == "file":
        items = value if isinstance(value, list) else [value]
        return [str(v) for v in items if v]
    if isinstance(value, list) and field.kind not in CHOICE_KINDS:
        return ", ".join(str(v) for v in value)
    return value if isinstance(value, list) else str(value)


def is_free_text(field: Field) -> bool:
    if field.kind in {"textarea", "contenteditable"}:
        return True
    if field.kind == "text":
        label = field.label.strip()
        return label.endswith("?") or bool(QUESTION_START.match(label)) and len(label) > 15
    return False


def safe_file(path: str) -> bool:
    """Uploads only from profiles/files/, never anywhere else on disk."""
    try:
        resolved = Path(path).resolve()
        return resolved.is_relative_to(FILES_ROOT.resolve()) and resolved.is_file()
    except OSError:
        return False


def _canned(profile: Profile, field: Field) -> str | None:
    text = field.label or field.name
    for item in profile.answers:
        if re.search(item.pattern, text, re.IGNORECASE):
            return item.answer
    return None


def _candidate(
    field: Field, profile: Profile, overrides: dict[str, str], data: dict[str, Any]
) -> tuple[Any, str, float] | None:
    for k in (field.key, field.name):
        if k and k in data and data[k] not in (None, ""):
            return data[k], f"data:{k}", 1.0
    if field.key in overrides:
        path = overrides[field.key]
        # A site override that points at nothing is a config mistake: never type "None".
        if profile.get(path) is None:
            return None
        return profile.get(path), f"override:{path}", 0.99
    if field.autocomplete in AUTOCOMPLETE:
        path = AUTOCOMPLETE[field.autocomplete]
        if profile.get(path) is not None:
            return profile.get(path), f"autocomplete:{path}", 0.95
    hay = _haystack(field)
    if is_question(field):
        # "State why you want this job" or "Describe your work in this city" mention words
        # from the synonym table, but they are questions for a draft (or a person).
        return _canned_candidate(profile, field)
    for pattern, path in SYNONYMS:
        if re.search(pattern, hay) and profile.get(path) is not None:
            if path.startswith("files.") and field.kind != "file":
                continue
            if field.kind == "file" and not path.startswith("files."):
                continue
            return profile.get(path), f"synonym:{path}", 0.85
    if field.kind == "email" and profile.get("contact.email"):
        return profile.get("contact.email"), "type:email", 0.8
    if field.kind == "tel" and profile.get("contact.phone"):
        return profile.get("contact.phone"), "type:tel", 0.8
    return _canned_candidate(profile, field)


def _canned_candidate(profile: Profile, field: Field) -> tuple[Any, str, float] | None:
    canned = _canned(profile, field)
    return (canned, "canned", 0.9) if canned is not None else None


def is_question(field: Field) -> bool:
    """A free-text question rather than a fact field. A short textarea label such as "Address"
    or "Mailing address" is still a fact field; a longer one is a question."""
    if not is_free_text(field):
        return False
    if field.kind == "text":
        return True  # is_free_text already saw a question in the label
    return len(norm(field.label).split()) > 3 or field.label.strip().endswith("?")


def map_field(
    field: Field,
    profile: Profile,
    *,
    overrides: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> Mapping:
    if not field.fillable:
        why = (
            "honeypot"
            if field.honeypot
            else ", ".join(field.hidden_reasons) or "disabled or readonly"
        )
        return Mapping(key=field.key, status="skipped", reason=f"not fillable ({why})")
    if field.kind == "password":
        return Mapping(
            key=field.key, status="needs_human", reason="passwords come only from the login step"
        )
    for k in (field.key, field.name):
        if k and data and k in data and data[k] in (None, ""):
            # The batch row has this column but leaves it empty: a person decides, rather than
            # quietly using the persona's value (a stock photo on a cookbook listing).
            return Mapping(
                key=field.key, status="needs_human", reason=f"the data row leaves {k} empty"
            )
    found = _candidate(field, profile, overrides or {}, data or {})
    if found is None:
        if is_free_text(field):
            return Mapping(key=field.key, status="needs_draft", reason="free-text question")
        return Mapping(key=field.key, status="needs_human", reason="no rule knows this field")
    raw, source, confidence = found
    flags: list[str] = []
    if field.kind in BOOL_KINDS and source == "canned":
        flags.append("agreement")
    if field.kind in CHOICE_KINDS:
        wanted = (
            raw
            if isinstance(raw, list)
            else str(raw).split(";")
            if field.kind.endswith("group")
            else [raw]
        )
        matched = [match_option(field, w) for w in wanted]
        if any(m is None for m in matched) or not matched:
            return Mapping(
                key=field.key,
                status="needs_human",
                source=source,
                reason=f"{raw!r} matches none of the options",
            )
        value: Any = (
            matched
            if field.kind.endswith("group") or field.kind == "select_multiple"
            else matched[0]
        )
    else:
        value = format_value(field, raw)
        if field.kind == "file":
            bad = [v for v in value if not safe_file(v)]
            if bad or not value:
                return Mapping(
                    key=field.key, status="needs_human", reason="file must be under profiles/files/"
                )
    if isinstance(value, str) and field.maxlength and len(value) > field.maxlength:
        return Mapping(
            key=field.key,
            status="needs_human",
            source=source,
            reason="value is longer than maxlength",
        )
    return Mapping(
        key=field.key,
        status="filled",
        value=value,
        source=source,
        confidence=confidence,
        flags=flags,
    )


def map_step(
    step: FormStep,
    profile: Profile,
    *,
    overrides: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> list[Mapping]:
    return [map_field(f, profile, overrides=overrides, data=data) for f in step.fields]
