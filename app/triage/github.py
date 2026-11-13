"""The GitHub issues connector: an async REST client, plus `FakeGitHub` for tests.

- `list_issues` follows `Link` headers 100 at a time and drops pull requests (the issues API
  returns both; PRs have a `pull_request` key).
- Rate limits: when `X-RateLimit-Remaining` hits 0, or GitHub answers a secondary limit (403 or
  429 with `Retry-After`), it waits if the wait is short (under a minute), else stops cleanly with
  `RateLimited`.
- `read_only=True` makes every write raise `ReadOnly`, so the bot can triage any public repo
  without changing anything.

The token is a fine-grained PAT with Issues read/write and Metadata read on the target repo only.
It goes in the Authorization header and nowhere else.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx2
import structlog

from app.runtime.logging import known_secrets, redact

log = structlog.get_logger(__name__)

API = "https://api.github.com"
MAX_WAIT_SECONDS = 60
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(Exception):
    pass


class RateLimited(GitHubError):
    """GitHub asked us to wait longer than we are willing to (the run stops cleanly)."""


class ReadOnly(GitHubError):
    """A write was attempted in read-only mode."""


class IssuesAPI(Protocol):
    read_only: bool

    async def list_issues(
        self, repo: str, since: datetime | None = None, state: str = "all"
    ) -> list[dict[str, Any]]: ...
    async def get_repo(self, repo: str) -> dict[str, Any]: ...
    async def get_issue(self, repo: str, number: int) -> dict[str, Any]: ...
    async def list_comments(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...
    async def create_comment(self, repo: str, number: int, body: str) -> dict[str, Any]: ...
    async def close_issue(self, repo: str, number: int, reason: str = "not_planned") -> None: ...
    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]: ...
    async def find_issues_with_label(self, repo: str, label: str) -> list[dict[str, Any]]: ...
    async def ensure_labels(self, repo: str, labels: dict[str, tuple[str, str]]) -> list[str]: ...


class GitHubClient:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        token: str | None = None,
        *,
        read_only: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.read_only = read_only
        self.sleep = sleep
        self.clock = clock
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "askfirst-triage",
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        if method != "GET" and self.read_only:
            raise ReadOnly(f"{method} {url.replace(API, '')} refused: read-only mode")
        full = url if url.startswith("http") else f"{API}{url}"
        for _attempt in range(3):
            try:
                response = await self.client.request(method, full, headers=self.headers, **kwargs)
            except httpx2.HTTPError as exc:
                raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
            wait = self._wait_for(response)
            if wait is None:
                break
            if wait > MAX_WAIT_SECONDS:
                raise RateLimited(f"GitHub rate limit: try again in {int(wait)}s")
            log.warning("github_rate_limited", wait_seconds=round(wait, 1))
            await self.sleep(wait)
        else:
            raise RateLimited("GitHub kept rate limiting")
        if response.is_error:
            # The error ends up in logs, the audit log and LangGraph's checkpoint (a failed
            # node's exception is saved there), so mask anything secret the answer quotes.
            message = redact(response.text[:300], extra_secrets=known_secrets())
            raise GitHubError(
                f"GitHub answered {response.status_code} for {method} "
                f"{full.replace(API, '')}: {message}"
            )
        return response

    def _wait_for(self, response: httpx2.Response) -> float | None:
        """Seconds to wait before retrying, or None when the response is not a rate limit."""
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                return 60.0
        if response.headers.get("X-RateLimit-Remaining") == "0":
            reset = float(response.headers.get("X-RateLimit-Reset", self.clock() + 3600))
            return max(reset - self.clock(), 1.0)
        if response.status_code == 429:
            return 60.0
        return None  # a plain 403 (permissions) is an error, not a rate limit

    async def _paginate(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        first = True
        while next_url:
            response = await self._request("GET", next_url, params=params if first else None)
            first = False
            items.extend(response.json())
            match = LINK_NEXT_RE.search(response.headers.get("Link", ""))
            next_url = match.group(1) if match else None
        return items

    async def list_issues(
        self, repo: str, since: datetime | None = None, state: str = "all"
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "state": state,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            params["since"] = since.isoformat()
        raw = await self._paginate(f"/repos/{repo}/issues", params)
        return [i for i in raw if "pull_request" not in i]

    async def get_repo(self, repo: str) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{repo}")
        return response.json()

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        response = await self._request("GET", f"/repos/{repo}/issues/{number}")
        return response.json()

    async def list_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._paginate(f"/repos/{repo}/issues/{number}/comments", {"per_page": 100})

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        await self._request(
            "POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels}
        )

    async def create_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body}
        )
        return response.json()

    async def close_issue(self, repo: str, number: int, reason: str = "not_planned") -> None:
        await self._request(
            "PATCH",
            f"/repos/{repo}/issues/{number}",
            json={"state": "closed", "state_reason": reason},
        )

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/repos/{repo}/issues", json={"title": title, "body": body, "labels": labels}
        )
        return response.json()

    async def find_issues_with_label(self, repo: str, label: str) -> list[dict[str, Any]]:
        raw = await self._paginate(
            f"/repos/{repo}/issues", {"state": "all", "labels": label, "per_page": 100}
        )
        return [i for i in raw if "pull_request" not in i]

    async def ensure_labels(self, repo: str, labels: dict[str, tuple[str, str]]) -> list[str]:
        """Create any missing labels (name -> (colour, description)). Returns the created names."""
        existing = {
            lab["name"] for lab in await self._paginate(f"/repos/{repo}/labels", {"per_page": 100})
        }
        created = []
        for name, (color, description) in labels.items():
            if name not in existing:
                await self._request(
                    "POST",
                    f"/repos/{repo}/labels",
                    json={"name": name, "color": color, "description": description},
                )
                created.append(name)
        return created


class FakeGitHub:
    """In-memory issues, comments and labels, with the same methods as `GitHubClient`."""

    def __init__(
        self,
        issues: list[dict[str, Any]] | None = None,
        *,
        read_only: bool = False,
        private: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.issues: dict[int, dict[str, Any]] = {i["number"]: dict(i) for i in issues or []}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.labels: dict[str, tuple[str, str]] = {}
        self.read_only = read_only
        self.private = private
        self.clock = clock
        self.writes: list[tuple[str, Any]] = []

    def _touch(self, number: int) -> None:
        """Like GitHub: any write to an issue moves its `updated_at`."""
        if number in self.issues:
            self.issues[number]["updated_at"] = self.clock().isoformat()

    def _write(self, kind: str, detail: Any) -> None:
        if self.read_only:
            raise ReadOnly(f"{kind} refused: read-only mode")
        self.writes.append((kind, detail))

    def _next_number(self) -> int:
        return max(self.issues, default=0) + 1

    async def list_issues(
        self, repo: str, since: datetime | None = None, state: str = "all"
    ) -> list[dict[str, Any]]:
        out = []
        for raw in self.issues.values():
            if "pull_request" in raw:
                continue
            if state != "all" and raw.get("state", "open") != state:
                continue
            if (
                since is not None
                and datetime.fromisoformat(str(raw["updated_at"]).replace("Z", "+00:00")) < since
            ):
                continue
            out.append(dict(raw))
        return sorted(out, key=lambda r: r["number"])

    async def get_repo(self, repo: str) -> dict[str, Any]:
        return {"full_name": repo, "private": self.private}

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return dict(self.issues[number])

    async def list_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.comments.get(number, []))

    async def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._write("add_labels", (number, labels))
        self._touch(number)
        current = self.issues[number].setdefault("labels", [])
        for name in labels:
            if name not in [lab["name"] if isinstance(lab, dict) else lab for lab in current]:
                current.append({"name": name})

    async def create_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        self._write("comment", (number, body))
        self._touch(number)
        comment = {
            "id": sum(len(c) for c in self.comments.values()) + 1,
            "body": body,
            "created_at": datetime.now().astimezone().isoformat(),
            # The bot writes with its owner's token, as on the real repo.
            "author_association": "OWNER",
        }
        self.comments.setdefault(number, []).append(comment)
        return comment

    async def close_issue(self, repo: str, number: int, reason: str = "not_planned") -> None:
        self._write("close", (number, reason))
        self.issues[number]["state"] = "closed"
        self._touch(number)

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        self._write("create_issue", title)
        number = self._next_number()
        now = datetime.now().astimezone().isoformat()
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
            "user": {"login": "askfirst-bot"},
            "created_at": now,
            "updated_at": now,
            "comments": 0,
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "state": "open",
        }
        return self.issues[number]

    async def find_issues_with_label(self, repo: str, label: str) -> list[dict[str, Any]]:
        return [
            dict(i)
            for i in self.issues.values()
            if label
            in [lab["name"] if isinstance(lab, dict) else lab for lab in i.get("labels", [])]
        ]

    async def ensure_labels(self, repo: str, labels: dict[str, tuple[str, str]]) -> list[str]:
        created = [n for n in labels if n not in self.labels]
        if created:
            self._write("ensure_labels", created)
        self.labels.update(labels)
        return created
