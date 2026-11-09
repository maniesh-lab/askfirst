"""Site profiles: per-site settings in `config/sites/<name>.yaml`.

A target is either a URL or `site:<site>/<form>` (e.g. `site:practice/job_apply`). The profile
gives the form's path, an optional login, the names of the "next" buttons, the exact final
submit button, how to recognise success, and field overrides (`field_key -> profile path`).
"""

from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml
from pydantic import BaseModel
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
    site = load_site(site_name, sites_dir)
    if form_name not in site.forms:
        raise KeyError(
            f"site {site_name!r} has no form {form_name!r} (has: {', '.join(site.forms)})"
        )
    form = site.forms[form_name]
    return Target(
        url=site.resolved_base(settings) + form.path, site=site, form_name=form_name, form=form
    )
