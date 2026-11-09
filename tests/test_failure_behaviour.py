"""What happens when something outside our control fails. Each case either degrades to "a
person decides" or stops cleanly; none of them guesses. (GitHub rate limits and Discord 429s
are in tests/triage/test_github.py and tests/unit/test_notify.py; Playwright timeouts and a page
that changed are in the browser tests.)"""

import sqlite3
import threading
import time
from pathlib import Path

from steadfast import LLMResponse
from steadfast.exceptions import NonRetryableProviderError

from app.browser.draft import draft_answers
from app.browser.mapping import Profile
from app.browser.schema import Field
from app.config import Settings
from app.runtime.llm import FakeProvider, make_llm
from app.runtime.notify import FakeNotifier
from app.runtime.runs import RunIndex, RunStatus
from app.triage.runner import open_triage_runner
from tests.triage.helpers import NOW, sandbox_github


class DownProvider(FakeProvider):
    """An LLM provider that is down (bad key, outage): every call fails."""

    async def chat(self, messages, tools=None, tool_choice=None, **params) -> LLMResponse:
        self.calls.append(list(messages))
        raise NonRetryableProviderError("503 service unavailable", provider="fake")


async def test_llm_down_drafts_are_left_empty_and_flagged() -> None:
    llm = make_llm(Settings(_env_file=None), provider=DownProvider())
    fields = [Field(key="why", kind="textarea", label="Why this role?", name="why")]
    answers, failed, error = await draft_answers(
        llm, fields, Profile.load(Path("profiles/example.yaml")), title="t", headings=[], context=""
    )
    assert answers == {} and failed == ["why"] and "provider failed" in error


async def test_llm_down_triage_still_runs_and_rules_still_escalate(tmp_path: Path) -> None:
    llm = make_llm(Settings(_env_file=None), provider=DownProvider())
    async with open_triage_runner(
        Settings(_env_file=None, data_dir=tmp_path),
        github=sandbox_github(),
        llm=llm,
        notifier=FakeNotifier(),
        durable=False,
        now=lambda: NOW,
    ) as runner:
        _, out = await runner.start("o/sandbox", since="60d")
    assert out.status == "done", out.error
    by_number = {r["number"]: r for r in out.values["results"]}
    # The security rule needs no LLM: #7 and #21 are escalated anyway.
    assert by_number[7]["escalate"] and by_number[21]["escalate"]
    assert set(out.values["unclassified"]) and by_number[1]["unclassified"]
    assert out.values["digest"]["unclassified"]  # listed for a person in the digest


def test_sqlite_locked_by_another_writer_waits_instead_of_failing(tmp_path: Path) -> None:
    index = RunIndex.in_data_dir(tmp_path)
    other = sqlite3.connect(tmp_path / "app.sqlite", check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")  # another process holds the write lock

    def release() -> None:
        time.sleep(0.5)
        other.commit()
        other.close()

    threading.Thread(target=release).start()
    started = time.monotonic()
    index.create("form-20261108-0300-lock")
    assert time.monotonic() - started >= 0.4  # it waited for the lock
    assert index.get("form-20261108-0300-lock").status is RunStatus.RUNNING
