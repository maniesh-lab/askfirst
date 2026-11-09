"""Stable hashes of plain data, used for plan hashes and schema hashes.

The same data always gives the same hash: keys are sorted and the JSON is compact. Values
under secret-looking keys are replaced by a placeholder first, so a hash never depends on (or
leaks) a password, but still changes if a secret field is added or removed.
"""

import hashlib
import json
from typing import Any

from app.runtime.logging import is_secret_key

SECRET_PLACEHOLDER = "<secret>"


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: SECRET_PLACEHOLDER if isinstance(k, str) and is_secret_key(k) else _strip_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_strip_secrets(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _strip_secrets(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def stable_hash(value: Any) -> str:
    """SHA-256 (hex) of the canonical JSON of `value`."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
