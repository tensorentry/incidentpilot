"""The investigation event log (spec §7.3)."""

import asyncio
import re

import pytest
from psycopg import errors

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import EventType, RunMode
from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import EventError, append_event
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import seed, start_request


async def started(connection: Connection) -> InvestigationId:
    seeded = await seed(connection, OWNER_A)
    result = await start_investigation(
        connection, OWNER_A, RunMode.OFFLINE_DEMO, seeded.incident_id, "start", start_request()
    )
    return result.investigation.id


async def sequence(connection: Connection, investigation_id: InvestigationId) -> list[object]:
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute(
            "SELECT seq FROM run_events WHERE investigation_id = %s ORDER BY seq",
            (investigation_id,),
        )
        return [seq for (seq,) in await cursor.fetchall()]


async def test_events_take_consecutive_numbers(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await started(connection)
        async with owner_transaction(connection, OWNER_A):
            numbers = [
                await append_event(connection, investigation_id, EventType.RETRY_SCHEDULED, {})
                for _ in range(3)
            ]

        assert numbers == [2, 3, 4]
        assert await sequence(connection, investigation_id) == [1, 2, 3, 4]


async def test_concurrent_appends_never_collide(database: ScratchDatabase) -> None:
    async with database.runtime() as setup:
        investigation_id = await started(setup)

    async def append_twice(connection: Connection) -> list[int]:
        numbers = []
        for _ in range(2):
            async with owner_transaction(connection, OWNER_A):
                numbers.append(
                    await append_event(connection, investigation_id, EventType.RETRY_SCHEDULED, {})
                )
        return numbers

    async with database.runtime() as a, database.runtime() as b, database.runtime() as c:
        results = await asyncio.gather(append_twice(a), append_twice(b), append_twice(c))

        assert sorted(n for numbers in results for n in numbers) == [2, 3, 4, 5, 6, 7]
        assert await sequence(a, investigation_id) == [1, 2, 3, 4, 5, 6, 7]


async def append_then_fail(connection: Connection, investigation_id: InvestigationId) -> None:
    async with owner_transaction(connection, OWNER_A):
        await append_event(connection, investigation_id, EventType.RETRY_SCHEDULED, {})
        await connection.execute("SELECT 1 / 0")


async def test_a_rolled_back_append_leaves_no_gap(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await started(connection)
        with pytest.raises(errors.DivisionByZero):
            await append_then_fail(connection, investigation_id)
        async with owner_transaction(connection, OWNER_A):
            seq = await append_event(connection, investigation_id, EventType.RETRY_SCHEDULED, {})

        assert seq == 2


async def test_a_stored_payload_is_sanitized(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await started(connection)
        async with owner_transaction(connection, OWNER_A):
            seq = await append_event(
                connection, investigation_id, EventType.RETRY_SCHEDULED, {"note": "token=abc123"}
            )
            cursor = await connection.execute(
                "SELECT payload FROM run_events WHERE investigation_id = %s AND seq = %s",
                (investigation_id, seq),
            )
            assert await cursor.fetchone() == ({"note": "token=[REDACTED-credential]"},)


async def test_an_event_for_another_owners_investigation_is_refused(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        investigation_id = await started(connection)
        message = f"investigation {investigation_id} doesn't exist"
        with pytest.raises(EventError, match=rf"^{re.escape(message)}$"):
            async with owner_transaction(connection, OWNER_B):
                await append_event(connection, investigation_id, EventType.RETRY_SCHEDULED, {})


@pytest.mark.parametrize(
    "statement", ["UPDATE run_events SET payload = '{}'", "DELETE FROM run_events"]
)
async def test_events_never_change(database: ScratchDatabase, statement: str) -> None:
    async with database.runtime() as connection:
        await started(connection)
        with pytest.raises(
            errors.InsufficientPrivilege, match=r"^permission denied for table run_events"
        ):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(statement)
