"""Checks start_investigation makes before touching the database (erratum E8)."""

import json
import re
from typing import cast
from uuid import UUID

import pytest

from incidentpilot.application.errors import Rejected, RequestError
from incidentpilot.application.investigations import admitted_mode, fingerprint, start_investigation
from incidentpilot.domain.enums import RunMode
from incidentpilot.domain.ids import IncidentId, OwnerId
from incidentpilot.domain.investigation import StartInvestigation
from incidentpilot.persistence.database import Connection

OWNER = OwnerId(UUID("00000000-0000-4000-8000-000000000001"))
INCIDENT = IncidentId(UUID("00000000-0000-4000-8000-000000000003"))
NO_DATABASE = cast(Connection, None)


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


def request(mode: str = "offline_demo") -> StartInvestigation:
    fields = {
        "mode": mode,
        "snapshot_id": "snap-example-01",
        "time_window": {"start_at": "2026-11-04T18:00:00Z", "end_at": "2026-11-04T18:30:00Z"},
        "model_profile": "single-investigator-default",
    }
    return StartInvestigation.model_validate_json(json.dumps(fields))


@pytest.mark.parametrize("mode", [RunMode.OFFLINE_DEMO, RunMode.LIVE])
def test_a_request_in_the_process_mode_is_admitted(mode: RunMode) -> None:
    assert admitted_mode(mode, mode) is mode


@pytest.mark.parametrize(
    ("requested", "process"),
    [(RunMode.LIVE, RunMode.OFFLINE_DEMO), (RunMode.OFFLINE_DEMO, RunMode.LIVE)],
)
def test_a_request_in_another_mode_is_rejected(requested: RunMode, process: RunMode) -> None:
    message = f"this process runs in {process} mode, not {requested}"
    with pytest.raises(Rejected, match=exactly(message)):
        admitted_mode(requested, process)


@pytest.mark.parametrize("process", list(RunMode))
def test_replay_never_starts_an_investigation(process: RunMode) -> None:
    message = "replay shows stored runs and never starts an investigation"
    with pytest.raises(Rejected, match=exactly(message)):
        admitted_mode(RunMode.REPLAY, process)


async def test_a_mode_mismatch_is_rejected_before_the_database() -> None:
    with pytest.raises(Rejected):
        await start_investigation(
            NO_DATABASE, OWNER, RunMode.OFFLINE_DEMO, INCIDENT, "k", request("live")
        )


async def test_a_malformed_key_is_refused_before_the_database() -> None:
    message = "an idempotency key is 1 to 128 printable ASCII characters"
    with pytest.raises(RequestError, match=exactly(message)):
        await start_investigation(NO_DATABASE, OWNER, RunMode.OFFLINE_DEMO, INCIDENT, "", request())


def test_the_fingerprint_is_pinned() -> None:
    assert fingerprint(OWNER, INCIDENT, request()) == (
        "0e51c7438a38e17ee1e5a8cc98c11103f695019b333f6222c4f76e841a761f44"
    )


def test_the_fingerprint_covers_the_incident_and_request() -> None:
    other = IncidentId(UUID("00000000-0000-4000-8000-000000000009"))

    assert fingerprint(OWNER, other, request()) != fingerprint(OWNER, INCIDENT, request())
    assert fingerprint(OWNER, INCIDENT, request("live")) != fingerprint(OWNER, INCIDENT, request())
