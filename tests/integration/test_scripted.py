"""The scripted model drives real ledger and evidence writes through recovery (T3.11)."""

import logging
from datetime import timedelta

import pytest
from psycopg import sql

from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import list_events
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import Lease, StaleLease
from incidentpilot.persistence.ledger import Usage
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.recovery import sweep_once
from incidentpilot.workflows.scripted import Crash, Fault, ScriptedModel, Step
from incidentpilot.workflows.worker import Worker
from tests.integration.accounting import counters
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import OFFLINE, expire, queue, update

ESTIMATE = 30_000


def record(n: int) -> EvidenceDraft:
    return EvidenceDraft(
        kind=EvidenceKind.LOG_RECORD,
        source_ref=f"log/checkout-api/{n}",
        adapter_version="fixture-1",
        window=None,
        content={"message": f"upstream timeout {n}"},
    )


def step(n: int, *evidence: int) -> Step:
    return Step(ESTIMATE, Usage(100 * n, 10 * n, 1_000 * n), tuple(map(record, evidence)))


SCRIPT = (step(1, 1, 2), step(2, 3), step(3))


async def run(
    connection: Connection, keeping: Connection, model: ScriptedModel, name: str = "w"
) -> Lease | None:
    """Claim and play one investigation, returning the lease it ran under."""
    leases: list[Lease] = []

    async def job(connection: Connection, lease: Lease, keeper: LeaseKeeper) -> None:
        leases.append(lease)
        await model(connection, lease, keeper)

    await Worker(name, OFFLINE, job).run_once(connection, keeping)
    return leases[0] if leases else None


async def recover(connection: Connection, lease: Lease) -> None:
    """Expire a crashed worker's lease, sweep it, and make the retry claimable now."""
    await expire(connection, lease)
    await sweep_once(connection)
    await update(
        connection,
        lease.owner_id,
        "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp() WHERE id = %s",
        lease.investigation_id,
    )


async def calls(
    connection: Connection, lease: Lease, column: str = "state"
) -> list[tuple[object, ...]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            sql.SQL(
                "SELECT attempt_id = %s, {} FROM model_calls WHERE investigation_id = %s"
                " ORDER BY created_at"
            ).format(sql.Identifier(column)),
            (lease.attempt_id, lease.investigation_id),
        )
        return list(await cursor.fetchall())


async def seen(connection: Connection, lease: Lease) -> list[str]:
    async with owner_transaction(connection, lease.owner_id):
        records = await visible_evidence(connection, lease.investigation_id)
    return [record.source_ref for record in records]


async def test_a_script_runs_to_the_end(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await run(connection, keeping, ScriptedModel(SCRIPT))
        assert lease is not None

        assert await counters(connection, lease) == {
            "model_requests_reserved": 3,
            "model_requests_dispatched": 3,
            "model_requests_settled": 3,
            "cost_known_micro_usd": 6_000,
            "input_tokens_actual": 600,
            "output_tokens_actual": 60,
        }
        assert await seen(connection, lease) == [f"log/checkout-api/{n}" for n in (1, 2, 3)]
        async with owner_transaction(connection, OWNER_A):
            kinds = [
                kind for _, kind, _, _ in await list_events(connection, lease.investigation_id)
            ]
        assert kinds[kinds.index("ATTEMPT_STARTED") + 1 :] == [
            "MODEL_CALL_DISPATCHED",
            "MODEL_CALL_SETTLED",
            "EVIDENCE_COMMITTED",
            "EVIDENCE_COMMITTED",
            "MODEL_CALL_DISPATCHED",
            "MODEL_CALL_SETTLED",
            "EVIDENCE_COMMITTED",
            "MODEL_CALL_DISPATCHED",
            "MODEL_CALL_SETTLED",
        ]


async def test_a_crash_after_dispatch_leaves_an_unknown_call_and_recovers(
    database: ScratchDatabase,
) -> None:
    model = ScriptedModel(SCRIPT, {1: Fault.CRASH_AFTER_DISPATCH})
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        crashed = await run(connection, keeping, model, "crashed")
        assert crashed is not None
        assert await calls(connection, crashed) == [(True, "SETTLED"), (True, "DISPATCHED")]

        await recover(connection, crashed)
        assert await calls(connection, crashed) == [(True, "SETTLED"), (True, "UNKNOWN")]

        # The same instance plays on: a planted fault strikes once.
        retry = await run(connection, keeping, model, "rescuer")
        assert retry is not None
        assert retry.epoch == crashed.epoch + 1
        assert [state for mine, state in await calls(connection, retry) if mine] == ["SETTLED"] * 3
        assert await counters(connection, retry) == {
            "model_requests_reserved": 5,
            "model_requests_dispatched": 5,
            "model_requests_settled": 4,
            "model_requests_unknown": 1,
            "cost_known_micro_usd": 7_000,
            "cost_unknown_micro_usd": ESTIMATE,
            "input_tokens_actual": 700,
            "output_tokens_actual": 70,
        }
        # The lost attempt's evidence was reused, not stored again.
        assert await seen(connection, retry) == [f"log/checkout-api/{n}" for n in (1, 2, 3)]


async def test_a_crash_before_dispatch_releases_the_reservation(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        model = ScriptedModel(SCRIPT, {0: Fault.CRASH_AFTER_RESERVE})
        crashed = await run(connection, keeping, model)
        assert crashed is not None
        assert await calls(connection, crashed) == [(True, "RESERVED")]

        await recover(connection, crashed)
        assert await calls(connection, crashed) == [(True, "FAILED")]
        assert await counters(connection, crashed) == {}
        assert await seen(connection, crashed) == []


async def test_a_provider_error_fails_the_step_and_the_script_goes_on(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        model = ScriptedModel(SCRIPT, {1: Fault.PROVIDER_ERROR})
        lease = await run(connection, keeping, model)
        assert lease is not None

        assert [state for _, state in await calls(connection, lease)] == [
            "SETTLED",
            "FAILED",
            "SETTLED",
        ]
        assert [failure for _, failure in await calls(connection, lease, "failure_category")] == [
            None,
            "PROVIDER_ERROR",
            None,
        ]
        assert await counters(connection, lease) == {
            "model_requests_reserved": 3,
            "model_requests_dispatched": 3,
            "model_requests_settled": 2,
            "cost_known_micro_usd": 4_000,
            "cost_unknown_micro_usd": ESTIMATE,
            "input_tokens_actual": 400,
            "output_tokens_actual": 40,
        }
        # The failed step's evidence was never committed.
        assert await seen(connection, lease) == ["log/checkout-api/1", "log/checkout-api/2"]


async def test_the_script_stops_when_the_budget_is_spent(
    database: ScratchDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="incidentpilot.workflows.scripted")
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await run(connection, keeping, ScriptedModel([step(1)] * 12))
        assert lease is not None
        assert len(await calls(connection, lease)) == 10
        investigation = lease.investigation_id
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "incidentpilot.workflows.scripted"
        ] == [
            f"script stopped on investigation {investigation}: investigation {investigation}"
            " can't reserve a model call: its budget is spent"
        ]
        # Stopping isn't a crash: the attempt still holds its lease for publication.
        assert await claim_next(connection, "other", OFFLINE) is None


async def test_nothing_starts_once_the_lease_is_lost(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await claim_next(connection, "w", OFFLINE)
        assert lease is not None
        keeper = LeaseKeeper(keeping, lease, timedelta(seconds=30), timedelta(seconds=10))
        keeper.lost.set()
        with pytest.raises(StaleLease):
            await ScriptedModel(SCRIPT)(connection, lease, keeper)
        assert await calls(connection, lease) == []


@pytest.mark.parametrize("fault", [Fault.CRASH_AFTER_RESERVE, Fault.CRASH_AFTER_DISPATCH])
async def test_a_planted_crash_names_itself(database: ScratchDatabase, fault: Fault) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        await queue(connection)
        lease = await claim_next(connection, "w", OFFLINE)
        assert lease is not None
        keeper = LeaseKeeper(keeping, lease, timedelta(seconds=30), timedelta(seconds=10))
        model = ScriptedModel(SCRIPT, {1: fault})
        with pytest.raises(Crash, match=rf"^planted {fault.name.lower()} at step 1$"):
            await model(connection, lease, keeper)
