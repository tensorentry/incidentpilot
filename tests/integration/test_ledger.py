"""The model-call ledger (spec §11.1, D14; errata E9 and E18)."""

import asyncio
import re
from typing import Any

import pytest
from psycopg import errors

from incidentpilot.domain.enums import AttemptStatus, FailureCategory
from incidentpilot.domain.ids import ModelCallId
from incidentpilot.domain.transitions import IllegalTransitionError
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.persistence.ledger import (
    BudgetDenied,
    Usage,
    dispatch_call,
    fail_call,
    mark_call_unknown,
    reserve_call,
    settle_call,
)
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.recovery import close_attempt, sweep_once
from tests.integration.accounting import counters
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase, still_blocked
from tests.integration.seeding import OFFLINE, claimed, expire, update

ESTIMATE = 20_000
USAGE = Usage(input_tokens=1_200, output_tokens=300, cost_micro_usd=7_500, provider_request_id="r1")


async def call_row(connection: Connection, lease: Lease, call_id: ModelCallId) -> dict[str, Any]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT state, reserved_cost_micro_usd, input_tokens, output_tokens,"
            " actual_cost_micro_usd, provider_request_id, failure_category,"
            " dispatched_at IS NOT NULL AS sent, ended_at IS NOT NULL AS ended, attempt_id"
            " FROM model_calls WHERE id = %s",
            (call_id,),
        )
        row = await cursor.fetchone()
        assert cursor.description is not None
        names = [column.name for column in cursor.description]
    assert row is not None
    return dict(zip(names, row, strict=True))


async def events(connection: Connection, lease: Lease) -> list[tuple[str, object]]:
    async with owner_transaction(connection, lease.owner_id):
        listed = await list_events(connection, lease.investigation_id)
    return [(kind, payload) for _, kind, payload, _ in listed if kind.startswith("MODEL_CALL")]


async def reserve(connection: Connection, lease: Lease, estimate: int = ESTIMATE) -> ModelCallId:
    async with fenced_transaction(connection, lease):
        return await reserve_call(connection, lease, estimate)


def denial(lease: Lease, limit: str) -> str:
    message = f"investigation {lease.investigation_id} can't reserve a model call: {limit}"
    return rf"^{re.escape(message)}$"


async def dispatched(connection: Connection, lease: Lease, estimate: int = ESTIMATE) -> ModelCallId:
    call_id = await reserve(connection, lease, estimate)
    async with fenced_transaction(connection, lease):
        await dispatch_call(connection, lease, call_id)
    return call_id


async def settled(connection: Connection, lease: Lease, usage: Usage = USAGE) -> ModelCallId:
    call_id = await dispatched(connection, lease)
    async with fenced_transaction(connection, lease):
        await settle_call(connection, lease, call_id, usage)
    return call_id


# Each change and its effect


async def test_a_reservation_holds_a_request_and_its_estimate(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await reserve(connection, lease)

        row = await call_row(connection, lease, call_id)
        assert (row["state"], row["reserved_cost_micro_usd"], row["attempt_id"]) == (
            "RESERVED",
            ESTIMATE,
            lease.attempt_id,
        )
        assert (row["sent"], row["ended"]) == (False, False)
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "cost_reserved_micro_usd": ESTIMATE,
        }
        assert await events(connection, lease) == []


async def test_dispatch_is_recorded_before_sending(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)

        row = await call_row(connection, lease, call_id)
        assert (row["state"], row["sent"], row["ended"]) == ("DISPATCHED", True, False)
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "cost_reserved_micro_usd": ESTIMATE,
        }
        assert await events(connection, lease) == [
            ("MODEL_CALL_DISPATCHED", {"model_call_id": str(call_id)})
        ]


async def test_settling_replaces_the_estimate_with_actual_usage(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await settled(connection, lease)

        row = await call_row(connection, lease, call_id)
        assert row | {"attempt_id": None} == {
            "state": "SETTLED",
            "reserved_cost_micro_usd": ESTIMATE,
            "input_tokens": 1_200,
            "output_tokens": 300,
            "actual_cost_micro_usd": 7_500,
            "provider_request_id": "r1",
            "failure_category": None,
            "sent": True,
            "ended": True,
            "attempt_id": None,
        }
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "model_requests_settled": 1,
            "cost_known_micro_usd": 7_500,
            "input_tokens_actual": 1_200,
            "output_tokens_actual": 300,
        }
        assert (await events(connection, lease))[-1] == (
            "MODEL_CALL_SETTLED",
            {
                "model_call_id": str(call_id),
                "input_tokens": 1_200,
                "output_tokens": 300,
                "cost_micro_usd": 7_500,
            },
        )


async def test_a_call_that_was_never_sent_fails_and_releases(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await reserve(connection, lease)
        async with fenced_transaction(connection, lease):
            await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)

        row = await call_row(connection, lease, call_id)
        assert (row["state"], row["failure_category"], row["sent"], row["ended"]) == (
            "FAILED",
            "PROVIDER_ERROR",
            False,
            True,
        )
        assert await counters(connection, lease) == {}
        assert await events(connection, lease) == [
            (
                "MODEL_CALL_FAILED",
                {
                    "model_call_id": str(call_id),
                    "failure_category": "PROVIDER_ERROR",
                    "sent": False,
                },
            )
        ]


async def test_a_sent_call_that_fails_keeps_its_reservation(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)
        async with fenced_transaction(connection, lease):
            await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)

        row = await call_row(connection, lease, call_id)
        assert (row["state"], row["sent"], row["input_tokens"]) == ("FAILED", True, None)
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "cost_unknown_micro_usd": ESTIMATE,
        }
        assert (await events(connection, lease))[-1] == (
            "MODEL_CALL_FAILED",
            {"model_call_id": str(call_id), "failure_category": "PROVIDER_ERROR", "sent": True},
        )


async def test_an_unknown_outcome_keeps_its_reservation(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)
        async with fenced_transaction(connection, lease):
            await mark_call_unknown(connection, lease, call_id)

        row = await call_row(connection, lease, call_id)
        assert (row["state"], row["failure_category"], row["ended"]) == ("UNKNOWN", None, True)
        assert await counters(connection, lease) == {
            "model_requests_reserved": 1,
            "model_requests_dispatched": 1,
            "model_requests_unknown": 1,
            "cost_unknown_micro_usd": ESTIMATE,
        }
        assert (await events(connection, lease))[-1] == (
            "MODEL_CALL_UNKNOWN",
            {"model_call_id": str(call_id)},
        )


# Limits


async def test_the_request_limit_is_enforced(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        calls = [await reserve(connection, lease, 1) for _ in range(10)]

        with pytest.raises(BudgetDenied, match=denial(lease, "its budget is spent")) as denied:
            await reserve(connection, lease, 1)
        assert denied.value.past_deadline is False
        assert await counters(connection, lease) == {
            "model_requests_reserved": 10,
            "cost_reserved_micro_usd": 10,
        }

        # A call released before it was sent gives its request back.
        async with fenced_transaction(connection, lease):
            await fail_call(connection, lease, calls[0], FailureCategory.PROVIDER_ERROR)
        await reserve(connection, lease, 1)


async def test_a_sent_call_never_gives_its_request_back(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        first = await dispatched(connection, lease, 1)
        for _ in range(9):
            await reserve(connection, lease, 1)
        async with fenced_transaction(connection, lease):
            await fail_call(connection, lease, first, FailureCategory.PROVIDER_ERROR)

        with pytest.raises(BudgetDenied):
            await reserve(connection, lease, 1)


async def test_the_spend_cap_counts_held_known_and_unknown_cost(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await settled(connection, lease, Usage(1, 1, 100_000))  # known: 100,000
        unknown = await dispatched(connection, lease, 150_000)
        async with fenced_transaction(connection, lease):
            await mark_call_unknown(connection, lease, unknown)  # unknown: 150,000
        await reserve(connection, lease, 200_000)  # held: 200,000

        # The cap is 500,000. The first call's 20,000 estimate was replaced by its usage.
        with pytest.raises(BudgetDenied, match=denial(lease, "its budget is spent")):
            await reserve(connection, lease, 50_001)
        await reserve(connection, lease, 50_000)


async def test_nothing_is_reserved_after_the_deadline(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()"
            " - interval '1 second' WHERE id = %s",
            lease.investigation_id,
        )
        with pytest.raises(BudgetDenied, match=denial(lease, "its deadline has passed")) as denied:
            await reserve(connection, lease)
        assert denied.value.past_deadline is True
        assert await counters(connection, lease) == {}


async def test_a_negative_estimate_is_refused(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        with pytest.raises(ValueError, match=r"^a cost estimate can't be negative$"):
            await reserve(connection, lease, -1)
        assert await counters(connection, lease) == {}

        # A free request still takes one of the investigation's requests.
        await reserve(connection, lease, 0)
        assert await counters(connection, lease) == {"model_requests_reserved": 1}


# Illegal changes


async def test_a_call_must_be_dispatched_before_it_settles(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await reserve(connection, lease)
        with pytest.raises(IllegalTransitionError):
            async with fenced_transaction(connection, lease):
                await settle_call(connection, lease, call_id, USAGE)
        assert (await call_row(connection, lease, call_id))["state"] == "RESERVED"


@pytest.mark.parametrize("end", ["settle", "unknown", "fail"])
async def test_an_ended_call_cannot_change_again(database: ScratchDatabase, end: str) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)

        async def finish(how: str) -> None:
            if how == "settle":
                await settle_call(connection, lease, call_id, USAGE)
            elif how == "unknown":
                await mark_call_unknown(connection, lease, call_id)
            else:
                await fail_call(connection, lease, call_id, FailureCategory.PROVIDER_ERROR)

        async with fenced_transaction(connection, lease):
            await finish(end)
        before = await counters(connection, lease)
        for again in ("settle", "unknown", "fail"):
            with pytest.raises(IllegalTransitionError):
                async with fenced_transaction(connection, lease):
                    await finish(again)
        assert await counters(connection, lease) == before


async def test_only_the_calls_own_attempt_can_change_it(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)
        other = await claimed(connection, tag="other")
        with pytest.raises(LookupError, match="doesn't belong"):
            async with fenced_transaction(connection, other):
                await settle_call(connection, other, call_id, USAGE)
        assert (await call_row(connection, lease, call_id))["state"] == "DISPATCHED"


async def test_another_owners_call_is_out_of_reach(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection, OWNER_B)
        call_id = await dispatched(connection, lease)
        # Same attempt ID, wrong owner: row-level security hides the call.
        impostor = Lease(OWNER_A, lease.investigation_id, lease.attempt_id, lease.epoch)
        async with owner_transaction(connection, OWNER_A):
            with pytest.raises(LookupError):
                await settle_call(connection, impostor, call_id, USAGE)
        assert (await call_row(connection, lease, call_id))["state"] == "DISPATCHED"


async def test_a_call_changes_once_even_outside_the_fence(database: ScratchDatabase) -> None:
    # Workers change calls under the investigation lock. The call's own row lock keeps
    # a caller that forgot it from moving the same call twice.
    async with database.runtime() as connection, database.runtime() as racing:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            await settle_call(connection, lease, call_id, USAGE)

            async def settle_again() -> None:
                async with owner_transaction(racing, OWNER_A):
                    await settle_call(racing, lease, call_id, USAGE)

            second = asyncio.create_task(settle_again())
            assert await still_blocked(second)
        with pytest.raises(IllegalTransitionError):
            await second
        assert (await counters(connection, lease))["model_requests_settled"] == 1


# Closing an attempt (E9)


async def test_closing_an_attempt_settles_its_ledger(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        done = await settled(connection, lease)
        in_flight = await dispatched(connection, lease)
        unsent = await reserve(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            await close_attempt(
                connection, lease.attempt_id, AttemptStatus.LOST, FailureCategory.LEASE_LOST
            )

        states = [
            ((row := await call_row(connection, lease, call))["state"], row["failure_category"])
            for call in (done, in_flight, unsent)
        ]
        assert states == [("SETTLED", None), ("UNKNOWN", None), ("FAILED", "LEASE_LOST")]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 2,
            "model_requests_dispatched": 2,
            "model_requests_settled": 1,
            "model_requests_unknown": 1,
            "cost_known_micro_usd": 7_500,
            "cost_unknown_micro_usd": ESTIMATE,
            "input_tokens_actual": 1_200,
            "output_tokens_actual": 300,
        }
        assert [kind for kind, _ in await events(connection, lease)][-2:] == [
            "MODEL_CALL_UNKNOWN",
            "MODEL_CALL_FAILED",
        ]


async def test_a_cancelled_attempt_fails_unsent_calls_without_a_category(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        unsent = await reserve(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            await close_attempt(connection, lease.attempt_id, AttemptStatus.CANCELLED)
        row = await call_row(connection, lease, unsent)
        assert (row["state"], row["failure_category"]) == ("FAILED", None)
        assert await counters(connection, lease) == {}


async def test_a_lost_workers_late_answer_cannot_settle(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        call_id = await dispatched(connection, lease)
        await expire(connection, lease)
        await sweep_once(connection)

        with pytest.raises(StaleLease):
            async with fenced_transaction(connection, lease):
                await settle_call(connection, lease, call_id, USAGE)
        # UNKNOWN is final, so even a write that slipped past the fence would fail.
        async with owner_transaction(connection, OWNER_A):
            with pytest.raises(IllegalTransitionError):
                await settle_call(connection, lease, call_id, USAGE)
        assert (await call_row(connection, lease, call_id))["state"] == "UNKNOWN"


async def test_the_budget_carries_over_to_the_recovery_attempt(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await settled(connection, lease)
        await dispatched(connection, lease)
        await expire(connection, lease)
        await sweep_once(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp() WHERE id = %s",
            lease.investigation_id,
        )
        retry = await claim_next(connection, "rescuer", OFFLINE)
        assert retry is not None
        assert retry.attempt_id != lease.attempt_id

        await settled(connection, retry)
        assert await counters(connection, retry) == {
            "model_requests_reserved": 3,
            "model_requests_dispatched": 3,
            "model_requests_settled": 2,
            "model_requests_unknown": 1,
            "cost_known_micro_usd": 15_000,
            "cost_unknown_micro_usd": ESTIMATE,
            "input_tokens_actual": 2_400,
            "output_tokens_actual": 600,
        }


# The schema backs the ledger up


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE model_calls SET dispatched_at = pg_catalog.now()",
        "UPDATE model_calls SET state = 'DISPATCHED'",
        "UPDATE model_calls SET state = 'SETTLED', dispatched_at = pg_catalog.now(),"
        " ended_at = pg_catalog.now()",
        "UPDATE model_calls SET failure_category = 'PROVIDER_ERROR'",
        "UPDATE model_calls SET state = 'FAILED'",
        "UPDATE model_calls SET reserved_cost_micro_usd = -1",
        "UPDATE investigations SET model_requests_reserved = max_model_requests + 1",
        "UPDATE investigations SET model_requests_dispatched = 2",
        "UPDATE investigations SET model_requests_settled = 2, model_requests_dispatched = 1",
        "UPDATE investigations SET cost_unknown_micro_usd = -1",
    ],
)
async def test_the_schema_refuses_an_inconsistent_ledger(
    database: ScratchDatabase, change: str
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve(connection, lease)
        with pytest.raises(errors.CheckViolation):
            await update(connection, OWNER_A, change)


async def test_the_runtime_role_cannot_delete_calls(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve(connection, lease)
        with pytest.raises(errors.InsufficientPrivilege):
            await update(connection, OWNER_A, "DELETE FROM model_calls")
