"""Closing attempts, the sweeper and recovery (spec §6.1 to §6.3; errata E1, E9)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import (
    AttemptStatus,
    FailureCategory,
    InvestigationStatus,
    RunMode,
    TerminationReason,
)
from incidentpilot.domain.ids import AttemptId, InvestigationId, OwnerId
from incidentpilot.domain.transitions import IllegalTransitionError
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.recovery import Swept, close_attempt, sweep, sweep_once
from incidentpilot.workflows.worker import Worker
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import seed, start_request

OFFLINE = RunMode.OFFLINE_DEMO
NOW = timedelta(0)


async def queue(
    connection: Connection, owner: OwnerId = OWNER_A, n: int = 1, tag: str = "q"
) -> list[InvestigationId]:
    seeded = await seed(connection, owner, key=f"seed-{tag}")
    ids = []
    for i in range(n):
        started = await start_investigation(
            connection, owner, OFFLINE, seeded.incident_id, f"{tag}-{i}", start_request()
        )
        ids.append(started.investigation.id)
    return ids


async def claimed(connection: Connection, owner: OwnerId = OWNER_A) -> Lease:
    await queue(connection, owner)
    lease = await claim_next(connection, "crashed-worker", OFFLINE)
    assert lease is not None
    return lease


async def update(connection: Connection, owner: OwnerId, sql: str, *params: object) -> None:
    async with owner_transaction(connection, owner):
        await connection.execute(sql, params)


async def expire(connection: Connection, lease: Lease) -> None:
    await update(
        connection,
        lease.owner_id,
        "UPDATE investigations SET lease_expires_at = pg_catalog.clock_timestamp()"
        " - interval '1 second' WHERE id = %s",
        lease.investigation_id,
    )


async def investigation(
    connection: Connection, owner: OwnerId, investigation_id: InvestigationId
) -> tuple[object, ...]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT status, termination_reason, current_attempt_id, lease_expires_at,"
            " eligible_at - pg_catalog.clock_timestamp() FROM investigations WHERE id = %s",
            (investigation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return row


async def attempt(
    connection: Connection, owner: OwnerId, attempt_id: AttemptId
) -> tuple[object, ...]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT status, failure_category, ended_at IS NOT NULL FROM attempts WHERE id = %s",
            (attempt_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return row


async def event_types(
    connection: Connection, owner: OwnerId, investigation_id: InvestigationId
) -> list[str]:
    async with owner_transaction(connection, owner):
        return [kind for _, kind, _, _ in await list_events(connection, investigation_id)]


async def bound_slots(database: ScratchDatabase) -> list[object]:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT attempt_id FROM capacity_slots WHERE attempt_id IS NOT NULL"
        )
        return [row[0] for row in await cursor.fetchall()]


# close_attempt


@pytest.mark.parametrize(
    ("status", "failure", "event"),
    [
        (AttemptStatus.LOST, FailureCategory.LEASE_LOST, "ATTEMPT_LOST"),
        (AttemptStatus.FAILED, FailureCategory.TELEMETRY_ERROR, "ATTEMPT_FAILED"),
        (AttemptStatus.SUCCEEDED, None, None),
        (AttemptStatus.CANCELLED, None, None),
    ],
)
async def test_closing_an_attempt_ends_it_and_frees_its_slot(
    database: ScratchDatabase,
    status: AttemptStatus,
    failure: FailureCategory | None,
    event: str | None,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        async with owner_transaction(connection, OWNER_A):
            await close_attempt(connection, lease.attempt_id, status, failure)
            events = await list_events(connection, lease.investigation_id)

        assert await attempt(connection, OWNER_A, lease.attempt_id) == (
            status,
            failure and failure.value,
            True,
        )
    assert await bound_slots(database) == []
    expected = ["INVESTIGATION_QUEUED", "ATTEMPT_STARTED", *([event] if event else [])]
    assert [kind for _, kind, _, _ in events] == expected
    if event:
        assert events[-1][2] == {
            "attempt_id": str(lease.attempt_id),
            "failure_category": failure and failure.value,
        }


async def close(connection: Connection, lease: Lease, status: AttemptStatus) -> None:
    async with owner_transaction(connection, lease.owner_id):
        await close_attempt(connection, lease.attempt_id, status)


async def test_an_ended_attempt_cannot_be_closed_again(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await close(connection, lease, AttemptStatus.LOST)
        with pytest.raises(IllegalTransitionError):
            await close(connection, lease, AttemptStatus.FAILED)


async def test_an_unknown_attempt_cannot_be_closed(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await claimed(connection)
        missing = AttemptId(uuid4())
        with pytest.raises(LookupError, match=rf"^attempt {missing} doesn't exist$"):
            async with owner_transaction(connection, OWNER_A):
                await close_attempt(connection, missing, AttemptStatus.LOST)


# The sweeper


@pytest.mark.parametrize("status", [InvestigationStatus.RUNNING, InvestigationStatus.VALIDATING])
async def test_an_expired_lease_is_lost_and_retried(
    database: ScratchDatabase, status: InvestigationStatus
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await update(connection, OWNER_A, "UPDATE investigations SET status = %s", status)
        await expire(connection, lease)

        swept = await sweep_once(connection, backoff=timedelta(seconds=5))
        state, reason, current, lease_end, eligible_in = await investigation(
            connection, OWNER_A, lease.investigation_id
        )
        assert swept == Swept(OWNER_A, lease.investigation_id, InvestigationStatus.RETRY_WAIT, None)
        assert (state, reason, current, lease_end) == ("RETRY_WAIT", None, None, None)
        assert timedelta(seconds=4) < eligible_in <= timedelta(seconds=5)  # type: ignore[operator]
        assert await attempt(connection, OWNER_A, lease.attempt_id) == ("LOST", "LEASE_LOST", True)
        assert (await event_types(connection, OWNER_A, lease.investigation_id))[-2:] == [
            "ATTEMPT_LOST",
            "RETRY_SCHEDULED",
        ]
        with pytest.raises(StaleLease):
            async with fenced_transaction(connection, lease):
                pass
    assert await bound_slots(database) == []


async def test_a_retry_announces_its_attempt_and_backoff(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await expire(connection, lease)
        await sweep_once(connection, backoff=timedelta(seconds=3))
        async with owner_transaction(connection, OWNER_A):
            *_, (_, kind, payload, _) = await list_events(connection, lease.investigation_id)

    assert (kind, payload) == ("RETRY_SCHEDULED", {"next_attempt_no": 2, "backoff_seconds": 3.0})


async def test_a_recovered_investigation_is_claimed_again_with_a_new_epoch(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        first = await claimed(connection)
        await expire(connection, first)
        await sweep_once(connection, backoff=NOW)

        second = await claim_next(connection, "recovering-worker", OFFLINE)
        assert second is not None
        assert (second.investigation_id, second.epoch) == (first.investigation_id, 2)
        assert second.attempt_id != first.attempt_id


@pytest.mark.parametrize(("started", "outcome"), [(2, "RETRY_WAIT"), (3, "FAILED")])
async def test_recovery_attempts_run_out_after_two(
    database: ScratchDatabase, started: int, outcome: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await update(
            connection, OWNER_A, "UPDATE investigations SET attempts_started = %s", started
        )
        await expire(connection, lease)
        swept = await sweep_once(connection)

        state, reason, *_ = await investigation(connection, OWNER_A, lease.investigation_id)
    assert swept is not None
    assert (state, swept.status) == (outcome, outcome)
    assert reason == (TerminationReason.RETRIES_EXHAUSTED if outcome == "FAILED" else None)


async def test_a_lost_attempt_past_the_deadline_fails(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()",
        )
        await expire(connection, lease)
        swept = await sweep_once(connection)

        assert await event_types(connection, OWNER_A, lease.investigation_id) == [
            "INVESTIGATION_QUEUED",
            "ATTEMPT_STARTED",
            "ATTEMPT_LOST",
            "INVESTIGATION_FAILED",
        ]
    assert swept == Swept(
        OWNER_A,
        lease.investigation_id,
        InvestigationStatus.FAILED,
        TerminationReason.DEADLINE_REACHED,
    )


async def test_queued_work_older_than_the_queue_age_fails(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        old, fresh = await queue(connection, n=2)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET created_at = created_at - interval '11 minutes'"
            " WHERE id = %s",
            old,
        )
        swept = await sweep_once(connection)
        assert await sweep_once(connection) is None

        assert swept == Swept(
            OWNER_A, old, InvestigationStatus.FAILED, TerminationReason.QUEUE_AGE_EXCEEDED
        )
        async with owner_transaction(connection, OWNER_A):
            *_, (_, kind, payload, _) = await list_events(connection, old)
        assert (kind, payload) == ("INVESTIGATION_FAILED", {"reason": "queue_age_exceeded"})
        assert (await investigation(connection, OWNER_A, fresh))[0] == "QUEUED"


@pytest.mark.parametrize(
    ("deadline", "swept"), [(timedelta(seconds=-1), True), (timedelta(hours=1), False)]
)
async def test_a_retry_wait_past_its_deadline_fails(
    database: ScratchDatabase, deadline: timedelta, swept: bool
) -> None:
    async with database.runtime() as connection:
        (investigation_id,) = await queue(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET status = 'RETRY_WAIT',"
            " deadline_at = pg_catalog.clock_timestamp() + %s",
            deadline,
        )
        result = await sweep_once(connection)

        state, reason, *_ = await investigation(connection, OWNER_A, investigation_id)
    if swept:
        assert (state, reason) == ("FAILED", "deadline_reached")
        assert result is not None
    else:
        assert (state, result) == ("RETRY_WAIT", None)


async def test_a_live_lease_is_left_alone(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)

        assert await sweep_once(connection) is None
        assert (await investigation(connection, OWNER_A, lease.investigation_id))[0] == "RUNNING"


async def test_another_owners_lost_work_is_recovered_under_that_owner(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection, OWNER_B)
        await expire(connection, lease)
        swept = await sweep_once(connection)

        assert swept is not None
        assert swept.owner_id == OWNER_B
        assert "ATTEMPT_LOST" in await event_types(connection, OWNER_B, lease.investigation_id)


async def test_a_sweep_pass_handles_everything_up_to_its_limit(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        queued = await queue(connection, n=3)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET created_at = created_at - interval '11 minutes'",
        )

        first = await sweep(connection, limit=2)
        rest = await sweep(connection)

        assert len(first) == 2
        assert [s.investigation_id for s in first + rest] == queued
        assert {(s.status, s.reason) for s in first + rest} == {
            (InvestigationStatus.FAILED, TerminationReason.QUEUE_AGE_EXCEEDED)
        }
        assert await sweep(connection) == []


async def test_a_locked_investigation_is_skipped_by_the_sweeper(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as other:
        lease = await claimed(connection)
        await expire(connection, lease)
        async with owner_transaction(other, OWNER_A):
            await other.execute(
                "SELECT 1 FROM investigations WHERE id = %s FOR UPDATE", (lease.investigation_id,)
            )
            assert await asyncio.wait_for(sweep_once(connection), timeout=5) is None


async def test_a_sweeping_worker_recovers_a_crashed_workers_job(database: ScratchDatabase) -> None:
    finished: list[tuple[InvestigationId, int]] = []
    stop = asyncio.Event()

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        finished.append((lease.investigation_id, lease.epoch))
        async with owner_transaction(connection, lease.owner_id):
            await close_attempt(connection, lease.attempt_id, AttemptStatus.SUCCEEDED)
        stop.set()

    async def maintenance(connection: Connection) -> None:
        await sweep(connection)

    async with database.runtime() as connection, database.runtime() as keeping:
        crashed = await claimed(connection)
        await expire(connection, crashed)
        rescuer = Worker(
            "rescuer",
            OFFLINE,
            job,
            maintenance,
            timedelta(seconds=1),
            timedelta(seconds=0.2),
            timedelta(seconds=0.2),
        )
        await asyncio.wait_for(rescuer.run(connection, keeping, stop), timeout=10)

    assert finished == [(crashed.investigation_id, 2)]
