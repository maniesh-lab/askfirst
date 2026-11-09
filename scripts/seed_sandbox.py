"""Create the sandbox issues in a GitHub repo you own (default: maniesh-lab/askfirst-sandbox).

Idempotent: an issue whose title already exists is skipped. `--dry-run` only prints what it
would create. Use a fresh, empty repo: issue numbers must match the file (#1 to #30), because
the gold answers and duplicate links use them; it stops if one doesn't. Needs
TRIAGE_GITHUB_TOKEN (fine-grained, Issues read/write on that repo only). The API can't backdate
issues, so "stale" can only be tested offline with fixtures.

Run: `python -m scripts.seed_sandbox --repo maniesh-lab/askfirst-sandbox --dry-run`
"""

import argparse
import asyncio

import httpx2

from app.config import get_settings
from app.triage.github import GitHubClient, GitHubError
from app.triage.rubric import Rubric
from app.triage.sandbox import load_sandbox


async def seed(repo: str, dry_run: bool) -> int:
    settings = get_settings()
    token = (
        settings.triage_github_token.get_secret_value() if settings.triage_github_token else None
    )
    if not token and not dry_run:
        raise SystemExit("set TRIAGE_GITHUB_TOKEN first (or use --dry-run)")
    issues = load_sandbox()
    async with httpx2.AsyncClient(timeout=30) as http:
        gh = GitHubClient(http, token, read_only=dry_run)
        try:
            # Works without a token for a public repo, so a dry run shows what really exists.
            existing = {i["title"] for i in await gh.list_issues(repo)}
        except GitHubError as exc:
            if not dry_run:
                raise
            print(f"could not read existing issues ({exc}); listing every issue")
            existing = set()
        todo = [i for i in issues if i["title"] not in existing]
        print(
            f"{len(issues)} sandbox issues, {len(existing)} already in {repo}, "
            f"{len(todo)} to create"
        )
        if dry_run:
            for i in todo:
                print(f"  would create: {i['title']}")
            return 0
        rubric = Rubric.load()
        wanted = {lab for i in todo for lab in i.get("labels", [])}
        await gh.ensure_labels(
            repo, {n: (rubric.label_colors.get(n, "ededed"), "") for n in wanted}
        )
        for i in todo:
            created = await gh.create_issue(
                repo, i["title"], i.get("body", ""), i.get("labels", [])
            )
            print(f"  created #{created['number']}: {i['title']}")
            if created["number"] != i["number"]:
                # eval/triage/gold.yaml and the duplicate links use these numbers.
                print(f"stopped: expected #{i['number']}, got #{created['number']}")
                return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="maniesh-lab/askfirst-sandbox")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(seed(args.repo, args.dry_run)))


if __name__ == "__main__":
    main()
