"""Make the demo GIF (`docs/demo.gif`) and the digest preview (`docs/digest.png`), reproducibly.

Everything runs locally against our own practice site with the fake persona and the fake LLM:

1. A caption card, then **the agent's own browser** (video recorded by Playwright) filling the
   job form and stopping at the review gate.
2. The review page the agent wrote (`review.html`), scrolled through.
3. A caption card ("approved"), then the agent's fresh browser replaying the approved plan,
   clicking submit once, and the success page.
4. The practice site's `/submissions` page: exactly one submission.

The clips are joined, slowed down where the agent is too fast to follow, and turned into a GIF
with ffmpeg (palette pass, 720 px wide). The digest preview renders the sandbox digest's Discord
embeds as a local HTML card and screenshots it; it is not a screenshot of Discord itself.

Needs ffmpeg and Playwright's Chromium. Run: `python -m scripts.make_demo [--headed]`
"""

import argparse
import asyncio
import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

from app.browser.runner import open_form_runner
from app.config import Settings
from app.runtime.approval import Decision
from app.runtime.logging import configure_logging
from app.runtime.notify import FakeNotifier
from app.runtime.runs import RunPaths
from app.triage.digest import render_discord
from app.triage.models import Digest
from practice_site.config import PracticeSettings
from practice_site.server import running_site

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
VIEWPORT = {"width": 1280, "height": 900}
# The agent fills a form in under a second; slow its clips down so people can follow.
AGENT_SLOWDOWN = 3.0
# A new browser context records a blank page for its first moments; cut that off our own clips.
LEAD_IN = 0.8

CARD = """<html><body style="margin:0;height:100vh;display:flex;align-items:center;
justify-content:center;background:#0f172a;color:#f8fafc;font:600 44px system-ui,sans-serif;
text-align:center;padding:0 80px">
<div>{title}<div style="font:400 26px system-ui,sans-serif;color:#94a3b8;margin-top:18px">
{subtitle}</div></div></body></html>"""


async def record(browser, out: Path, action) -> Path:
    """Run `action(page)` in a new context with video on; return the saved video's path."""
    context = await browser.new_context(
        viewport=VIEWPORT, record_video_dir=str(out), record_video_size=VIEWPORT
    )
    page = await context.new_page()
    await page.wait_for_timeout(LEAD_IN * 1000)  # the blank lead-in, trimmed by to_gif
    await action(page)
    video = page.video
    await context.close()
    return Path(await video.path())


async def card(browser, out: Path, title: str, subtitle: str, seconds: float = 2.0) -> Path:
    async def show(page):
        await page.set_content(CARD.format(title=title, subtitle=subtitle))
        await page.wait_for_timeout(seconds * 1000)

    return await record(browser, out, show)


def newest_video(folder: Path, before: set[Path]) -> Path:
    videos = set(folder.glob("*.webm")) - before
    if len(videos) != 1:
        raise RuntimeError(f"expected one new agent video in {folder}, found {len(videos)}")
    return videos.pop()


async def make_clips(work: Path, headed: bool) -> list[tuple[Path, float, float]]:
    """(video, speed factor, seconds to cut from the start) in order. A factor above 1 slows the
    clip down."""
    agent_videos = work / "agent"
    clips: list[tuple[Path, float, float]] = []
    site = PracticeSettings(_env_file=None, data_dir=work / "practice", allow_reset=True)
    with running_site(site) as (base, _app):
        settings = Settings(
            _env_file=None,
            data_dir=work / "data",
            practice_base_url=base,
            allowed_hosts="127.0.0.1",
            llm_provider="fake",
        )
        async with async_playwright() as pw:
            viewer = await pw.chromium.launch(headless=not headed)
            clips.append(
                (
                    await card(
                        viewer,
                        work / "cards",
                        "askfirst fills a job form, then stops",
                        "Our own practice site · a made-up person · nothing is sent yet",
                    ),
                    1.0,
                    LEAD_IN,
                )
            )
            async with open_form_runner(
                settings,
                notifier=FakeNotifier(),
                headless=not headed,
                video_dir=str(agent_videos),
            ) as runner:
                seen: set[Path] = set()
                run_id, out = await runner.start("site:practice/job_apply", "profiles/example.yaml")
                if out.status != "waiting":
                    raise RuntimeError(f"expected the run to wait for review: {out.error}")
                fill_video = newest_video(agent_videos, seen)
                seen.add(fill_video)
                clips.append((fill_video, AGENT_SLOWDOWN, 0.0))

                review = RunPaths.for_run(settings.data_dir, run_id).review_html

                async def read_review(page):
                    await page.goto(review.as_uri())
                    await page.wait_for_timeout(1500)
                    for _ in range(6):
                        await page.mouse.wheel(0, 380)
                        await page.wait_for_timeout(700)
                    await page.wait_for_timeout(800)

                clips.append((await record(viewer, work / "review", read_review), 1.0, LEAD_IN))
                clips.append(
                    (
                        await card(
                            viewer,
                            work / "cards",
                            "A person approves",
                            f"python -m app.cli review approve {run_id}",
                        ),
                        1.0,
                        LEAD_IN,
                    )
                )
                decision = Decision(
                    choice="approve", reviewer="demo", plan_hash=out.packet.plan_hash
                )
                done = await runner.resume(run_id, decision)
                if done.values.get("result", {}).get("status") != "submitted":
                    raise RuntimeError(f"expected a submission: {done.error}")
                clips.append((newest_video(agent_videos, seen), AGENT_SLOWDOWN, 0.0))

            async def show_submissions(page):
                await page.goto(f"{base}/submissions")
                await page.wait_for_timeout(3000)

            clips.append((await record(viewer, work / "subs", show_submissions), 1.0, LEAD_IN))
            await viewer.close()
    return clips


def to_gif(
    clips: list[tuple[Path, float, float]], out: Path, width: int = 720, fps: int = 10
) -> None:
    """One ffmpeg pass: slow down / resize each clip, join them, build a palette from the whole
    video and map every frame onto it (the concat filter re-times each clip from zero)."""
    inputs: list[str] = []
    chains = []
    for n, (clip, slow, skip) in enumerate(clips):
        inputs += ["-i", str(clip)]
        chains.append(
            f"[{n}:v]trim=start={skip},setpts={slow}*(PTS-STARTPTS),fps={fps},scale={width}:-2:flags=lanczos,"
            f"setsar=1[v{n}]"
        )
    joined = "".join(f"[v{n}]" for n in range(len(clips)))
    graph = ";".join(
        [
            *chains,
            f"{joined}concat=n={len(clips)}:v=1:a=0[all]",
            "[all]split[a][b]",
            "[a]palettegen=stats_mode=diff[p]",
            "[b][p]paletteuse=dither=bayer:bayer_scale=4",
        ]
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, str(out)],
        check=True,
    )


def discord_markdown(text: str) -> str:
    """The bits of Markdown the digest uses (links, bold, code), as Discord shows them."""
    out = html.escape(text)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<a>\1</a>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out.replace("\n", "<br>")


def digest_card_html(digest: Digest) -> str:
    """The digest's Discord embeds as a plain dark card (a local preview, not Discord)."""
    blocks = []
    for message in render_discord(digest)[:1]:
        for embed in message[:6]:
            color = f"#{embed.get('color', 0x5865F2):06x}"
            body = discord_markdown(embed.get("description", ""))
            fields = "".join(
                f"<div class=f><b>{html.escape(f['name'])}</b><br>{html.escape(f['value'])}</div>"
                for f in embed.get("fields", [])
            )
            foot = html.escape(embed.get("footer", {}).get("text", ""))
            blocks.append(
                f"<div class=e style='border-left-color:{color}'>"
                f"<div class=t>{html.escape(embed['title'])}</div><div class=d>{body}</div>"
                f"<div class=fs>{fields}</div><div class=ft>{foot}</div></div>"
            )
    return f"""<html><head><style>
body{{margin:0;padding:24px;background:#1e1f22;color:#dbdee1;font:15px system-ui,sans-serif;
width:820px}} .e{{background:#2b2d31;border-left:4px solid;border-radius:4px;padding:12px 16px;
margin:0 0 10px}} .t{{font-weight:700;color:#f2f3f5;margin-bottom:6px}} .d{{line-height:1.45}}
.fs{{display:flex;gap:28px;margin-top:6px}} .ft{{color:#949ba4;font-size:12px;margin-top:8px}}
.note{{color:#949ba4;font-size:12px;margin-top:14px}} a{{color:#00a8fc}}
code{{background:#1e1f22;padding:1px 4px;border-radius:3px;font-size:13px}}
</style></head><body>{"".join(blocks)}
<div class=note>Local preview of the triage digest's Discord embeds (sandbox issues, fake
data). Rendered by scripts/make_demo.py.</div></body></html>"""


async def make_digest_png(work: Path, out: Path) -> None:
    from app.runtime.notify import FakeNotifier as Fake
    from app.triage.runner import open_triage_runner
    from tests.triage.helpers import NOW, sandbox_github

    async with open_triage_runner(
        Settings(_env_file=None, data_dir=work / "triage"),
        github=sandbox_github(),
        notifier=Fake(),
        now=lambda: NOW,
    ) as runner:
        _, result = await runner.start("o/sandbox", since="60d")
    digest = Digest.model_validate(result.values["digest"])
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 868, "height": 600})
        await page.set_content(digest_card_html(digest))
        await page.screenshot(path=str(out), full_page=True)
        await browser.close()


async def main_async(headed: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        clips = await make_clips(work, headed)
        to_gif(clips, DOCS / "demo.gif")
        await make_digest_png(work, DOCS / "digest.png")
    for name in ("demo.gif", "digest.png"):
        size = (DOCS / name).stat().st_size / 1_000_000
        print(f"docs/{name}: {size:.1f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Make docs/demo.gif and docs/digest.png")
    parser.add_argument("--headed", action="store_true", help="show the browser windows")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is needed for the GIF")
    configure_logging("ERROR", "console")
    asyncio.run(main_async(args.headed))


if __name__ == "__main__":
    main()
