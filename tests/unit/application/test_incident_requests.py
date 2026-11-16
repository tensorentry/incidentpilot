"""Checks create_incident makes before touching the database."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from incidentpilot.application.incidents import IncidentError, create_incident, fingerprint
from incidentpilot.domain.ids import OwnerId
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.persistence.database import Connection

OWNER = OwnerId(UUID("00000000-0000-4000-8000-000000000001"))
OTHER = OwnerId(UUID("00000000-0000-4000-8000-000000000002"))
START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
# Nothing may reach the database, so any use of this fails the test.
NO_DATABASE = cast(Connection, None)


def incident(description: str = "Checkout latency is up") -> CreateIncident:
    window = {"start_at": START.isoformat(), "end_at": (START + timedelta(minutes=30)).isoformat()}
    fields = {
        "description": description,
        "service": "checkout-api",
        "severity": "sev2",
        "window": window,
    }
    return CreateIncident.model_validate_json(json.dumps(fields))


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


def test_the_same_request_has_the_same_fingerprint() -> None:
    assert fingerprint(OWNER, incident()) == fingerprint(OWNER, incident())


def test_the_fingerprint_is_pinned() -> None:
    # Stored fingerprints are compared with new requests, so the encoding can't drift.
    assert fingerprint(OWNER, incident()) == (
        "b38e24d8dfe9d983c31e45a8150fe3e6d7d2efb3defeaa5d8bf3722add5c572e"
    )


def test_a_different_request_or_owner_changes_the_fingerprint() -> None:
    assert fingerprint(OWNER, incident("Other text")) != fingerprint(OWNER, incident())
    assert fingerprint(OTHER, incident()) != fingerprint(OWNER, incident())


@pytest.mark.parametrize("key", ["", "x" * 129, "has space", "café", "tab\tkey"])
async def test_a_malformed_key_is_refused(key: str) -> None:
    message = "an idempotency key is 1 to 128 printable ASCII characters"
    with pytest.raises(IncidentError, match=exactly(message)):
        await create_incident(NO_DATABASE, OWNER, key, incident())


@pytest.mark.parametrize("description", ["​‮", "\x00\x01", "  \n\t "])
async def test_a_description_with_nothing_visible_is_refused(description: str) -> None:
    with pytest.raises(IncidentError, match=exactly("the description has no visible text")):
        await create_incident(NO_DATABASE, OWNER, "key-1", incident(description))
