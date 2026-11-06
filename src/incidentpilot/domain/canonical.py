"""Canonical JSON and digests. Digests are stored, so output must never change.

Anything without one unambiguous encoding is rejected rather than coerced. Errors
name the type, never the value, because the value may be sensitive.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID


class CanonicalizationError(ValueError):
    """The value has no canonical encoding."""


def canonical_json(value: object) -> bytes:
    text = json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise CanonicalizationError("text is not valid UTF-8") from None


def sha256_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _normalize(value: object) -> object:
    match value:
        case Enum():
            return _normalize(value.value)
        case None | bool() | int() | str():
            return value
        case float():
            if not math.isfinite(value):
                raise CanonicalizationError("floats must be finite")
            return value + 0.0  # Turns -0.0 into 0.0.
        case datetime():
            if value.utcoffset() is None:
                raise CanonicalizationError("datetimes must have a timezone")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        case UUID():
            return str(value)
        case Mapping():
            return _normalize_mapping(value)
        case list() | tuple():
            return [_normalize(item) for item in value]
        case _:
            raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def _normalize_mapping(value: Mapping[object, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for raw_key, item in value.items():
        key = raw_key.value if isinstance(raw_key, Enum) else raw_key
        if not isinstance(key, str):
            raise CanonicalizationError("mapping keys must be strings")
        if key in normalized:
            raise CanonicalizationError("duplicate key after normalization")
        normalized[key] = _normalize(item)
    return normalized
