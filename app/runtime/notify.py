"""Messages to people: the `Notifier` interface, `DiscordNotifier` and `FakeNotifier`.

Two channels: `DIGEST` (the triage digest) and `REVIEW` ("a form run is waiting for review").
Each channel is one Discord webhook. `make_notifier(settings, client)` gives a
`DiscordNotifier` when at least one webhook is set, else a `FakeNotifier` that only logs (tests,
CI, demos without Discord). A channel with no webhook is skipped with a log line.

Ported from leadgate's notifier, made async and with embeds. The webhook URL holds its secret
token, so it never goes into errors or logs.
"""

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

import httpx2
import structlog

from app.config import Settings
from app.runtime.logging import known_secrets, redact

log = structlog.get_logger(__name__)

# Discord's limit for a message's `content`.
MAX_CONTENT = 2000
MAX_EMBEDS = 10
# A 429 says how long to wait (`retry_after`, seconds); never wait longer than this.
MAX_WAIT_SECONDS = 60
TRIES = 3


class Channel(StrEnum):
    DIGEST = "digest"
    REVIEW = "review"


class NotifyError(Exception):
    """The message wasn't posted."""


class Notifier(Protocol):
    async def send(
        self, channel: Channel, text: str = "", *, embeds: list[dict[str, Any]] | None = None
    ) -> bool:
        """Post one message. Returns False when the channel isn't configured."""
        ...


class FakeNotifier:
    """Remembers every message in `sent` (and logs it) instead of posting it."""

    def __init__(self) -> None:
        self.sent: list[tuple[Channel, str, list[dict[str, Any]]]] = []

    async def send(
        self, channel: Channel, text: str = "", *, embeds: list[dict[str, Any]] | None = None
    ) -> bool:
        self.sent.append((channel, text, list(embeds or [])))
        log.info("fake_message", channel=channel.value, text=text[:200], embeds=len(embeds or []))
        return True


class DiscordNotifier:
    def __init__(
        self,
        client: httpx2.AsyncClient,
        webhook_urls: dict[Channel, str],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.webhook_urls = {k: v for k, v in webhook_urls.items() if v}
        self.sleep = sleep  # tests pass a fake, so they don't wait

    async def send(
        self, channel: Channel, text: str = "", *, embeds: list[dict[str, Any]] | None = None
    ) -> bool:
        url = self.webhook_urls.get(channel)
        if not url:
            log.info("notify_skipped", channel=channel.value, reason="no webhook for channel")
            return False
        embeds = list(embeds or [])
        if len(embeds) > MAX_EMBEDS:
            raise NotifyError(f"too many embeds ({len(embeds)} > {MAX_EMBEDS}); split first")
        body: dict[str, Any] = {
            "content": text[:MAX_CONTENT],
            # Never ping anyone, even if an issue title has "@everyone" in it.
            "allowed_mentions": {"parse": []},
        }
        if embeds:
            body["embeds"] = embeds
        for attempt in range(1, TRIES + 1):
            try:
                response = await self.client.post(url, json=body)
            except httpx2.HTTPError as exc:
                raise NotifyError(f"Discord request failed: {type(exc).__name__}") from exc
            if response.status_code == 429 and attempt < TRIES:
                wait = retry_after(response)
                log.warning("discord_rate_limited", channel=channel.value, wait_seconds=wait)
                await self.sleep(wait)
                continue
            if response.is_error:
                # Discord's answer can quote the webhook URL, which holds its token.
                answer = redact(response.text[:300], extra_secrets=(url, *known_secrets()))
                raise NotifyError(f"Discord answered {response.status_code}: {answer}")
            log.info("message_sent", channel=channel.value, embeds=len(embeds))
            return True
        raise NotifyError("Discord kept rate limiting")  # pragma: no cover - loop always returns


def retry_after(response: httpx2.Response) -> float:
    """Seconds Discord asks us to wait: the body's `retry_after`, else the header, else 1."""
    try:
        seconds = float(response.json()["retry_after"])
    except ValueError, KeyError, TypeError:
        try:
            seconds = float(response.headers.get("Retry-After", 1))
        except ValueError:
            seconds = 1.0
    return min(max(seconds, 0.0), MAX_WAIT_SECONDS)


def make_notifier(settings: Settings, client: httpx2.AsyncClient) -> Notifier:
    urls = {
        Channel.DIGEST: settings.discord_webhook_url,
        Channel.REVIEW: settings.discord_review_webhook_url,
    }
    real = {k: v.get_secret_value() for k, v in urls.items() if v is not None}
    if not real:
        log.info("notifier_fake", note="no Discord webhook set: messages are only logged")
        return FakeNotifier()
    log.info("notifier_discord", channels=sorted(c.value for c in real))
    return DiscordNotifier(client, real)
