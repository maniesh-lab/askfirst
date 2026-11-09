"""Practice site settings, read from environment variables (and the repo-root `.env`).

The practice site is a test target, not part of the agent, so it keeps its own small settings
class instead of importing the agent's.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT_ENV = Path(__file__).resolve().parents[1] / ".env"


class PracticeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT_ENV, env_prefix="PRACTICE_", extra="ignore", env_ignore_empty=True
    )

    # Fake seller account for the marketplace login. Not a real person or password.
    seller_email: str = "seller@example.test"
    seller_password: SecretStr = SecretStr("practice-only-pass")
    # Signs the login cookie and the form tokens. Change it on a public deploy.
    secret: SecretStr = SecretStr("practice-site-not-a-real-secret")
    # DELETE /api/submissions and POST /api/variant only work when this is on (tests, local).
    allow_reset: bool = False
    # "a" = normal pages; "b" renames one field on the job form (to test "page changed").
    variant: Literal["a", "b"] = "a"
    data_dir: Path = Path(__file__).resolve().parent / "data"
    host: str = "127.0.0.1"
    port: int = 8800


@lru_cache
def get_practice_settings() -> PracticeSettings:
    return PracticeSettings()
