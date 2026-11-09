"""Draft validators, the fake drafter, fill-value matching and the review report."""

import json

from app.browser.draft import Drafts, fake_draft, invented_facts, make_validators
from app.browser.fill import FillStep, matches
from app.browser.mapping import Profile
from app.browser.review import ReviewRow, is_clean, ping_text, render_html
from app.browser.schema import Field, Locator
from app.runtime.approval import ReviewPacket

PROFILE = Profile.load("profiles/example.yaml")
FACTS = PROFILE.facts_text()


def fields() -> list[Field]:
    return [Field(key="why", kind="textarea", label="Why?", constraints={"maxlength": "40"})]


def run_validators(answers: dict) -> list[str]:
    d = Drafts(answers=answers)
    return [p for v in make_validators(fields(), FACTS) if (p := v(d))]


def test_validators_accept_a_good_draft() -> None:
    assert run_validators({"why": "I like helping people at Example Labs."}) == []


def test_validators_catch_each_problem() -> None:
    assert "exactly these keys" in run_validators({"why": "ok", "extra": "x"})[0]
    assert "is empty" in run_validators({"why": "  "})[0]
    assert "limit is 40" in run_validators({"why": "x" * 41})[0]
    assert "no placeholders" in run_validators({"why": "Hi, [Your Name] here"})[0]
    assert "no placeholders" in run_validators({"why": "As an AI I cannot apply."})[0]
    problems = run_validators({"why": "I worked at Acme Corp."})
    assert "'Acme Corp' is not in the profile" in problems[0]


def test_invented_facts_whole_words_and_sentence_starts() -> None:
    assert invented_facts("Three years ago I joined Example Labs.", FACTS) == []
    assert invented_facts("I have 9 years of experience.", FACTS) == ["9"]
    found = invented_facts("Write to me at https://evil.example/x or a@b.co.", FACTS)
    assert set(found) == {"a@b.co", "https://evil.example/x"}


def test_invented_name_at_sentence_start_is_caught() -> None:
    assert invented_facts("Initech was my first employer.", FACTS) == ["Initech"]
    assert invented_facts("Helping people is what I enjoy.", FACTS) == []
    assert invented_facts("Patience matters. I value patience.", FACTS) == []
    assert invented_facts("Reach me any time.", FACTS) == []


def test_fake_drafter_uses_only_profile_facts_and_respects_length() -> None:
    inputs = {
        "questions": [{"key": "a", "max_length": 60}, {"key": "b", "max_length": 1000}],
        "profile": {"work": PROFILE.work, "skills": PROFILE.skills},
    }
    answers = json.loads(fake_draft(inputs))["answers"]
    assert len(answers["a"]) <= 60 and answers["b"]
    assert invented_facts(answers["b"], FACTS) == []


def step(kind: str, value) -> FillStep:
    return FillStep(
        step_index=0,
        key="k",
        label="k",
        kind=kind,
        value=value,
        source="x",
        locator=Locator(strategy="css", css="#k"),
    )


def test_matches_by_kind() -> None:
    assert matches(step("number", "3"), "3.0")
    assert matches(step("checkbox_group", ["a", "b"]), ["b", "a"])
    assert matches(step("file", ["profiles/files/resume.pdf"]), ["resume.pdf"])
    assert matches(step("checkbox", True), True) and not matches(step("checkbox", True), False)
    assert matches(step("text", "hi "), "hi")


def packet(rows: list[ReviewRow], warnings: list[str] | None = None) -> ReviewPacket:
    return ReviewPacket(
        run_id="form-1",
        kind="form",
        title="t",
        plan_hash="h" * 64,
        details={
            "url": "http://127.0.0.1/x",
            "rows": [r.model_dump() for r in rows],
            "counts": {
                "filled": len(rows),
                "drafted": 0,
                "needs_human": 0,
                "flagged": sum(bool(r.flags) for r in rows),
            },
            "warnings": warnings or [],
            "screenshots": ["artifacts/step-1.png"],
            "final_button": "Send <now>",
            "target": "site:practice/contact",
        },
    )


def test_review_html_escapes_and_shows_flags() -> None:
    rows = [
        ReviewRow(
            step=0,
            key="msg",
            label="<script>x</script>",
            value="a & b",
            source="draft",
            flags=["agreement"],
        )
    ]
    html = render_html(packet(rows))
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "a &amp; b" in html and "agreement" in html and "Send &lt;now&gt;" in html
    assert 'src="artifacts/step-1.png"' in html


def test_is_clean_and_ping_has_no_values() -> None:
    clean = packet(
        [ReviewRow(step=0, key="email", label="Email", value="riya.testwell@example.com")]
    )
    assert is_clean(clean)
    assert "riya" not in ping_text(clean).lower()
    url_target = clean.model_copy(deep=True)
    url_target.details["target"] = "https://forms.example.test/apply?email=riya@example.com"
    assert "riya" not in ping_text(url_target) and "forms.example.test" in ping_text(url_target)
    assert not is_clean(packet([], warnings=["page changed"]))
    assert not is_clean(
        packet([ReviewRow(step=0, key="c", label="c", value=True, flags=["agreement"])])
    )
