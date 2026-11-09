"""The practice site: our own forms for testing the browser agent, never a real service.

Pages (all server-rendered, plain HTML and CSS, a little vanilla JS where a real site would
have it):
- `/jobs/apply`: a single-page job application with a honeypot and a hidden token
- `/login` then `/listings/new`: a marketplace listing in three on-page steps, then a review
  step with "Save draft" (reversible) and "Publish listing" (irreversible)
- `/contact`: no `<label>`s at all, and a `<button>` without a type (it submits: a trap)
- `/survey`: ARIA widgets (radiogroup, combobox, checkbox, contenteditable), Google-Forms style
- `/submissions` (HTML) and `/api/submissions` (JSON); reset and variant switches for tests
"""

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from practice_site import forms
from practice_site.config import PracticeSettings, get_practice_settings
from practice_site.store import SubmissionStore

HERE = Path(__file__).resolve().parent
COOKIE = "seller"
TOKEN_MAX_AGE = 24 * 3600
HONEYPOT = "website"


def create_app(settings: PracticeSettings | None = None) -> FastAPI:
    settings = settings or get_practice_settings()
    store = SubmissionStore(settings.data_dir / "submissions.sqlite")
    templates = Jinja2Templates(directory=HERE / "templates")
    app = FastAPI(title="Practice forms", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.settings = settings
    app.state.store = store
    app.state.variant = settings.variant
    secret = settings.secret.get_secret_value().encode()

    def sign(text: str) -> str:
        return hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()[:32]

    def new_token(form: str) -> str:
        stamp = str(int(time.time()))
        return f"{stamp}.{sign(form + stamp)}"

    def token_ok(form: str, token: str) -> bool:
        stamp, _, sig = (token or "").partition(".")
        if not stamp.isdigit() or time.time() - int(stamp) > TOKEN_MAX_AGE:
            return False
        return hmac.compare_digest(sig, sign(form + stamp))

    def seller(request: Request) -> str | None:
        email, _, sig = request.cookies.get(COOKIE, "").partition("|")
        return email if email and hmac.compare_digest(sig, sign("seller:" + email)) else None

    def render(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("errors", {})
        ctx.setdefault("values", {})
        ctx["forms"] = forms
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def variant(request: Request) -> str:
        return request.query_params.get("variant") or app.state.variant

    async def read_form(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
        """Plain values (multi-values as lists) and file info (name + size, never contents)."""
        form = await request.form()
        values: dict[str, Any] = {}
        files: dict[str, Any] = {}
        for key in form:
            items = form.getlist(key)
            uploads = [i for i in items if isinstance(i, UploadFile)]
            if uploads:
                infos = []
                for up in uploads:
                    if up.filename:
                        infos.append({"name": up.filename, "size": len(await up.read())})
                files[key] = infos
                continue
            texts = [str(i) for i in items]
            values[key] = texts if len(texts) > 1 or key in {"shipping", "tools"} else texts[0]
        return values, files

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return render(request, "index.html")

    # Job application ---------------------------------------------------------------------

    @app.get("/jobs/apply", response_class=HTMLResponse)
    async def job_form(request: Request) -> HTMLResponse:
        return render(request, "job_apply.html", token=new_token("job"), variant=variant(request))

    @app.post("/jobs/apply", response_class=HTMLResponse, response_model=None)
    async def job_submit(request: Request) -> HTMLResponse | RedirectResponse:
        values, files = await read_form(request)
        current = variant(request)
        phone_name = "mobile" if current == "b" else "phone"
        errors = forms.check_job(values, phone_name)
        if not token_ok("job", values.get("csrf_token", "")):
            errors["csrf_token"] = "The form expired. Please reload the page."
        resume = files.get("resume") or []
        if resume and not resume[0]["name"].lower().endswith(".pdf"):
            errors["resume"] = "Please upload a PDF."
        if errors:
            return render(
                request,
                "job_apply.html",
                422,
                token=new_token("job"),
                variant=current,
                errors=errors,
                values=values,
            )
        data = {k: v for k, v in values.items() if k not in {"csrf_token", HONEYPOT}}
        data["resume"] = resume
        confirmation = store.add("job_apply", data, flagged_bot=bool(values.get(HONEYPOT)))
        return RedirectResponse(f"/jobs/apply/success?id={confirmation}", status_code=303)

    @app.get("/jobs/apply/success", response_class=HTMLResponse)
    async def job_success(request: Request, id: str = "") -> HTMLResponse:
        return render(request, "success.html", title="Application received", confirmation=id)

    # Marketplace ---------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/listings/new") -> HTMLResponse:
        return render(request, "login.html", next=next)

    @app.post("/login", response_class=HTMLResponse, response_model=None)
    async def login(request: Request) -> HTMLResponse | RedirectResponse:
        values, _ = await read_form(request)
        good_email = hmac.compare_digest(values.get("email", ""), settings.seller_email)
        good_pass = hmac.compare_digest(
            values.get("password", ""), settings.seller_password.get_secret_value()
        )
        target = values.get("next") or "/listings/new"
        if not target.startswith("/") or target.startswith("//"):
            target = "/listings/new"
        if not (good_email and good_pass):
            return render(
                request,
                "login.html",
                401,
                next=target,
                errors={"login": "Wrong email or password."},
                values={"email": values.get("email", "")},
            )
        response = RedirectResponse(target, status_code=303)
        email = settings.seller_email
        response.set_cookie(
            COOKIE, f"{email}|{sign('seller:' + email)}", httponly=True, samesite="lax"
        )
        return response

    @app.get("/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/listings/new", response_class=HTMLResponse, response_model=None)
    async def listing_form(request: Request) -> HTMLResponse | RedirectResponse:
        if not seller(request):
            return RedirectResponse("/login?next=/listings/new", status_code=303)
        return render(request, "listing_new.html", token=new_token("listing"))

    @app.post("/listings/new", response_class=HTMLResponse, response_model=None)
    async def listing_submit(request: Request) -> HTMLResponse | RedirectResponse:
        if not seller(request):
            return RedirectResponse("/login?next=/listings/new", status_code=303)
        values, files = await read_form(request)
        action = values.pop("action", "")
        errors = forms.check_listing(values) if action == "publish" else {}
        if not token_ok("listing", values.get("csrf_token", "")):
            errors["csrf_token"] = "The form expired. Please reload the page."
        if action not in {"publish", "draft"}:
            errors["action"] = "Please choose Save draft or Publish listing."
        if errors:
            return render(
                request,
                "listing_new.html",
                422,
                token=new_token("listing"),
                errors=errors,
                values=values,
                start_step=4,
            )
        data = {k: v for k, v in values.items() if k != "csrf_token"}
        data["photos"] = files.get("photos") or []
        kind = "listing" if action == "publish" else "draft"
        confirmation = store.add(kind, data)
        return RedirectResponse(f"/listings/done?id={confirmation}&t={kind}", status_code=303)

    @app.get("/listings/done", response_class=HTMLResponse)
    async def listing_done(request: Request, id: str = "", t: str = "listing") -> HTMLResponse:
        title = "Listing published" if t == "listing" else "Draft saved"
        return render(request, "success.html", title=title, confirmation=id)

    # Contact and survey ------------------------------------------------------------------

    @app.get("/contact", response_class=HTMLResponse)
    async def contact_form(request: Request) -> HTMLResponse:
        return render(request, "contact.html")

    @app.post("/contact", response_class=HTMLResponse, response_model=None)
    async def contact_submit(request: Request) -> HTMLResponse | RedirectResponse:
        values, _ = await read_form(request)
        errors = forms.check_contact(values)
        if errors:
            return render(request, "contact.html", 422, errors=errors, values=values)
        confirmation = store.add("contact", values)
        return RedirectResponse(f"/contact/thanks?id={confirmation}", status_code=303)

    @app.get("/contact/thanks", response_class=HTMLResponse)
    async def contact_thanks(request: Request, id: str = "") -> HTMLResponse:
        return render(request, "success.html", title="Thanks for your message", confirmation=id)

    @app.get("/survey", response_class=HTMLResponse)
    async def survey_form(request: Request) -> HTMLResponse:
        return render(request, "survey.html")

    @app.post("/survey", response_class=HTMLResponse, response_model=None)
    async def survey_submit(request: Request) -> HTMLResponse | RedirectResponse:
        values, _ = await read_form(request)
        errors = forms.check_survey(values)
        if errors:
            return render(request, "survey.html", 422, errors=errors, values=values)
        confirmation = store.add("survey", values)
        return RedirectResponse(f"/survey/thanks?id={confirmation}", status_code=303)

    @app.get("/survey/thanks", response_class=HTMLResponse)
    async def survey_thanks(request: Request, id: str = "") -> HTMLResponse:
        return render(request, "success.html", title="Thanks for your answers", confirmation=id)

    # Submissions, reset and variant -----------------------------------------------------

    @app.get("/submissions", response_class=HTMLResponse)
    async def submissions_page(request: Request) -> HTMLResponse:
        return render(request, "submissions.html", rows=store.all())

    @app.get("/api/submissions")
    async def submissions_api() -> list[dict[str, Any]]:
        return store.all()

    def require_reset_allowed() -> None:
        if not settings.allow_reset:
            raise HTTPException(403, "reset is turned off (set PRACTICE_ALLOW_RESET=1)")

    @app.delete("/api/submissions")
    async def submissions_reset() -> dict[str, int]:
        require_reset_allowed()
        return {"deleted": store.clear()}

    @app.post("/api/variant")
    async def set_variant(request: Request) -> JSONResponse:
        require_reset_allowed()
        body = await request.json()
        if body.get("variant") not in {"a", "b"}:
            raise HTTPException(422, "variant must be 'a' or 'b'")
        app.state.variant = body["variant"]
        return JSONResponse({"variant": app.state.variant})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
