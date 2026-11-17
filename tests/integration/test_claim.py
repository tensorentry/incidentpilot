"""Claiming work: attempts, capacity slots and the mode filter (spec §6.1; E8, E19).

The plan's acceptance for T3.8: two workers contend, and at most two investigations
are ever active.
"""

import asyncio
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg import errors

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import EventType, InvestigationStatus, RunMode
from incidentpilot.domain.ids import InvestigationId, LeaseEpoch, OwnerId
from incidentpilot.persistence.database import (
    Connection,
    OwnerContextError,
    adopt_owner,
    owner_transaction,
    scheduler_transaction,
)
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease, fenced_transaction
from incidentpilot.workflows.claim import claim_next
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import ENDED_BECAUSE, seed, start_request

OFFLINE = RunMode.OFFLINE_DEMO


async def queue(
    connection: Connection, owner: OwnerId, n: int, mode: RunMode = OFFLINE, tag: str = "q"
) -> list[InvestigationId]:
    seeded = await seed(connection, owner, key=f"seed-{tag}")
    ids = []
    for i in range(n):
        started = await start_investigation(
            connection,
            owner,
            mode,
            seeded.incident_id,
            f"{tag}-{i}",
            start_request(mode=mode.value),
        )
        ids.append(started.investigation.id)
    return ids


async def state(
    connection: Connection, owner: OwnerId, investigation_id: InvestigationId
) -> tuple[object, ...]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT status, lease_epoch, attempts_started, current_attempt_id,"
            " lease_expires_at - pg_catalog.clock_timestamp(),"
            " deadline_at - pg_catalog.clock_timestamp() FROM investigations WHERE id = %s",
            (investigation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return row


async def slots(database: ScratchDatabase) -> list[tuple[object, ...]]:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT id, owner_id, investigation_id, attempt_id, lease_epoch"
            " FROM capacity_slots ORDER BY id"
        )
        return await cursor.fetchall()


async def test_a_claim_starts_an_attempt_and_binds_a_slot(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        (investigation_id,) = await queue(connection, OWNER_A, 1)
        lease = await claim_next(connection, "worker-1", OFFLINE)
        assert lease is not None
        status, epoch, started, attempt, lease_left, deadline_left = await state(
            connection, OWNER_A, investigation_id
        )
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT attempt_no, status, worker_id, lease_epoch, ended_at"
                " FROM attempts WHERE id = %s",
                (lease.attempt_id,),
            )
            attempt_row = await cursor.fetchone()
            cursor = await connection.execute(
                "SELECT seq, event_type, payload FROM run_events"
                " WHERE investigation_id = %s AND seq = 2",
                (investigation_id,),
            )
            event = await cursor.fetchone()

    assert lease == Lease(OWNER_A, investigation_id, lease.attempt_id, LeaseEpoch(1))
    assert (status, epoch, started, attempt) == ("RUNNING", 1, 1, lease.attempt_id)
    assert timedelta(seconds=25) < lease_left <= timedelta(seconds=30)  # type: ignore[operator]
    assert timedelta(seconds=175) < deadline_left <= timedelta(seconds=180)  # type: ignore[operator]
    assert attempt_row == (1, "RUNNING", "worker-1", 1, None)
    assert event == (
        2,
        EventType.ATTEMPT_STARTED,
        {
            "attempt_id": str(lease.attempt_id),
            "attempt_no": 1,
            "worker_id": "worker-1",
            "lease_epoch": 1,
            "slot": 1,
        },
    )
    assert await slots(database) == [
        (1, OWNER_A, investigation_id, lease.attempt_id, 1),
        (2, None, None, None, None),
    ]


async def test_a_claimed_lease_passes_the_fence(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection, OWNER_A, 1)
        lease = await claim_next(connection, "worker-1", OFFLINE)
        assert lease is not None

        async with fenced_transaction(connection, lease):
            await append_event(connection, lease.investigation_id, EventType.RETRY_SCHEDULED, {})


async def test_nothing_to_claim_returns_none(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        assert await claim_next(connection, "worker-1", OFFLINE) is None
    assert [row[3] for row in await slots(database)] == [None, None]


async def test_a_worker_claims_only_its_own_mode(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        (live,) = await queue(connection, OWNER_A, 1, mode=RunMode.LIVE)

        assert await claim_next(connection, "offline-worker", OFFLINE) is None
        lease = await claim_next(connection, "live-worker", RunMode.LIVE)
        assert lease is not None
        assert lease.investigation_id == live


async def test_the_oldest_investigation_is_claimed_first(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        first, second = await queue(connection, OWNER_A, 2)

        claimed = [await claim_next(connection, f"w{i}", OFFLINE) for i in range(2)]
        assert [lease.investigation_id for lease in claimed if lease] == [first, second]


async def test_at_most_two_investigations_run_at_once(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        first, second, third = await queue(connection, OWNER_A, 3)
        leases = [await claim_next(connection, f"w{i}", OFFLINE) for i in range(3)]

        assert [lease.investigation_id if lease else None for lease in leases] == [
            first,
            second,
            None,
        ]
        status, *_ = await state(connection, OWNER_A, third)
        assert status == "QUEUED"
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute("SELECT count(*) FROM attempts")
            assert await cursor.fetchone() == (2,)


async def test_releasing_a_slot_lets_the_next_claim_in(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        _, _, third = await queue(connection, OWNER_A, 3)
        first = await claim_next(connection, "w1", OFFLINE)
        await claim_next(connection, "w2", OFFLINE)
        assert first is not None
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT release_capacity_slot(%s)", (first.attempt_id,)
            )
            assert await cursor.fetchone() == (True,)

        lease = await claim_next(connection, "w3", OFFLINE)
        assert lease is not None
        assert lease.investigation_id == third


@pytest.mark.parametrize("round_", range(3))
async def test_contending_workers_never_run_more_than_two(
    database: ScratchDatabase, round_: int
) -> None:
    async with database.runtime() as setup:
        await queue(setup, OWNER_A, 2, tag="a")
        await queue(setup, OWNER_B, 2, tag="b")
    async with AsyncExitStack() as stack:
        workers = [await stack.enter_async_context(database.runtime()) for _ in range(5)]
        leases = await asyncio.gather(
            *(claim_next(w, f"worker-{i}-{round_}", OFFLINE) for i, w in enumerate(workers))
        )

    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 2
    assert len({lease.investigation_id for lease in claimed}) == 2
    assert [row[0] for row in await slots(database) if row[3] is not None] == [1, 2]


async def test_a_locked_investigation_is_skipped_not_waited_for(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as other:
        first, second = await queue(connection, OWNER_A, 2)
        async with scheduler_transaction(other):
            cursor = await other.execute(
                "SELECT investigation_id FROM claimable_investigation(%s)", (OFFLINE,)
            )
            assert await cursor.fetchone() == (first,)
            lease = await asyncio.wait_for(claim_next(connection, "w2", OFFLINE), timeout=5)

        assert lease is not None
        assert lease.investigation_id == second


async def test_another_owners_work_is_claimed_under_that_owner(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        (theirs,) = await queue(connection, OWNER_B, 1)
        lease = await claim_next(connection, "worker-1", OFFLINE)
        assert lease is not None
        assert lease.owner_id == OWNER_B

        for owner, visible in [(OWNER_A, 0), (OWNER_B, 1)]:
            async with owner_transaction(connection, owner):
                cursor = await connection.execute(
                    "SELECT count(*) FROM attempts WHERE investigation_id = %s", (theirs,)
                )
                assert await cursor.fetchone() == (visible,)


async def test_a_retry_is_claimed_once_eligible_and_keeps_its_deadline(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        (investigation_id,) = await queue(connection, OWNER_A, 1)
        first = await claim_next(connection, "w1", OFFLINE)
        assert first is not None
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT deadline_at FROM investigations WHERE id = %s", (investigation_id,)
            )
            (deadline,) = await cursor.fetchone() or (None,)
            await connection.execute(
                "UPDATE attempts SET status = 'LOST', ended_at = pg_catalog.clock_timestamp()"
            )
            await connection.execute("SELECT release_capacity_slot(%s)", (first.attempt_id,))
            await connection.execute(
                "UPDATE investigations SET status = 'RETRY_WAIT',"
                " eligible_at = pg_catalog.clock_timestamp() + interval '1 hour'"
            )
        assert await claim_next(connection, "w2", OFFLINE) is None

        async with owner_transaction(connection, OWNER_A):
            await connection.execute(
                "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp()"
            )
        second = await claim_next(connection, "w2", OFFLINE)
        assert second is not None
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT attempts_started, lease_epoch, deadline_at"
                " FROM investigations WHERE id = %s",
                (investigation_id,),
            )
            assert await cursor.fetchone() == (2, 2, deadline)
    assert second.epoch == 2


@pytest.mark.parametrize(
    "status",
    [InvestigationStatus.RUNNING, InvestigationStatus.VALIDATING, InvestigationStatus.COMPLETED],
)
async def test_work_that_isnt_waiting_is_never_claimed(
    database: ScratchDatabase, status: InvestigationStatus
) -> None:
    async with database.runtime() as connection:
        await queue(connection, OWNER_A, 1)
        async with owner_transaction(connection, OWNER_A):
            await connection.execute(
                "UPDATE investigations SET status = %s, termination_reason = %s",
                (status, ENDED_BECAUSE.get(status)),
            )

        assert await claim_next(connection, "w1", OFFLINE) is None


async def test_a_slot_cannot_be_bound_for_another_owners_attempt(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection, OWNER_A, 1)
        lease = await claim_next(connection, "w1", OFFLINE)
        assert lease is not None
        message = f"attempt {lease.attempt_id} does not belong to the current owner"
        with pytest.raises(errors.InsufficientPrivilege, match=re.escape(message)):
            async with owner_transaction(connection, OWNER_B):
                await connection.execute("SELECT bind_capacity_slot(%s, 1)", (lease.attempt_id,))
        async with owner_transaction(connection, OWNER_B):
            cursor = await connection.execute(
                "SELECT release_capacity_slot(%s)", (lease.attempt_id,)
            )
            assert await cursor.fetchone() == (False,)
    assert [row[3] for row in await slots(database)] == [lease.attempt_id, None]


@pytest.mark.parametrize(
    "statement", ["SELECT * FROM capacity_slots", "UPDATE capacity_slots SET attempt_id = NULL"]
)
async def test_the_runtime_role_reaches_slots_only_through_functions(
    database: ScratchDatabase, statement: str
) -> None:
    async with database.runtime() as connection:
        with pytest.raises(
            errors.InsufficientPrivilege, match=r"^permission denied for table capacity_slots"
        ):
            await connection.execute(statement)


@pytest.mark.parametrize("worker_id", ["", "has space", "x" * 129])
async def test_a_malformed_worker_id_is_refused(database: ScratchDatabase, worker_id: str) -> None:
    async with database.runtime() as connection:
        with pytest.raises(
            ValueError, match=r"^a worker ID is 1 to 128 letters, digits or \. _ : -$"
        ):
            await claim_next(connection, worker_id, OFFLINE)


async def adopt_twice(connection: Connection) -> None:
    async with scheduler_transaction(connection):
        await adopt_owner(connection, OWNER_A)
        await adopt_owner(connection, OWNER_B)


async def nest(connection: Connection) -> None:
    async with connection.transaction(), scheduler_transaction(connection):
        pass


async def test_an_owner_is_adopted_once_and_only_inside_a_transaction(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        with pytest.raises(
            OwnerContextError, match=r"^an owner can only be adopted inside a transaction$"
        ):
            await adopt_owner(connection, OWNER_A)
        with pytest.raises(OwnerContextError, match=r"^this transaction already has an owner$"):
            await adopt_twice(connection)
        message = "a scheduler transaction can't start inside another transaction"
        with pytest.raises(OwnerContextError, match=rf"^{re.escape(message)}$"):
            await nest(connection)


async def test_a_scheduler_transaction_sees_no_tenant_rows(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await queue(connection, OWNER_A, 1)
        async with scheduler_transaction(connection):
            cursor = await connection.execute("SELECT count(*) FROM investigations")
            assert await cursor.fetchone() == (0,)


async def test_a_slot_held_by_an_open_claim_is_skipped_not_waited_for(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as other:
        first, second = await queue(connection, OWNER_A, 2)
        async with scheduler_transaction(other):
            # Part way through a claim: the first investigation and slot 1 are locked.
            await other.execute("SELECT * FROM claimable_investigation(%s)", (OFFLINE,))
            await adopt_owner(other, OWNER_A)
            attempt = uuid4()
            await other.execute(
                "INSERT INTO attempts (owner_id, id, investigation_id, attempt_no, worker_id,"
                " lease_epoch) VALUES (current_owner_id(), %s, %s, 1, 'w1', 1)",
                (attempt, first),
            )
            cursor = await other.execute("SELECT bind_capacity_slot(%s, 1)", (attempt,))
            assert await cursor.fetchone() == (1,)

            lease = await asyncio.wait_for(claim_next(connection, "w2", OFFLINE), timeout=5)

        assert lease is not None
        assert lease.investigation_id == second


async def test_the_epoch_advances_from_its_own_value_not_the_attempt_count(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        await queue(connection, OWNER_A, 1)
        async with owner_transaction(connection, OWNER_A):
            await connection.execute(
                "UPDATE investigations SET status = 'RETRY_WAIT', lease_epoch = 5,"
                " attempts_started = 1"
            )
        lease = await claim_next(connection, "w1", OFFLINE)

    assert lease is not None
    assert lease.epoch == 6
