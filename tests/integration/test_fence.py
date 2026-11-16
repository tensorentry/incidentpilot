"""The lease fence: a stale worker can't commit (spec §6.1, §6.5; invariant 2)."""

import asyncio
import re
from datetime import timedelta
from uuid import uuid4

import pytest

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import EventType, InvestigationStatus, RunMode
from incidentpilot.domain.ids import AttemptId, InvestigationId, LeaseEpoch
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase, still_blocked
from tests.integration.seeding import seed, start_request

LIVE = timedelta(seconds=30)


async def running(
    connection: Connection,
    status: InvestigationStatus = InvestigationStatus.RUNNING,
    expires_in: timedelta = LIVE,
) -> Lease:
    """An investigation held by one attempt at epoch 1, as a claim would leave it."""
    seeded = await seed(connection, OWNER_A)
    started = await start_investigation(
        connection, OWNER_A, RunMode.OFFLINE_DEMO, seeded.incident_id, "start", start_request()
    )
    lease = Lease(OWNER_A, started.investigation.id, AttemptId(uuid4()), LeaseEpoch(1))
    await take_over(connection, lease, status, expires_in)
    return lease


async def take_over(
    connection: Connection,
    lease: Lease,
    status: InvestigationStatus = InvestigationStatus.RUNNING,
    expires_in: timedelta = LIVE,
) -> None:
    async with owner_transaction(connection, lease.owner_id):
        await connection.execute(
            "UPDATE investigations SET status = %s, current_attempt_id = %s, lease_epoch = %s,"
            " lease_expires_at = pg_catalog.clock_timestamp() + %s WHERE id = %s",
            (status, lease.attempt_id, lease.epoch, expires_in, lease.investigation_id),
        )


async def events(connection: Connection, investigation_id: InvestigationId) -> int:
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute(
            "SELECT count(*) FROM run_events WHERE investigation_id = %s", (investigation_id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def write(connection: Connection, lease: Lease) -> None:
    async with fenced_transaction(connection, lease):
        await append_event(connection, lease.investigation_id, EventType.RETRY_SCHEDULED, {})


def stale(lease: Lease) -> str:
    message = (
        f"attempt {lease.attempt_id} no longer holds investigation {lease.investigation_id}"
        f" at epoch {lease.epoch}"
    )
    return rf"^{re.escape(message)}$"


@pytest.mark.parametrize("status", [InvestigationStatus.RUNNING, InvestigationStatus.VALIDATING])
async def test_the_current_lease_commits(
    database: ScratchDatabase, status: InvestigationStatus
) -> None:
    async with database.runtime() as connection:
        lease = await running(connection, status)
        await write(connection, lease)

        assert await events(connection, lease.investigation_id) == 2


@pytest.mark.parametrize(
    "change",
    ["older epoch", "newer epoch", "other attempt", "other owner", "other investigation"],
)
async def test_a_lease_that_isnt_current_cannot_commit(
    database: ScratchDatabase, change: str
) -> None:
    async with database.runtime() as connection:
        lease = await running(connection)
        held = {
            "older epoch": Lease(
                lease.owner_id, lease.investigation_id, lease.attempt_id, LeaseEpoch(0)
            ),
            "newer epoch": Lease(
                lease.owner_id, lease.investigation_id, lease.attempt_id, LeaseEpoch(2)
            ),
            "other attempt": Lease(
                lease.owner_id, lease.investigation_id, AttemptId(uuid4()), lease.epoch
            ),
            "other owner": Lease(OWNER_B, lease.investigation_id, lease.attempt_id, lease.epoch),
            "other investigation": Lease(
                lease.owner_id, InvestigationId(uuid4()), lease.attempt_id, lease.epoch
            ),
        }[change]

        with pytest.raises(StaleLease, match=stale(held)):
            await write(connection, held)
        assert await events(connection, lease.investigation_id) == 1


async def test_an_expired_lease_cannot_commit(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await running(connection, expires_in=timedelta(seconds=-1))

        with pytest.raises(StaleLease, match=stale(lease)):
            await write(connection, lease)
        assert await events(connection, lease.investigation_id) == 1


@pytest.mark.parametrize(
    "status",
    sorted(
        set(InvestigationStatus) - {InvestigationStatus.RUNNING, InvestigationStatus.VALIDATING}
    ),
)
async def test_an_investigation_that_isnt_running_takes_no_writes(
    database: ScratchDatabase, status: InvestigationStatus
) -> None:
    async with database.runtime() as connection:
        lease = await running(connection, status)

        with pytest.raises(StaleLease):
            await write(connection, lease)


async def test_a_lease_never_given_out_cannot_commit(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await running(connection)
        async with owner_transaction(connection, OWNER_A):
            await connection.execute(
                "UPDATE investigations SET current_attempt_id = NULL, lease_expires_at = NULL"
            )

        with pytest.raises(StaleLease):
            await write(connection, lease)


async def test_a_reclaim_waits_for_an_open_fenced_write_then_fences_it_out(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as old_worker, database.runtime() as new_worker:
        lease = await running(old_worker)
        successor = Lease(OWNER_A, lease.investigation_id, AttemptId(uuid4()), LeaseEpoch(2))

        async with fenced_transaction(old_worker, lease):
            await append_event(old_worker, lease.investigation_id, EventType.RETRY_SCHEDULED, {})
            reclaim = asyncio.create_task(take_over(new_worker, successor))
            # The reclaim needs the row lock the open fenced write holds.
            assert await still_blocked(reclaim)
        await reclaim

        with pytest.raises(StaleLease, match=stale(lease)):
            await write(old_worker, lease)
        await write(new_worker, successor)
        assert await events(old_worker, lease.investigation_id) == 3


async def test_a_stale_writes_earlier_statements_roll_back(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await running(connection)
        await take_over(
            connection, Lease(OWNER_A, lease.investigation_id, AttemptId(uuid4()), LeaseEpoch(2))
        )

        with pytest.raises(StaleLease):
            await write(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT next_event_seq FROM investigations WHERE id = %s", (lease.investigation_id,)
            )
            assert await cursor.fetchone() == (2,)


async def test_the_fence_itself_holds_the_row_until_commit(database: ScratchDatabase) -> None:
    # Evidence and tool-call writes won't touch the investigation row, so the fence's
    # own lock is what keeps a reclaim out until the fenced transaction ends.
    async with database.runtime() as old_worker, database.runtime() as new_worker:
        lease = await running(old_worker)
        successor = Lease(OWNER_A, lease.investigation_id, AttemptId(uuid4()), LeaseEpoch(2))

        async with fenced_transaction(old_worker, lease):
            reclaim = asyncio.create_task(take_over(new_worker, successor))
            assert await still_blocked(reclaim)
        await reclaim
