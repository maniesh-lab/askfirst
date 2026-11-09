"""Every LLM call goes through here: a thin wrapper around Steadfast, plus a fake provider.

`make_llm(settings)` builds an `LLM`:
- `LLM.structured(prompt, schema, ...)` returns a validated Pydantic object. Steadfast runs the
  validate-and-retry loop: bad JSON, a wrong shape or a failed custom validator is fed back to
  the model, up to `max_attempts` tries.
- `LLM.text(prompt, ...)` returns plain text.
Both record token usage and attempts in the run's audit log. Steadfast's
`ValidationRetryExceeded` and `AllProvidersFailed` (and any provider error) come out as our own
`LLMFailed`, so callers only catch one thing.

`FakeProvider` is a real Steadfast provider that answers from a script instead of the network.
Tests use it so they run Steadfast's actual retry loop. With `LLM_PROVIDER=fake` (the default)
the app answers with deterministic task responders (see `register_fake_task`), so CI and demos
work offline.

Prompts are files: `app/prompts/<name>.<version>.txt`, loaded with `load_prompt()`.
"""

import json
import re
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

import structlog
from pydantic import BaseModel
from steadfast import (
    AllProvidersFailed,
    LLMResponse,
    ProviderError,
    SteadfastClient,
    Usage,
    ValidationRetryExceeded,
)
from steadfast.providers.base import BaseProvider

from app.config import Settings
from app.runtime.audit import AuditLog

log = structlog.get_logger(__name__)

# OpenAI's default is its cheapest current model (checked on its models page, 2026-11-09).
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-6-luna",
    "fake": "fake",
}
# Calls per minute per provider. Groq's free tier allows about 30 requests a minute.
RATE_LIMITS = {"groq": 25, "anthropic": 40, "openai": 60, "fake": 10_000}

# Prompts put the task name on the first line and the inputs as JSON between these tags, so
# the fake responders can read them back.
TASK_RE = re.compile(r"^TASK:\s*(\S+)", re.MULTILINE)
INPUT_RE = re.compile(r"<input>\s*(.*?)\s*</input>", re.DOTALL)


class LLMFailed(Exception):
    """The LLM gave no usable answer (kept failing validation, or every provider failed)."""

    def __init__(self, message: str, *, attempts: int = 0, errors: list[str] | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.errors = errors or []


def load_prompt(name: str, version: str = "v1") -> str:
    return resources.files("app.prompts").joinpath(f"{name}.{version}.txt").read_text("utf-8")


def render_prompt(template: str, task: str, inputs: dict[str, Any]) -> str:
    """Fill a prompt template: `{task}` and `{input}` (the inputs as pretty JSON in tags)."""
    block = "<input>\n" + json.dumps(inputs, indent=2, ensure_ascii=False) + "\n</input>"
    return template.replace("{task}", task).replace("{input}", block)


def read_task(messages: Sequence[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """The task name and inputs from the first user message (for fake responders)."""
    for message in messages:
        if message.get("role") == "user":
            text = str(message.get("content") or "")
            task = TASK_RE.search(text)
            data = INPUT_RE.search(text)
            inputs = json.loads(data.group(1)) if data else {}
            return (task.group(1) if task else None), inputs
    return None, {}


# task name -> function(inputs) -> reply text. Modules register their own at import time.
FAKE_TASKS: dict[str, Callable[[dict[str, Any]], str]] = {}


def register_fake_task(task: str) -> Callable[[Callable[[dict[str, Any]], str]], Callable]:
    def wrap(fn: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], str]:
        FAKE_TASKS[task] = fn
        return fn

    return wrap


class FakeProvider(BaseProvider):
    """A Steadfast provider with no network.

    - `replies`: answered in order, one per call (the last one repeats when they run out)
    - `by_task`: a list of replies per task name (read from the prompt's `TASK:` line)
    - otherwise: the registered fake task responder for the prompt's task
    `calls` keeps every message list it was sent, so tests can check what the model saw.
    """

    name = "fake"
    model = "fake"

    def __init__(
        self,
        replies: Sequence[str] | None = None,
        by_task: dict[str, Sequence[str]] | None = None,
    ) -> None:
        self.replies = list(replies or [])
        self.by_task = {k: list(v) for k, v in (by_task or {}).items()}
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, tool_choice=None, **params) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        task, inputs = read_task(messages)
        if self.replies:
            text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        elif task in self.by_task and self.by_task[task]:
            queue = self.by_task[task]
            text = queue.pop(0) if len(queue) > 1 else queue[0]
        elif task in FAKE_TASKS:
            text = FAKE_TASKS[task](inputs)
        else:
            text = "{}"
        words = sum(len(str(m.get("content") or "").split()) for m in messages)
        return LLMResponse(
            text=text,
            finish_reason="stop",
            usage=Usage(input_tokens=words, output_tokens=len(text.split())),
            model=self.model,
            provider=self.name,
        )


def _make_provider(name: str, settings: Settings, model: str | None) -> BaseProvider:
    if name == "fake":
        return FakeProvider()
    if name == "groq":
        from steadfast.providers.groq_provider import GroqProvider

        key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else None
        if not key:
            raise ValueError("LLM provider 'groq' needs GROQ_API_KEY")
        return GroqProvider(api_key=key, model=model or DEFAULT_MODELS["groq"])
    if name == "anthropic":
        try:
            from steadfast.providers.anthropic_provider import AnthropicProvider
        except ImportError as exc:  # pragma: no cover - needs the optional extra
            raise ValueError(
                'LLM provider "anthropic" needs: pip install -e ".[anthropic]"'
            ) from exc
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        if not key:
            raise ValueError("LLM provider 'anthropic' needs ANTHROPIC_API_KEY")
        return AnthropicProvider(api_key=key, model=model or DEFAULT_MODELS["anthropic"])
    if name == "openai":
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        if not key:
            raise ValueError("LLM provider 'openai' needs OPENAI_API_KEY")
        return _openai_provider(key, model or DEFAULT_MODELS["openai"])
    raise ValueError(f"unknown LLM provider {name!r}")


def openai_params(model: str, params: dict[str, Any]) -> dict[str, Any]:
    """OpenAI's reasoning models (everything newer than the gpt-4 family) may refuse a custom
    `temperature`, which our calls set. Leave it out for them; the validators keep the output
    in shape either way."""
    if str(model).startswith("gpt-4"):
        return params
    return {k: v for k, v in params.items() if k != "temperature"}


def _openai_provider(key: str, model: str) -> BaseProvider:
    try:
        from steadfast.providers.openai_provider import OpenAIProvider
    except ImportError as exc:  # pragma: no cover - needs the optional extra
        raise ValueError('LLM provider "openai" needs: pip install -e ".[openai]"') from exc

    class _OpenAI(OpenAIProvider):
        def _build_request(self, messages, tools, tool_choice, params):
            return super()._build_request(
                messages, tools, tool_choice, openai_params(self.model, params)
            )

    return _OpenAI(api_key=key, model=model)


class LLM:
    """One provider (plus an optional fallback) behind two simple calls."""

    def __init__(
        self,
        client: SteadfastClient,
        provider: str,
        *,
        fallbacks: Sequence[str] = (),
        audit: AuditLog | None = None,
    ) -> None:
        self.client = client
        self.provider = provider
        self.fallbacks = list(fallbacks)
        self.audit = audit

    @property
    def model(self) -> str | None:
        return self.client.providers[self.provider].model

    @property
    def usage(self) -> Usage:
        return self.client.total_usage

    @property
    def calls(self) -> int:
        """Provider calls made so far, retries included (evals use it to count attempts)."""
        return self._calls()

    async def structured[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        validators: Sequence[Callable[[T], str | None]] = (),
        max_attempts: int = 3,
        run_id: str | None = None,
        purpose: str = "structured",
        **params: Any,
    ) -> T:
        response = await self._chat(
            prompt, system, schema, list(validators), max_attempts, run_id, purpose, params
        )
        return response.parsed

    async def text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        run_id: str | None = None,
        purpose: str = "text",
        **params: Any,
    ) -> str:
        response = await self._chat(prompt, system, None, [], 1, run_id, purpose, params)
        return response.text

    async def _chat(
        self, prompt, system, schema, validators, max_attempts, run_id, purpose, params
    ):
        # Fall back to the run bound by bind_run(), so every call in a run is audited.
        run_id = run_id or structlog.contextvars.get_contextvars().get("run_id")
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt})
        before = Usage(**vars(self.client.total_usage))
        calls_before = self._calls()
        try:
            response = await self.client.chat(
                self.provider,
                messages,
                schema=schema,
                validators=validators or None,
                max_attempts=max_attempts,
                fallbacks=self.fallbacks or None,
                correlation_id=run_id,
                **params,
            )
        except ValidationRetryExceeded as exc:
            self._record(run_id, purpose, before, exc.attempts, "failed", exc.errors)
            raise LLMFailed(
                f"{purpose}: output still invalid after {exc.attempts} attempts",
                attempts=exc.attempts,
                errors=exc.errors,
            ) from exc
        except (AllProvidersFailed, ProviderError) as exc:
            self._record(
                run_id, purpose, before, self._calls() - calls_before, "failed", [str(exc)]
            )
            raise LLMFailed(f"{purpose}: provider failed ({type(exc).__name__})") from exc
        self._record(
            run_id, purpose, before, self._calls() - calls_before, "ok", [], response.provider
        )
        return response

    def _calls(self) -> int:
        return sum(getattr(p, "calls", 0) for p in self.client.providers.values())

    def _record(self, run_id, purpose, before, attempts, outcome, errors, provider=None):
        after = self.client.total_usage
        detail = {
            "purpose": purpose,
            "provider": provider or self.provider,
            "model": self.model,
            # Provider calls made, including validation retries and transport retries.
            "attempts": attempts,
            "input_tokens": after.input_tokens - before.input_tokens,
            "output_tokens": after.output_tokens - before.output_tokens,
            "errors": errors[:5],
        }
        log.info("llm_call", outcome=outcome, **detail)
        if self.audit and run_id:
            self.audit.record(run_id, "system", "llm_call", outcome=outcome, **detail)


class _Counted:
    """Wraps a provider to count calls, so the audit log can say how many attempts a call took
    (Steadfast doesn't report the attempt number on success)."""

    def __init__(self, inner: BaseProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = getattr(inner, "model", None)
        self.calls = 0

    async def chat(self, messages, tools=None, tool_choice=None, **params) -> LLMResponse:
        self.calls += 1
        return await self.inner.chat(messages, tools=tools, tool_choice=tool_choice, **params)


def make_llm(
    settings: Settings,
    *,
    audit: AuditLog | None = None,
    provider: BaseProvider | None = None,
) -> LLM:
    """Build the LLM from settings. Pass `provider` (e.g. a scripted FakeProvider) in tests."""
    client = SteadfastClient(max_retries=3, min_wait=1.0, max_wait=20.0)
    if provider is not None:
        name = client.register(_Counted(provider), rate=RATE_LIMITS.get(provider.name, 10_000))
        return LLM(client, name, audit=audit)
    main = settings.llm_provider
    client.register(
        _Counted(_make_provider(main, settings, settings.llm_model)), rate=RATE_LIMITS[main]
    )
    fallbacks = []
    fallback = settings.llm_fallback
    if fallback and fallback != main:
        client.register(
            _Counted(_make_provider(fallback, settings, None)), rate=RATE_LIMITS[fallback]
        )
        fallbacks.append(fallback)
    return LLM(client, main, fallbacks=fallbacks, audit=audit)
