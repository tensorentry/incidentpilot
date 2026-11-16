"""The create-incident request (spec §7.2, §12.1)."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from incidentpilot.domain.incident import MAX_DESCRIPTION, CreateIncident

START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def request(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "description": "Checkout latency is up",
        "service": "checkout-api",
        "severity": "sev2",
        "window": {
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(minutes=30)).isoformat(),
        },
    }
    return fields | changes


def parse(fields: dict[str, object]) -> CreateIncident:
    return CreateIncident.model_validate_json(json.dumps(fields))


def exactly(message: str) -> str:
    return rf"Value error, {re.escape(message)} \["


def test_a_valid_request_parses() -> None:
    parsed = parse(request())

    assert (parsed.service, parsed.severity, parsed.window.start_at) == (
        "checkout-api",
        "sev2",
        START,
    )


def test_a_window_of_exactly_24_hours_is_accepted() -> None:
    end = (START + timedelta(hours=24)).isoformat()

    assert parse(request(window={"start_at": START.isoformat(), "end_at": end}))


def test_a_window_longer_than_24_hours_is_refused() -> None:
    end = (START + timedelta(hours=24, seconds=1)).isoformat()
    with pytest.raises(ValidationError, match=exactly("an incident window can't exceed 24 hours")):
        parse(request(window={"start_at": START.isoformat(), "end_at": end}))


@pytest.mark.parametrize(
    "changes",
    [
        {"description": ""},
        {"description": "x" * (MAX_DESCRIPTION + 1)},
        {"severity": "sev5"},
        {"service": "Checkout"},
        {"extra": "field"},
    ],
    ids=[
        "empty description",
        "long description",
        "unknown severity",
        "bad service",
        "unknown field",
    ],
)
def test_malformed_requests_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse(request(**changes))


def test_the_longest_description_is_accepted() -> None:
    assert len(parse(request(description="x" * MAX_DESCRIPTION)).description) == MAX_DESCRIPTION
