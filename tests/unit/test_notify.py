"""Discord notifier: webhook per channel, limits, 429 handling; fake notifier."""

import httpx2
import pytest

from app.config import Settings
from app.runtime.notify import (
    Channel,
    DiscordNotifier,
    FakeNotifier,
    NotifyError,
    make_notifier,
    retry_after,
)

HOOK = "https://discord.com/api/webhooks/1/secret-token"


def client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_posts_content_and_embeds_without_mentions() -> None:
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(204)

    async with client(handler) as c:
        notifier = DiscordNotifier(c, {Channel.DIGEST: HOOK})
        assert await notifier.send(Channel.DIGEST, "x" * 3000, embeds=[{"title": "t"}])
        assert not await notifier.send(Channel.REVIEW, "nobody listens")
    body = __import__("json").loads(seen[0].content)
    assert len(body["content"]) == 2000
    assert body["allowed_mentions"] == {"parse": []} and body["embeds"] == [{"title": "t"}]


async def test_honours_429_retry_after_then_succeeds() -> None:
    replies = [httpx2.Response(429, json={"retry_after": 2.5}), httpx2.Response(204)]
    waits = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    async with client(lambda _r: replies.pop(0)) as c:
        assert await DiscordNotifier(c, {Channel.DIGEST: HOOK}, sleep=fake_sleep).send(
            Channel.DIGEST, "hi"
        )
    assert waits == [2.5]


async def test_errors_never_include_the_webhook_url() -> None:
    async with client(lambda _r: httpx2.Response(400, text="bad")) as c:
        with pytest.raises(NotifyError) as info:
            await DiscordNotifier(c, {Channel.DIGEST: HOOK}).send(Channel.DIGEST, "hi")
    assert "secret-token" not in str(info.value)

    def boom(_r):
        raise httpx2.ConnectError("down")

    async with client(boom) as c:
        with pytest.raises(NotifyError):
            await DiscordNotifier(c, {Channel.DIGEST: HOOK}).send(Channel.DIGEST, "hi")
    async with client(lambda _r: httpx2.Response(204)) as c:
        with pytest.raises(NotifyError, match="too many embeds"):
            await DiscordNotifier(c, {Channel.DIGEST: HOOK}).send(Channel.DIGEST, embeds=[{}] * 11)


def test_retry_after_parsing() -> None:
    assert retry_after(httpx2.Response(429, json={"retry_after": 999})) == 60
    assert retry_after(httpx2.Response(429, headers={"Retry-After": "3"})) == 3
    assert retry_after(httpx2.Response(429, headers={"Retry-After": "soon"})) == 1


async def test_make_notifier_picks_fake_or_discord() -> None:
    async with httpx2.AsyncClient() as c:
        fake = make_notifier(Settings(_env_file=None), c)
        assert isinstance(fake, FakeNotifier)
        await fake.send(Channel.DIGEST, "logged")
        assert fake.sent[0][1] == "logged"
        real = make_notifier(Settings(_env_file=None, discord_webhook_url=HOOK), c)
        assert isinstance(real, DiscordNotifier)
