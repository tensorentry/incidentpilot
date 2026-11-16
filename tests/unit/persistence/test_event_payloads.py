"""Event payloads are sanitized and shaped at the one place every event passes (§15.1)."""

import re

import pytest

from incidentpilot.persistence.events import EventError, clean_payload


def test_every_string_is_sanitized_at_any_depth() -> None:
    payload = {"note": "pwd=hunter2", "nested": {"items": ["ip-canary-0123456789abcdef", 3]}}

    assert clean_payload(payload) == {
        "note": "pwd=[REDACTED-credential]",
        "nested": {"items": ["[REDACTED-canary]", 3]},
    }


def test_numbers_booleans_and_null_pass_unchanged() -> None:
    payload = {"count": 2, "ratio": 0.5, "ok": True, "missing": None, "pair": (1, 2)}

    assert clean_payload(payload) == {
        "count": 2,
        "ratio": 0.5,
        "ok": True,
        "missing": None,
        "pair": [1, 2],
    }


@pytest.mark.parametrize("key", ["Mixed", "has space", "1st", "", 7])
def test_keys_must_be_snake_case(key: object) -> None:
    with pytest.raises(EventError, match=rf"^{re.escape(f'event payload key {key!r} isn')}"):
        clean_payload({key: 1})


def test_other_types_are_refused() -> None:
    with pytest.raises(EventError, match=r"^event payloads can't hold bytes$"):
        clean_payload({"raw": b"x"})
