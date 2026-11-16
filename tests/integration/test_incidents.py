"""Creating incidents idempotently (spec §12.1, invariant 11; erratum E19)."""

import asyncio
import json
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import errors

from incidentpilot.application.incidents import (
    OPERATION,
    CreatedIncident,
    IdempotencyConflict,
    create_incident,
    fingerprint,
)
from incidentpilot.domain.ids import IncidentId, OwnerId
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.persistence import idempotency
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.incidents import get_incident, insert_incident
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase

START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
KEY = "9adc6bbf-3a4c-4624-8c50-a11938dbdf55"
# Long enough that a transaction still waiting on a lock is clearly blocked.
BLOCKED = 0.3


def incident(description: str = "Checkout latency is up since the 18:05 deploy") -> CreateIncident:
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


async def stored(connection: Connection, owner: OwnerId) -> list[tuple[object, ...]]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute("SELECT id, description FROM incidents ORDER BY id")
        return await cursor.fetchall()


async def test_an_incident_is_stored_and_read_back(database: ScratchDatabase) -> None:
    request = incident()
    async with database.runtime() as connection:
        result = await create_incident(connection, OWNER_A, KEY, request)
        async with owner_transaction(connection, OWNER_A):
            read = await get_incident(connection, result.incident.id)

    assert result.created
    assert read == result.incident
    assert (read.description, read.service, read.severity, read.window) == (
        request.description,
        request.service,
        request.severity,
        request.window,
    )
    assert read.sanitizer_version == "1"


async def test_the_description_is_sanitized_before_it_is_stored(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        result = await create_incident(
            connection,
            OWNER_A,
            KEY,
            incident("Checkout down‮, pwd=hunter2 ip-canary-0123456789abcdef"),
        )
        rows = await stored(connection, OWNER_A)

    clean = "Checkout down, pwd=[REDACTED-credential] [REDACTED-canary]"
    assert result.incident.description == clean
    assert rows == [(result.incident.id, clean)]


async def test_a_retry_returns_the_original_incident(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        first = await create_incident(connection, OWNER_A, KEY, incident())
        retry = await create_incident(connection, OWNER_A, KEY, incident())

        assert (retry.incident, retry.created) == (first.incident, False)
        assert len(await stored(connection, OWNER_A)) == 1


async def test_the_same_key_with_a_different_request_conflicts(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await create_incident(connection, OWNER_A, KEY, incident())
        message = f"idempotency key {KEY!r} was used for a different request"
        with pytest.raises(IdempotencyConflict, match=exactly(message)):
            await create_incident(connection, OWNER_A, KEY, incident("Something else"))

        assert len(await stored(connection, OWNER_A)) == 1


async def test_different_keys_create_different_incidents(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        first = await create_incident(connection, OWNER_A, "key-1", incident())
        second = await create_incident(connection, OWNER_A, "key-2", incident())

        assert first.incident.id != second.incident.id
        assert len(await stored(connection, OWNER_A)) == 2


async def test_keys_belong_to_their_owner(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        mine = await create_incident(connection, OWNER_A, KEY, incident())
        theirs = await create_incident(connection, OWNER_B, KEY, incident())

        assert theirs.created
        assert mine.incident.id != theirs.incident.id
        async with owner_transaction(connection, OWNER_B):
            assert await get_incident(connection, mine.incident.id) is None
        assert [id_ for id_, _ in await stored(connection, OWNER_B)] == [theirs.incident.id]


async def hold_key(connection: Connection, request: CreateIncident) -> IncidentId:
    """Claim the key and insert the incident, as create_incident does, without committing."""
    incident_id = IncidentId(uuid4())
    assert await idempotency.claim(
        connection, OPERATION, KEY, fingerprint(OWNER_A, request), incident_id
    )
    await insert_incident(connection, incident_id, request, request.description, "1")
    return incident_id


async def test_a_concurrent_retry_waits_and_returns_the_first_incident(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as first, database.runtime() as second:
        async with owner_transaction(first, OWNER_A):
            held = await hold_key(first, incident())
            retry = asyncio.create_task(create_incident(second, OWNER_A, KEY, incident()))
            await asyncio.sleep(BLOCKED)
            assert not retry.done()

        result = await retry
        assert (result.incident.id, result.created) == (held, False)
        assert len(await stored(first, OWNER_A)) == 1


async def test_a_concurrent_different_request_conflicts_once_the_first_commits(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as first, database.runtime() as second:
        async with owner_transaction(first, OWNER_A):
            await hold_key(first, incident())
            other = asyncio.create_task(create_incident(second, OWNER_A, KEY, incident("Other")))
            await asyncio.sleep(BLOCKED)
            assert not other.done()

        with pytest.raises(IdempotencyConflict):
            await other


class RolledBack(Exception):
    pass


async def hold_then_roll_back(
    first: Connection, second: Connection
) -> tuple[IncidentId, asyncio.Task[CreatedIncident]]:
    try:
        async with owner_transaction(first, OWNER_A):
            held = await hold_key(first, incident())
            waiting = asyncio.create_task(create_incident(second, OWNER_A, KEY, incident()))
            await asyncio.sleep(BLOCKED)
            assert not waiting.done()
            raise RolledBack
    except RolledBack:
        return held, waiting


async def test_a_waiting_request_creates_the_incident_if_the_first_rolls_back(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as first, database.runtime() as second:
        held, waiting = await hold_then_roll_back(first, second)

        result = await waiting
        assert result.created
        assert result.incident.id != held
        assert [id_ for id_, _ in await stored(first, OWNER_A)] == [result.incident.id]


async def test_racing_identical_requests_create_one_incident(database: ScratchDatabase) -> None:
    async with AsyncExitStack() as stack:
        connections = [await stack.enter_async_context(database.runtime()) for _ in range(5)]
        results = await asyncio.gather(
            *(create_incident(c, OWNER_A, KEY, incident()) for c in connections)
        )

    assert sorted(r.created for r in results) == [False] * 4 + [True]
    assert len({r.incident.id for r in results}) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE incidents SET description = 'edited'",
        "DELETE FROM incidents",
        "UPDATE idempotency_requests SET request_sha256 = repeat('0', 64)",
        "DELETE FROM idempotency_requests",
    ],
)
async def test_incidents_and_their_keys_never_change(
    database: ScratchDatabase, statement: str
) -> None:
    async with database.runtime() as connection:
        await create_incident(connection, OWNER_A, KEY, incident())
        with pytest.raises(errors.InsufficientPrivilege, match=r"^permission denied for table"):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(statement)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("window_end", START, "incidents_window_ordered"),
        ("window_end", START + timedelta(hours=24, seconds=1), "incidents_window_bounded"),
        ("severity", "sev5", "incidents_severity_check"),
        ("description", "", "incidents_description_check"),
        ("description", "x" * 2001, "incidents_description_check"),
        ("service", "Checkout", "incidents_service_check"),
        ("sanitizer_version", "v1", "incidents_sanitizer_version_check"),
    ],
)
async def test_the_table_enforces_what_the_request_checks(
    database: ScratchDatabase, column: str, value: object, constraint: str
) -> None:
    row: dict[str, object] = {
        "description": "x",
        "service": "checkout-api",
        "severity": "sev2",
        "window_start": START,
        "window_end": START + timedelta(minutes=30),
        "sanitizer_version": "1",
    } | {column: value}
    async with database.migrator() as connection:
        with pytest.raises(errors.CheckViolation, match=rf'constraint "{constraint}"'):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(
                    "INSERT INTO incidents (owner_id, id, description, service, severity,"
                    " window_start, window_end, sanitizer_version)"
                    " VALUES (current_owner_id(), gen_random_uuid(), %(description)s, %(service)s,"
                    " %(severity)s, %(window_start)s, %(window_end)s, %(sanitizer_version)s)",
                    row,
                )


@pytest.mark.parametrize("key", ["", "x" * 129, "has space"])
async def test_the_table_refuses_a_malformed_key(database: ScratchDatabase, key: str) -> None:
    async with database.runtime() as connection:
        with pytest.raises(
            errors.CheckViolation, match='constraint "idempotency_requests_key_check"'
        ):
            async with owner_transaction(connection, OWNER_A):
                await idempotency.claim(connection, OPERATION, key, "0" * 64, uuid4())
