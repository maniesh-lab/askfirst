"""App settings, read from environment variables (and the repo-root `.env` if present).

One `Settings` object holds every config value so the rest of the code never reads
`os.environ` directly. Call `get_settings()` to get it. Every key here must also be listed in
`.env.example`. Secrets are `SecretStr`, so printing settings never shows them.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> repo root. Inside Docker this path has no .env, which is fine: the
# container gets the values as real environment variables instead.
REPO_ROOT_ENV = Path(__file__).resolve().parents[1] / ".env"

LlmProvider = Literal["fake", "groq", "anthropic", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT_ENV,
        extra="ignore",
        # A key left empty in .env (`KEY=`) means "use the default".
        env_ignore_empty=True,
        # Error messages never echo the values (later keys include API keys and passwords).
        hide_input_in_errors=True,
    )

    # Shown by the CLI and used in the browser's User-Agent. (The package name `askfirst`,
    # the triage bot's own User-Agent and label texts are fixed strings.)
    app_name: str = "askfirst"
    # SQLite files and per-run folders go here. Relative paths are from where you run it.
    data_dir: Path = Path("data")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # json for machines (CI, Docker), console for reading in a terminal.
    log_format: Literal["json", "console"] = "json"

    # "fake" answers without any network (tests, CI, offline demos); the others call the
    # real APIs through Steadfast.
    llm_provider: LlmProvider = "fake"
    # Empty = the provider's default model (set in app/runtime/llm.py).
    llm_model: str | None = None
    # Tried when the main provider fails. Empty = no fallback.
    llm_fallback: LlmProvider | None = None
    groq_api_key: SecretStr | None = None
    # Optional and paid: only used when LLM_PROVIDER or LLM_FALLBACK is "anthropic".
    anthropic_api_key: SecretStr | None = None
    # Optional and paid: only used when LLM_PROVIDER or LLM_FALLBACK is "openai".
    openai_api_key: SecretStr | None = None

    # Discord channel webhooks. Empty = messages are only logged (FakeNotifier).
    discord_webhook_url: SecretStr | None = None
    # Optional "a review is waiting" pings; falls back to nothing (not the digest channel).
    discord_review_webhook_url: SecretStr | None = None

    # Hosts the browser may load (comma-separated). Everything else is blocked.
    allowed_hosts: str = "127.0.0.1,localhost"
    headless: bool = True
    # Debugging only (CI sets it): save a Playwright trace per browser session in this folder.
    # A trace holds everything typed into the page, so never set it for a real account.
    playwright_trace_dir: Path | None = None

    # The practice site (our own forms): where it runs and its fake seller login.
    practice_base_url: str = "http://127.0.0.1:8800"
    practice_seller_email: str = "seller@example.test"
    practice_seller_password: SecretStr = SecretStr("practice-only-pass")

    # Triage bot: a fine-grained PAT (Issues read/write + Metadata read on the target repo only),
    # the repo as owner/name, and what it may change on its own.
    triage_github_token: SecretStr | None = None
    triage_repo: str | None = None
    # propose = no changes to issues, only list proposals (the bot's own log issue still gets a
    # comment per run); apply-labels = also add allowlisted labels.
    triage_mode: Literal["propose", "apply-labels"] = "propose"

    def secret_values(self) -> list[str]:
        """Every secret that is set, so the logger and audit log can mask the exact strings."""
        return [
            value.get_secret_value()
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), SecretStr) and value.get_secret_value()
        ]

    @property
    def allowed_host_set(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
