"""Lease renewal and the worker loop (spec §6.1, D08)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import EventType, RunMode
from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.lease import LeaseKeeper, renew_lease
from incidentpilot.workflows.worker import Job, Maintenance, Worker, no_maintenance
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import seed, start_request

OFFLINE = RunMode.OFFLINE_DEMO
SHORT = timedelta(seconds=1)
OFTEN = timedelta(seconds=0.2)


async def queue(connection: Connection, n: int = 1) -> list[InvestigationId]:
    seeded = await seed(connection, OWNER_A)
    ids = []
    for i in range(n):
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, f"q-{i}", start_request()
        )
        ids.append(started.investigation.id)
    return ids


async def expiry_left(connection: Connection, lease: Lease) -> timedelta:
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute(
            "SELECT lease_expires_at - pg_catalog.clock_timestamp() FROM investigations"
            " WHERE id = %s",
            (lease.investigation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    left: timedelta = row[0]
    return left


async def steal(connection: Connection, lease: Lease) -> None:
    async with owner_transaction(connection, OWNER_A):
        await connection.execute(
            "UPDATE investigations SET current_attempt_id = %s, lease_epoch = lease_epoch + 1"
            " WHERE id = %s",
            (uuid4(), lease.investigation_id),
        )


async def write(connection: Connection, lease: Lease) -> None:
    async with fenced_transaction(connection, lease):
        await append_event(connection, lease.investigation_id, EventType.RETRY_SCHEDULED, {})


async def test_renewal_pushes_the_expiry_out(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection)
        lease = await claim_next(connection, "w1", OFFLINE, lease=SHORT)
        assert lease is not None
        await renew_lease(connection, lease, timedelta(minutes=5))

        assert await expiry_left(connection, lease) > timedelta(minutes=4)


async def test_a_stale_lease_cannot_be_renewed(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection)
        lease = await claim_next(connection, "w1", OFFLINE)
        assert lease is not None
        await steal(connection, lease)
        before = await expiry_left(connection, lease)

        with pytest.raises(StaleLease):
            await renew_lease(connection, lease, timedelta(minutes=5))
        assert await expiry_left(connection, lease) <= before


async def test_without_renewal_a_short_lease_lapses(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection)
        lease = await claim_next(connection, "w1", OFFLINE, lease=SHORT)
        assert lease is not None
        await asyncio.sleep(1.2)

        with pytest.raises(StaleLease):
            await write(connection, lease)


async def test_a_keeper_holds_a_lease_past_its_first_expiry(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await claim_next(connection, "w1", OFFLINE, lease=SHORT)
        assert lease is not None
        keeper = LeaseKeeper(keeping, lease, SHORT, OFTEN)
        renewing = asyncio.create_task(keeper.run())
        await asyncio.sleep(1.6)

        await write(connection, lease)
        keeper.require()
        # Renewed with the keeper's duration, not the 30-second default.
        assert await expiry_left(connection, lease) <= SHORT
        keeper.stop()
        await renewing
        assert not keeper.lost.is_set()


async def test_a_keeper_notices_a_lost_lease_and_stops_dispatch(
    database: ScratchDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await claim_next(connection, "w1", OFFLINE, lease=SHORT)
        assert lease is not None
        keeper = LeaseKeeper(keeping, lease, SHORT, OFTEN)
        renewing = asyncio.create_task(keeper.run())
        await steal(connection, lease)

        await asyncio.wait_for(renewing, timeout=2)
        assert keeper.lost.is_set()
        assert caplog.messages == [f"lost the lease on investigation {lease.investigation_id}"]
        with pytest.raises(StaleLease):
            keeper.require()


def worker(
    job: Job,
    maintenance: Maintenance = no_maintenance,
    poll_interval: timedelta = timedelta(seconds=0.05),
) -> Worker:
    return Worker("worker-1", OFFLINE, job, maintenance, SHORT, OFTEN, poll_interval)


async def test_run_once_with_nothing_queued_does_nothing(database: ScratchDatabase) -> None:
    calls: list[Lease] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        calls.append(lease)

    async with database.runtime() as connection, database.runtime() as keeping:
        assert await worker(job).run_once(connection, keeping) is False
    assert calls == []


async def test_run_once_runs_the_job_under_a_kept_lease(database: ScratchDatabase) -> None:
    seen: list[tuple[Lease, bool]] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        # Claimed with the worker's lease duration, not the 30-second default.
        assert await expiry_left(connection, lease) <= SHORT
        await asyncio.sleep(1.4)
        keeper.require()
        await write(connection, lease)
        seen.append((lease, keeper.lost.is_set()))

    async with database.runtime() as connection, database.runtime() as keeping:
        (investigation_id,) = await queue(connection)
        assert await worker(job).run_once(connection, keeping) is True

    ((lease, lost),) = seen
    assert (lease.investigation_id, lost) == (investigation_id, False)


async def test_a_failing_job_doesnt_stop_the_worker(
    database: ScratchDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        raise RuntimeError("the model adapter exploded")

    async with database.runtime() as connection, database.runtime() as keeping:
        (investigation_id,) = await queue(connection)
        assert await worker(job).run_once(connection, keeping) is True

    (record,) = caplog.records
    assert record.getMessage() == f"job failed on investigation {investigation_id}"
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "the model adapter exploded"


async def test_the_loop_works_the_queue_runs_maintenance_and_stops(
    database: ScratchDatabase,
) -> None:
    done: list[InvestigationId] = []
    maintained: list[int] = []
    stop = asyncio.Event()

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        # Long enough for the keeper to renew on its own connection.
        await asyncio.sleep(0.5)
        keeper.require()
        done.append(lease.investigation_id)
        async with owner_transaction(connection, lease.owner_id):
            await connection.execute("SELECT release_capacity_slot(%s)", (lease.attempt_id,))

    async def maintenance(connection: Connection) -> None:
        await connection.execute("SELECT 1")
        maintained.append(1)
        if len(done) == 2:
            stop.set()

    async with database.runtime() as connection, database.runtime() as keeping:
        queued = await queue(connection, 2)
        await asyncio.wait_for(
            worker(job, maintenance=maintenance).run(connection, keeping, stop), timeout=10
        )

    assert done == queued
    assert len(maintained) >= 3


async def test_the_loop_waits_when_idle_and_stops_promptly(database: ScratchDatabase) -> None:
    stop = asyncio.Event()
    maintained: list[int] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        raise AssertionError("nothing is queued")

    async def maintenance(connection: Connection) -> None:
        maintained.append(1)

    async with database.runtime() as connection, database.runtime() as keeping:
        loop = asyncio.create_task(
            worker(job, maintenance, poll_interval=timedelta(seconds=30)).run(
                connection, keeping, stop
            )
        )
        await asyncio.sleep(0.2)
        assert not loop.done()
        stop.set()
        await asyncio.wait_for(loop, timeout=2)

    # One pass, then a wait: an idle worker doesn't spin.
    assert maintained == [1]


async def test_an_idle_loop_picks_up_work_that_arrives_later(database: ScratchDatabase) -> None:
    stop = asyncio.Event()
    done: list[InvestigationId] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        done.append(lease.investigation_id)
        stop.set()

    async with database.runtime() as connection, database.runtime() as keeping:
        async with database.runtime() as other:
            loop = asyncio.create_task(worker(job).run(connection, keeping, stop))
            await asyncio.sleep(0.2)
            queued = await queue(other)
        await asyncio.wait_for(loop, timeout=5)

    assert done == queued


async def test_a_stolen_lease_reaches_the_job(database: ScratchDatabase) -> None:
    outcomes: list[str] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        await steal(connection, lease)
        await asyncio.sleep(0.6)
        try:
            keeper.require()
        except StaleLease:
            outcomes.append("stopped")

    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        await worker(job).run_once(connection, keeping)

    assert outcomes == ["stopped"]
