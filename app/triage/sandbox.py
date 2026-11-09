"""The sandbox issues file (`tests/fixtures/sandbox_issues.yaml`) as GitHub-shaped dicts.

Used by the seed script (to create real issues) and by tests and evals (as FakeGitHub data, with
`age_days` turned into created/updated dates relative to `now`).
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

SANDBOX_FILE = Path("tests/fixtures/sandbox_issues.yaml")


def load_sandbox(path: Path = SANDBOX_FILE) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["issues"]


def as_github(
    issues: list[dict[str, Any]], now: datetime, repo: str = "maniesh-lab/askfirst-sandbox"
) -> list[dict[str, Any]]:
    out = []
    for item in issues:
        when = (now - timedelta(days=item.get("age_days", 0))).isoformat()
        out.append(
            {
                "number": item["number"],
                "title": item["title"],
                "body": item.get("body", ""),
                "labels": [{"name": n} for n in item.get("labels", [])],
                "user": {"login": "sandbox-user"},
                "created_at": when,
                "updated_at": when,
                "comments": 0,
                "html_url": f"https://github.com/{repo}/issues/{item['number']}",
                "state": "open",
            }
        )
    return out
