"""Structured logs (structlog) that always carry the run ID and never carry secrets.

Call `configure_logging(settings)` once at program start. Inside a run, wrap the work in
`bind_run(run_id, module=...)`: every log line in that block gets `run_id` (plus `module` and
`thread_id`), and Steadfast's own log lines get the same ID as their `correlation_id`.

`redact()` is shared with the audit log, so both mask the same things: values under keys that
look secret (token, password, api_key, authorization, ...), and values that look like secrets
wherever they appear (bearer tokens, API keys, Discord webhook URLs).
"""

import logging
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import structlog
from steadfast import correlation_context

MASK = "***"

# A key whose name contains one of these words holds a secret, whatever its value is.
# Whole words only: `passport`, `passed` and `input_tokens` are not secrets.
_SECRET_KEY_RE = re.compile(
    r"(?<![a-z])(pass(word|wd|phrase)?|pwd|secret|token|api[_-]?key|authorization|"
    r"auth[_-]?header|cookie|credentials?|webhook[_-]?url|private[_-]?key)(?![a-z]|_count)",
    re.IGNORECASE,
)

# Values that look like secrets anywhere in a string.
_SECRET_VALUE_RES = [
    # Discord webhook URLs hold their token in the path.
    re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    # GitHub tokens (classic, fine-grained, app tokens).
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    # Groq, Anthropic and OpenAI style keys.
    re.compile(r"\b(?:gsk_|sk-ant-|sk-)[A-Za-z0-9_-]{16,}\b"),
]


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def redact_text(text: str) -> str:
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub(MASK, text)
    return text


def redact(value: Any, *, extra_secrets: tuple[str, ...] = ()) -> Any:
    """A copy of `value` with secrets masked. Walks dicts, lists and tuples.

    `extra_secrets` are exact strings to mask too (for example a password read from the
    environment, which has no telltale shape).
    """
    if isinstance(value, Mapping):
        return {
            k: (
                MASK
                if isinstance(k, str) and is_secret_key(k) and v
                else redact(v, extra_secrets=extra_secrets)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact(v, extra_secrets=extra_secrets) for v in value)
    if isinstance(value, str):
        text = redact_text(value)
        for secret in extra_secrets:
            if secret:
                text = text.replace(secret, MASK)
        return text
    return value


# Exact secret strings registered at runtime (e.g. the practice-site password), masked
# from every log line. Kept small and process-wide on purpose.
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this exact value in every log line and audit event from now on."""
    if value and len(value) >= 4:
        _known_secrets.add(value)


def register_secrets(values: list[str]) -> None:
    """Register several secrets at once, e.g. `register_secrets(settings.secret_values())`."""
    for value in values:
        register_secret(value)


def known_secrets() -> tuple[str, ...]:
    return tuple(_known_secrets)


def _redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact(event_dict, extra_secrets=known_secrets())


class _CurrentStderr:
    """Writes to whatever `sys.stderr` is *now* (pytest swaps it per test)."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Set up structlog for the whole process (safe to call more than once)."""
    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        # Logs go to stderr so CLI output on stdout stays clean for people and pipes.
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),
        cache_logger_on_first_use=False,
    )


@contextmanager
def bind_run(
    run_id: str, *, module: str | None = None, thread_id: str | None = None
) -> Iterator[None]:
    """Every log line inside this block carries `run_id` (and Steadfast's correlation ID)."""
    extra = {k: v for k, v in {"module": module, "thread_id": thread_id or run_id}.items() if v}
    with (
        structlog.contextvars.bound_contextvars(run_id=run_id, **extra),
        correlation_context(run_id),
    ):
        yield
