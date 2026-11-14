"""Site profiles: per-site settings in `config/sites/<name>.yaml`.

A target is either a URL or `site:<site>/<form>` (e.g. `site:practice/job_apply`). The profile
gives the form's path, an optional login, the names of the "next" buttons, the exact final
submit button, how to recognise success, and field overrides (`field_key -> profile path`).
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml
from pydantic import BaseModel, field_validator
from pydantic import Field as PField

from app.config import Settings

SITES_DIR = Path("config/sites")


class LoginSpec(BaseModel):
    url: str
    email_field: str = "Email"
    password_field: str = "Password"
    submit_button: str = "Log in"
    # Names of the Settings attributes that hold the credentials (never the values).
    email_setting: str
    password_setting: str

    @field_validator("email_setting", "password_setting")
    @classmethod
    def _login_settings_only(cls, name: str, info: Any) -> str:
        # Only settings made for a site login: a profile naming `triage_github_token` (or an API
        # key) must not get that secret typed into a web page.
        wanted = "_email" if info.field_name == "email_setting" else "_password"
        if not name.endswith(wanted):
            raise ValueError(f"{info.field_name} must name a setting ending in {wanted!r}")
        return name


class SuccessRule(BaseModel):
    url_contains: str | None = None
    text: str | None = None


class FormSpec(BaseModel):
    path: str
    login: LoginSpec | None = None
    steps: list[str] = PField(default_factory=list)
    submit_button: str | None = None
    success: SuccessRule = PField(default_factory=SuccessRule)
    overrides: dict[str, str] = PField(default_factory=dict)
    data_columns: dict[str, str] = PField(default_factory=dict)


class SiteProfile(BaseModel):
    name: str
    base_url: str
    base_url_setting: str | None = None
    # Hosts this site uses, for documentation. Each must ALSO be in ALLOWED_HOSTS: a site
    # profile can never widen what the browser may load.
    allowed_hosts: list[str] = PField(default_factory=list)
    forms: dict[str, FormSpec] = PField(default_factory=dict)

    def resolved_base(self, settings: Settings) -> str:
        if self.base_url_setting and getattr(settings, self.base_url_setting, None):
            return str(getattr(settings, self.base_url_setting)).rstrip("/")
        return self.base_url.rstrip("/")


class Target(BaseModel):
    """What to open: a URL, plus the site and form spec when it came from a site profile."""

    url: str
    site: SiteProfile | None = None
    form_name: str | None = None
    form: FormSpec | None = None

    @property
    def label(self) -> str:
        return f"site:{self.site.name}/{self.form_name}" if self.site else self.url

    def absolute(self, path: str) -> str:
        return urljoin(self.url, path)


class UnknownForm(KeyError):
    """The site profile exists but has no form with that name."""

    def __str__(self) -> str:  # KeyError would wrap the message in quotes
        return str(self.args[0])


def load_site(name: str, sites_dir: Path = SITES_DIR) -> SiteProfile:
    path = sites_dir / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no site profile {path}")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SiteProfile.model_validate(data)


def parse_target(text: str, settings: Settings, sites_dir: Path = SITES_DIR) -> Target:
    if not text.startswith("site:"):
        return Target(url=text)
    site_name, _, form_name = text.removeprefix("site:").partition("/")
    if not re.fullmatch(r"[a-z0-9_-]+", site_name):
        raise UnknownForm(f"site names are lower-case letters, digits, - and _ (got {site_name!r})")
    site = load_site(site_name, sites_dir)
    if form_name not in site.forms:
        raise UnknownForm(
            f"site {site_name!r} has no form {form_name!r} (has: {', '.join(site.forms)})"
        )
    form = site.forms[form_name]
    return Target(
        url=site.resolved_base(settings) + form.path, site=site, form_name=form_name, form=form
    )
