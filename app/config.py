"""App settings, read from environment variables (and the repo-root `.env` if present).

One `Settings` object holds every config value so the rest of the code never reads
`os.environ` directly. Call `get_settings()` to get it. Every key here must also be listed in
`.env.example`. Later phases add the browser, practice site, Discord and triage keys.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> repo root. Inside Docker this path has no .env, which is fine: the
# container gets the values as real environment variables instead.
REPO_ROOT_ENV = Path(__file__).resolve().parents[1] / ".env"

LlmProvider = Literal["fake", "groq", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT_ENV,
        extra="ignore",
        # A key left empty in .env (`KEY=`) means "use the default".
        env_ignore_empty=True,
        # Error messages never echo the values (later keys include API keys and passwords).
        hide_input_in_errors=True,
    )

    # The one place the name lives in code: shown by the CLI and used in the User-Agent.
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
