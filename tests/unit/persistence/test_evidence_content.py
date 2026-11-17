"""Evidence content is sanitized before it is hashed or stored (§7.3, §15.1)."""

import pytest
from pydantic import JsonValue

from incidentpilot.persistence.evidence import MAX_CONTENT_TEXT, sanitize_content
from incidentpilot.policies.redaction import TRUNCATED

CANARY = "ip-canary-0123456789abcdef"
REDACTED = "[REDACTED-canary]"


def test_every_string_is_sanitized_at_any_depth_keys_included() -> None:
    content: JsonValue = {"message": f"saw {CANARY}", CANARY: {"items": [CANARY, 3]}}

    assert sanitize_content(content) == {
        "message": f"saw {REDACTED}",
        REDACTED: {"items": [REDACTED, 3]},
    }


def test_numbers_booleans_and_null_pass_unchanged() -> None:
    content: JsonValue = {"count": 2, "ratio": 0.5, "ok": True, "missing": None}

    assert sanitize_content(content) == content


def test_long_text_is_cut_at_the_limit() -> None:
    cleaned = sanitize_content("x" * (MAX_CONTENT_TEXT + 1))

    assert isinstance(cleaned, str)
    assert len(cleaned) == MAX_CONTENT_TEXT
    assert cleaned.endswith(TRUNCATED)
    assert sanitize_content("x" * MAX_CONTENT_TEXT) == "x" * MAX_CONTENT_TEXT


def test_long_keys_are_cut_too() -> None:
    cleaned = sanitize_content({"k" * (MAX_CONTENT_TEXT + 1): 1})

    assert isinstance(cleaned, dict)
    assert [len(key) for key in cleaned] == [MAX_CONTENT_TEXT]


def test_keys_that_collide_once_sanitized_are_refused() -> None:
    with pytest.raises(ValueError, match=r"^two keys are the same once sanitized$"):
        sanitize_content({CANARY: 1, "ip-canary-fedcba9876543210": 2})
