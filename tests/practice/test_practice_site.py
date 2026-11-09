"""Each practice form accepts a valid submission, re-renders on errors, flags honeypots;
reset and the variant switch only work when allowed."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from practice_site.app import create_app
from practice_site.config import PracticeSettings

PDF = Path("profiles/files/resume.pdf")


def make_client(tmp_path: Path, **kw) -> TestClient:
    settings = PracticeSettings(_env_file=None, data_dir=tmp_path, **kw)
    return TestClient(create_app(settings), follow_redirects=True)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return make_client(tmp_path, allow_reset=True)


def token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def job_data(client: TestClient, **over) -> dict:
    data = {
        "csrf_token": token(client.get("/jobs/apply").text),
        "full_name": "Riya Testwell",
        "email": "riya.testwell@example.com",
        "phone": "+977 980-000-0123",
        "location": "Lalitpur",
        "linkedin": "https://example.com/in/riya-testwell",
        "years_experience": "3",
        "work_authorization": "yes",
        "start_date": "2026-12-01",
        "why_role": "I enjoy helping people.",
        "cover_letter": "",
        "consent": "yes",
        "website": "",
    }
    data.update(over)
    return data


def test_every_page_has_banner_and_noindex(client: TestClient) -> None:
    for path in ["/", "/jobs/apply", "/login", "/contact", "/survey", "/submissions"]:
        html = client.get(path).text
        assert "Not a real job board or marketplace" in html, path
        assert 'name="robots" content="noindex' in html, path


def test_job_form_accepts_valid_submission(client: TestClient) -> None:
    with PDF.open("rb") as fh:
        page = client.post(
            "/jobs/apply",
            data=job_data(client),
            files={"resume": ("resume.pdf", fh, "application/pdf")},
        )
    assert page.status_code == 200 and "JOB-" in page.text
    [row] = client.get("/api/submissions").json()
    assert row["form"] == "job_apply" and not row["flagged_bot"]
    assert row["data"]["resume"][0]["name"] == "resume.pdf"
    assert "csrf_token" not in row["data"]


def test_job_form_errors_rerender(client: TestClient) -> None:
    page = client.post("/jobs/apply", data=job_data(client, phone="call me", email="", consent=""))
    assert page.status_code == 422
    assert "valid phone number" in page.text and "This field is required" in page.text
    assert 'value="Riya Testwell"' in page.text  # values kept
    assert client.get("/api/submissions").json() == []


def test_job_form_rejects_bad_token_and_non_pdf(client: TestClient) -> None:
    assert client.post("/jobs/apply", data=job_data(client, csrf_token="1.bad")).status_code == 422
    page = client.post(
        "/jobs/apply",
        data=job_data(client),
        files={"resume": ("cv.exe", b"x", "application/octet-stream")},
    )
    assert page.status_code == 422 and "upload a PDF" in page.text


def test_honeypot_flags_submission(client: TestClient) -> None:
    client.post("/jobs/apply", data=job_data(client, website="http://spam.example"))
    [row] = client.get("/api/submissions").json()
    assert row["flagged_bot"] and "website" not in row["data"]


def test_marketplace_needs_login_then_publishes(client: TestClient) -> None:
    assert "/login" in str(client.get("/listings/new").url)
    bad = client.post("/login", data={"email": "seller@example.test", "password": "nope"})
    assert bad.status_code == 401
    page = client.post(
        "/login",
        data={
            "email": "seller@example.test",
            "password": "practice-only-pass",
            "next": "/listings/new",
        },
    )
    assert "Create a listing" in page.text
    data = {
        "csrf_token": token(page.text),
        "title": "Practice desk lamp",
        "category": "furniture",
        "condition": "like_new",
        "price": "1800",
        "currency": "NPR",
        "quantity": "1",
        "shipping": ["pickup", "courier"],
        "description": "A lamp.",
    }
    done = client.post("/listings/new", data={**data, "action": "publish"})
    assert "Listing published" in done.text and "LST-" in done.text
    draft = client.post(
        "/listings/new", data={**data, "csrf_token": token(page.text), "action": "draft"}
    )
    assert "Draft saved" in draft.text
    forms_seen = [r["form"] for r in client.get("/api/submissions").json()]
    assert forms_seen == ["listing", "draft"]
    assert client.get("/api/submissions").json()[0]["data"]["shipping"] == ["pickup", "courier"]


def test_listing_validation_on_publish(client: TestClient) -> None:
    page = client.post(
        "/login", data={"email": "seller@example.test", "password": "practice-only-pass"}
    )
    bad = client.post(
        "/listings/new", data={"csrf_token": token(page.text), "action": "publish", "title": ""}
    )
    assert bad.status_code == 422 and 'data-start-step="4"' in bad.text


def test_login_next_cannot_redirect_off_site(client: TestClient) -> None:
    page = client.post(
        "/login",
        data={
            "email": "seller@example.test",
            "password": "practice-only-pass",
            "next": "//evil.example",
        },
    )
    assert str(page.url).endswith("/listings/new")


def test_contact_and_survey(client: TestClient) -> None:
    assert (
        client.post("/contact", data={"name": "", "email": "x", "message": ""}).status_code == 422
    )
    ok = client.post(
        "/contact",
        data={
            "name": "Riya",
            "email": "riya.testwell@example.com",
            "topic": "General question",
            "message": "Hello",
        },
    )
    assert "CON-" in ok.text
    assert client.post("/survey", data={"satisfaction": "", "role": "Pilot"}).status_code == 422
    ok = client.post(
        "/survey",
        data={
            "satisfaction": "4",
            "role": "Developer",
            "tools": ["Email"],
            "feedback": "Fewer clicks.",
        },
    )
    assert "SRV-" in ok.text
    assert client.get("/api/submissions").json()[-1]["data"]["tools"] == ["Email"]


def test_reset_only_when_allowed(tmp_path: Path) -> None:
    locked = make_client(tmp_path / "a")
    assert locked.delete("/api/submissions").status_code == 403
    assert locked.post("/api/variant", json={"variant": "b"}).status_code == 403
    open_ = make_client(tmp_path / "b", allow_reset=True)
    open_.post("/contact", data={"name": "R", "email": "r@example.com", "message": "m"})
    assert open_.delete("/api/submissions").json() == {"deleted": 1}


def test_variant_switch_renames_the_phone_field(client: TestClient) -> None:
    assert 'name="phone"' in client.get("/jobs/apply").text
    assert client.post("/api/variant", json={"variant": "b"}).json() == {"variant": "b"}
    html = client.get("/jobs/apply").text
    assert 'name="mobile"' in html and "Mobile number" in html and 'name="phone"' not in html
    assert client.post("/api/variant", json={"variant": "x"}).status_code == 422
    assert (
        'name="mobile"'
        in make_client(client.app.state.settings.data_dir / "q").get("/jobs/apply?variant=b").text
    )


def test_submissions_page_and_health(client: TestClient) -> None:
    assert "0 stored" in client.get("/submissions").text
    assert client.get("/healthz").json() == {"status": "ok"}
