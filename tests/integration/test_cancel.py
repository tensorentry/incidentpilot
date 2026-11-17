"""Cancelling an investigation (spec D09, §12.1; erratum E9)."""

import re

import pytest

from incidentpilot.application.errors import Conflict, NotFound
from incidentpilot.application.investigations import cancel_investigation, start_investigation
from incidentpilot.domain.enums import InvestigationStatus, RunMode
from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.fence import StaleLease, fenced_transaction
from incidentpilot.workflows.claim import claim_next
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import seed, start_request

OFFLINE = RunMode.OFFLINE_DEMO


async def queued(connection: Connection) -> InvestigationId:
    seeded = await seed(connection, OWNER_A)
    started = await start_investigation(
        connection, OWNER_A, OFFLINE, seeded.incident_id, "start", start_request()
    )
    return started.investigation.id


async def events(
    connection: Connection, investigation_id: InvestigationId
) -> list[tuple[str, object]]:
    async with owner_transaction(connection, OWNER_A):
        return [
            (kind, payload)
            for _, kind, payload, _ in await list_events(connection, investigation_id)
        ]


async def test_queued_work_is_cancelled(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await queued(connection)
        result = await cancel_investigation(connection, OWNER_A, investigation_id)

        assert result.changed
        assert result.investigation.status is InvestigationStatus.CANCELLED
        assert (await events(connection, investigation_id))[-1] == (
            "INVESTIGATION_CANCELLED",
            {"reason": "cancelled"},
        )
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT termination_reason FROM investigations WHERE id = %s", (investigation_id,)
            )
            assert await cursor.fetchone() == ("cancelled",)


async def test_running_work_is_cancelled_and_its_worker_fenced_out(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.migrator() as admin:
        investigation_id = await queued(connection)
        lease = await claim_next(connection, "w1", OFFLINE)
        assert lease is not None
        await cancel_investigation(connection, OWNER_A, investigation_id)

        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT status, ended_at IS NOT NULL FROM attempts WHERE id = %s",
                (lease.attempt_id,),
            )
            assert await cursor.fetchone() == ("CANCELLED", True)
        cursor = await admin.execute(
            "SELECT count(*) FROM capacity_slots WHERE attempt_id IS NOT NULL"
        )
        assert await cursor.fetchone() == (0,)
        with pytest.raises(StaleLease):
            async with fenced_transaction(connection, lease):
                pass
        assert [kind for kind, _ in await events(connection, investigation_id)] == [
            "INVESTIGATION_QUEUED",
            "ATTEMPT_STARTED",
            "INVESTIGATION_CANCELLED",
        ]


async def test_cancelling_twice_is_not_an_error(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await queued(connection)
        first = await cancel_investigation(connection, OWNER_A, investigation_id)
        again = await cancel_investigation(connection, OWNER_A, investigation_id)

        assert (first.changed, again.changed) == (True, False)
        assert again.investigation == first.investigation
        assert len(await events(connection, investigation_id)) == 2


@pytest.mark.parametrize("status", [InvestigationStatus.COMPLETED, InvestigationStatus.FAILED])
async def test_ended_work_cannot_be_cancelled(
    database: ScratchDatabase, status: InvestigationStatus
) -> None:
    async with database.runtime() as connection:
        investigation_id = await queued(connection)
        async with owner_transaction(connection, OWNER_A):
            await connection.execute("UPDATE investigations SET status = %s", (status,))

        message = f"investigation {investigation_id} has already ended: {status}"
        with pytest.raises(Conflict, match=rf"^{re.escape(message)}$"):
            await cancel_investigation(connection, OWNER_A, investigation_id)


async def test_another_owners_investigation_cannot_be_cancelled(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        investigation_id = await queued(connection)

        message = f"investigation {investigation_id} doesn't exist"
        with pytest.raises(NotFound, match=rf"^{re.escape(message)}$"):
            await cancel_investigation(connection, OWNER_B, investigation_id)
        assert len(await events(connection, investigation_id)) == 1
