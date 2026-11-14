"""Deterministic mapping, button classification, keys and site profiles (no browser)."""

from pathlib import Path

import pytest

from app.browser.detect import classify_button, pick_main_form
from app.browser.mapping import Profile, format_value, is_free_text, map_field, match_option
from app.browser.schema import Field, FormStep, Option, assign_keys, slugify
from app.browser.sites import parse_target
from app.config import Settings

PROFILE = Profile.load("profiles/example.yaml")


def field(kind: str = "text", label: str = "", **kw) -> Field:
    return Field(key=kw.pop("key", slugify(label) or kind), kind=kind, label=label, **kw)


@pytest.mark.parametrize(
    ("text", "html_type", "kind"),
    [
        ("Submit application", "submit", "submit"),
        ("Send message", "submit", "submit"),
        ("Publish listing", "submit", "submit"),
        ("Place order", "submit", "submit"),
        ("Save draft", "submit", "save_draft"),
        ("Next", "button", "next"),
        ("Review listing", "button", "next"),
        ("Continue", "submit", "next"),
        ("Back", "button", "back"),
        ("Preview", "button", "other"),
        ("Clear", "reset", "other"),
        ("Anything", "submit", "submit"),
        ("Help", "button", "other"),
    ],
)
def test_classify_button(text: str, html_type: str, kind: str) -> None:
    assert classify_button(text, html_type) == kind


def test_button_override_wins() -> None:
    assert classify_button("Go", "submit", {"Go": "next"}) == "next"


@pytest.mark.parametrize(
    ("label", "kw", "status", "value", "source"),
    [
        ("First name", {}, "filled", "Riya", "synonym:identity.first_name"),
        ("Surname", {}, "filled", "Testwell", "synonym:identity.last_name"),
        ("Your name", {}, "filled", "Riya Testwell", "synonym:identity.full_name"),
        ("Contact", {"kind": "email"}, "filled", "riya.testwell@example.com", "type:email"),
        ("Reach me on", {"kind": "tel"}, "filled", "+977 980-000-0123", "type:tel"),
        (
            "x",
            {"autocomplete": "postal-code"},
            "filled",
            "44700",
            "autocomplete:address.postal_code",
        ),
        (
            "LinkedIn profile URL",
            {"kind": "url", "autocomplete": "url"},
            "filled",
            "https://example.com/in/riya-testwell",
            "synonym:links.linkedin",
        ),
        ("Years of experience", {"kind": "number"}, "filled", "3", "synonym:work.years_experience"),
        (
            "Earliest start date",
            {"kind": "date"},
            "filled",
            "2026-12-01",
            "synonym:work.earliest_start",
        ),
        ("How did you hear about us?", {}, "filled", "Through a friend.", "canned"),
        ("Why do you want this role?", {"kind": "textarea"}, "needs_draft", None, ""),
        ("What motivates you most at work?", {}, "needs_draft", None, ""),
        ("Nickname", {}, "needs_human", None, ""),
        # Questions that mention a synonym word are drafted, not filled from the profile.
        ("State why you want this job", {"kind": "textarea"}, "needs_draft", None, ""),
        (
            "Why do you want to work in this location?",
            {"kind": "textarea"},
            "needs_draft",
            None,
            "",
        ),
        (
            "Describe your experience with city planning",
            {"kind": "textarea"},
            "needs_draft",
            None,
            "",
        ),
        (
            "Mailing address",
            {"kind": "textarea"},
            "filled",
            "12 Example Marg",
            "synonym:address.street",
        ),
        # A bare "name" inside another label is not the person's name.
        ("Company name", {}, "needs_human", None, ""),
        ("Reference name", {}, "needs_human", None, ""),
        (
            "Name",
            {"name": "name", "id": "name"},
            "filled",
            "Riya Testwell",
            "synonym:identity.full_name",
        ),
        ("Password", {"kind": "password"}, "needs_human", None, ""),
    ],
)
def test_map_field_table(label: str, kw: dict, status: str, value, source: str) -> None:
    m = map_field(field(label=label, **{"kind": "text", **kw}), PROFILE)
    assert (m.status, m.value) == (status, value)
    if source:
        assert m.source == source


def test_override_to_a_missing_profile_value_is_not_filled() -> None:
    f = field(label="Favourite colour", key="colour")
    m = map_field(f, PROFILE, overrides={"colour": "identity.favourite_colour"})
    assert m.status == "needs_human" and m.value is None


def test_unfillable_fields_are_skipped_never_filled() -> None:
    for extra in [
        {"visible": False},
        {"disabled": True},
        {"readonly": True},
        {"visible": False, "honeypot": True},
    ]:
        m = map_field(field(label="Email", kind="email", **extra), PROFILE)
        assert m.status == "skipped" and m.value is None


def test_choices_must_match_an_option() -> None:
    country = field(
        "select",
        "Country",
        options=[
            Option(value="", text="Select"),
            Option(value="NP", text="Nepal"),
            Option(value="IN", text="India"),
        ],
    )
    assert map_field(country, PROFILE).value == "NP"
    city = field("select", "City", options=[Option(value="ktm", text="Kathmandu")])
    m = map_field(city, PROFILE)
    assert m.status == "needs_human" and "matches none" in m.reason
    auth = field(
        "radio",
        "Are you allowed to work in Nepal?",
        options=[Option(value="y", text="Yes, I can"), Option(value="n", text="No")],
    )
    assert map_field(auth, PROFILE).value == "y"


def test_match_option_rules() -> None:
    f = field(
        "select",
        "x",
        options=[Option(value="NP", text="Nepal"), Option(value="us", text="United States")],
    )
    assert match_option(f, "NP") == "NP"
    assert match_option(f, "nepal") == "NP"
    assert match_option(f, "USA") == "us"
    assert match_option(f, "") is None and match_option(f, "Mars") is None


def test_data_row_and_override_beat_heuristics() -> None:
    title = field("text", "Listing title", key="listing_title")
    assert map_field(title, PROFILE).status == "needs_human"
    m = map_field(title, PROFILE, data={"listing_title": "Lamp"})
    assert (m.value, m.source) == ("Lamp", "data:listing_title")
    loc = field("text", "Location", key="location")
    assert map_field(loc, PROFILE, overrides={"location": "address.region"}).value == "Bagmati"
    group = field(
        "checkbox_group",
        "Shipping",
        key="shipping",
        options=[Option(value="pickup", text="Pickup"), Option(value="courier", text="Courier")],
    )
    assert map_field(group, PROFILE, data={"shipping": "pickup;courier"}).value == [
        "pickup",
        "courier",
    ]


def test_agreement_boxes_are_flagged() -> None:
    m = map_field(
        field("checkbox", "I agree that Practice Co. may store this application"), PROFILE
    )
    assert m.value is True and "agreement" in m.flags


def test_files_only_from_profile_folder(tmp_path: Path) -> None:
    resume = field("file", "Resume (PDF)")
    assert map_field(resume, PROFILE).value == ["profiles/files/resume.pdf"]
    outside = tmp_path / "secret.pdf"
    outside.write_text("x")
    m = map_field(resume, PROFILE, data={"resume_pdf": str(outside)})
    assert m.status == "needs_human"
    m = map_field(resume, PROFILE, data={"resume_pdf": "profiles/files/../../pyproject.toml"})
    assert m.status == "needs_human"


def test_maxlength_too_short_for_value_needs_human() -> None:
    m = map_field(field("text", "Full name", constraints={"maxlength": "4"}), PROFILE)
    assert m.status == "needs_human"


def test_format_value_and_free_text() -> None:
    assert format_value(field("month", "m"), "2026-12-01") == "2026-12"
    assert format_value(field("checkbox", "c"), "yes") is True
    assert format_value(field("text", "t"), ["a", "b"]) == "a, b"
    assert format_value(field("text", "t"), None) is None
    assert is_free_text(field("contenteditable", "Notes"))
    assert not is_free_text(field("email", "Why?"))


def test_keys_are_stable_and_unique() -> None:
    fields = [
        field(label="E-mail address"),
        field(label="E-mail address"),
        field(label="", name="zip_code"),
    ]
    assign_keys(fields)
    assert [f.key for f in fields] == ["e_mail_address", "e_mail_address_2", "zip_code"]


def test_pick_main_form_skips_search() -> None:
    raw = {
        "forms": [{"index": 0, "is_search": True}, {"index": 1, "is_search": False}],
        "fields": [{"form_index": 0, "visible": True, "disabled": False}] * 3
        + [{"form_index": 1, "visible": True, "disabled": False}],
    }
    assert pick_main_form(raw) == 1
    assert pick_main_form({"forms": [], "fields": []}) == -1


def test_schema_hash_tracks_labels_and_options() -> None:
    a = FormStep(url="u", fields=[field(label="Phone", kind="tel")])
    b = FormStep(url="u", fields=[field(label="Mobile number", kind="tel")])
    hidden = FormStep(
        url="u", fields=[field(label="Phone", kind="tel"), field(label="Website", visible=False)]
    )
    assert a.compute_hash() != b.compute_hash()
    assert a.compute_hash() == hidden.compute_hash()


def test_parse_target(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, practice_base_url="http://practice-site:8800/")
    target = parse_target("site:practice/job_apply", settings)
    assert target.url == "http://practice-site:8800/jobs/apply"
    assert (
        target.form.submit_button == "Submit application"
        and target.label == "site:practice/job_apply"
    )
    assert parse_target("https://example.test/x", settings).site is None
    with pytest.raises(KeyError):
        parse_target("site:practice/nope", settings)
    with pytest.raises(FileNotFoundError):
        parse_target("site:nowhere/x", settings)


def test_dedupe_keys_only_renames_fillable_repeats() -> None:
    from app.browser.schema import dedupe_keys

    first = FormStep(
        url="u",
        index=0,
        fields=[field(label="Email", kind="email"), field(label="Price", visible=False)],
    )
    second = FormStep(
        url="u",
        index=1,
        fields=[
            field(label="Email", kind="email"),
            field(label="Price"),
            field(label="Name", visible=False),
        ],
    )
    dedupe_keys(second, [first])
    assert [f.key for f in second.fields] == ["email_s2", "price", "name"]


def test_batch_rows_keep_blank_cells_and_read_excel_csv(tmp_path) -> None:
    from app.browser.runner import load_batch_rows

    csv_file = tmp_path / "rows.csv"
    csv_file.write_bytes("﻿title,photos\nCookbook,\n".encode())
    assert load_batch_rows(str(csv_file), {}) == [{"title": "Cookbook", "photos": ""}]


def test_a_blank_data_cell_never_falls_back_to_the_persona() -> None:
    from app.browser.mapping import Profile, map_field
    from app.browser.schema import Field

    photos = Field(key="photos", name="photos", kind="file", label="Photos", visible=True)
    m = map_field(photos, Profile.load("profiles/example.yaml"), data={"photos": ""})
    assert m.status == "needs_human" and "leaves photos empty" in m.reason
