"""Starting an investigation (spec §12.1, §12.2; errata E8, E11 and E19)."""

import asyncio
import re
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg import errors

from incidentpilot.application.errors import IdempotencyConflict, NotFound, Rejected, RequestError
from incidentpilot.application.incidents import create_incident
from incidentpilot.application.investigations import (
    ADMISSION_LOCK,
    OPERATION,
    fingerprint,
    start_investigation,
)
from incidentpilot.domain.enums import InvestigationStatus, RunMode
from incidentpilot.domain.ids import IncidentId, InvestigationId, OwnerId
from incidentpilot.domain.investigation import QUEUE_LIMIT, FrozenConfig, InvestigationLimits
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.investigations import (
    InvestigationIntegrityError,
    get_investigation,
    insert_investigation,
)
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase, still_blocked
from tests.integration.seeding import MANIFEST, incident_request, seed, start_request

OFFLINE = RunMode.OFFLINE_DEMO
KEY = "start-1"


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


async def counts(connection: Connection, owner: OwnerId) -> tuple[object, ...]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT (SELECT count(*) FROM investigations), (SELECT count(*) FROM run_events),"
            " (SELECT count(*) FROM idempotency_requests WHERE operation = 'start_investigation')"
        )
        row = await cursor.fetchone()
    assert row is not None
    return row


async def test_a_start_queues_the_investigation_with_its_frozen_config(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT seq, event_type, payload FROM run_events WHERE investigation_id = %s",
                (started.investigation.id,),
            )
            events = await cursor.fetchall()

    record = started.investigation
    assert started.created
    assert (record.status, record.incident_id, record.lease_epoch, record.next_event_seq) == (
        InvestigationStatus.QUEUED,
        seeded.incident_id,
        0,
        2,
    )
    assert record.current_attempt_id is None
    assert record.config.mode is RunMode.OFFLINE_DEMO
    assert record.config.window == MANIFEST.incident.window
    assert record.config.allowed_services == sorted(MANIFEST.service_graph.services)
    assert record.config.limits == InvestigationLimits()
    assert events == [
        (
            1,
            "INVESTIGATION_QUEUED",
            {
                "incident_id": str(seeded.incident_id),
                "snapshot_id": MANIFEST.snapshot_id,
                "mode": "offline_demo",
                "config_sha256": record.config.digest,
            },
        )
    ]


async def test_a_retry_returns_the_original_investigation(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        first = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        retry = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )

        assert (retry.investigation, retry.created) == (first.investigation, False)
        assert await counts(connection, OWNER_A) == (1, 1, 1)


async def test_the_same_key_with_a_different_request_conflicts(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        shorter = MANIFEST.incident.window.model_copy(
            update={"end_at": MANIFEST.incident.window.end_at - timedelta(minutes=5)}
        )
        with pytest.raises(IdempotencyConflict):
            await start_investigation(
                connection,
                OWNER_A,
                OFFLINE,
                seeded.incident_id,
                KEY,
                start_request(time_window=shorter.model_dump(mode="json")),
            )
        assert await counts(connection, OWNER_A) == (1, 1, 1)


async def test_an_incident_key_and_a_start_key_never_collide(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A, key=KEY)
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )

    assert started.created


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ("other owner's incident", NotFound, "incident {incident} doesn't exist"),
        ("missing snapshot", NotFound, "snapshot snap-000000000000 doesn't exist"),
        (
            "service outside the graph",
            RequestError,
            f"snapshot {MANIFEST.snapshot_id} has no service search",
        ),
        (
            "window outside the snapshot",
            RequestError,
            f"snapshot {MANIFEST.snapshot_id} doesn't cover that window",
        ),
    ],
)
async def test_a_request_that_fails_validation_leaves_nothing_behind(
    database: ScratchDatabase, change: str, error: type[Exception], message: str
) -> None:
    async with database.runtime() as connection:
        mine = await seed(connection, OWNER_A, service="search" if "graph" in change else None)
        incident = mine.incident_id
        request = start_request()
        if change == "other owner's incident":
            incident = (await seed(connection, OWNER_B, key="theirs")).incident_id
        elif change == "missing snapshot":
            request = start_request(snapshot_id="snap-000000000000")
        elif change == "window outside the snapshot":
            observed = MANIFEST.observation_window
            outside = {
                "start_at": (observed.end_at - timedelta(minutes=30)).isoformat(),
                "end_at": (observed.end_at + timedelta(minutes=1)).isoformat(),
            }
            request = start_request(time_window=outside)

        with pytest.raises(error, match=exactly(message.format(incident=incident))):
            await start_investigation(connection, OWNER_A, OFFLINE, incident, KEY, request)
        assert await counts(connection, OWNER_A) == (0, 0, 0)


async def queue(connection: Connection, owner: OwnerId, incident: IncidentId, n: int) -> None:
    for i in range(n):
        await start_investigation(
            connection, owner, OFFLINE, incident, f"fill-{i}", start_request()
        )


async def test_the_queue_limit_counts_every_owner(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        mine = await seed(connection, OWNER_A)
        theirs = await seed(connection, OWNER_B)
        await queue(connection, OWNER_A, mine.incident_id, QUEUE_LIMIT // 2)
        await queue(connection, OWNER_B, theirs.incident_id, QUEUE_LIMIT - QUEUE_LIMIT // 2)

        message = f"the queue already holds {QUEUE_LIMIT} investigations"
        with pytest.raises(Rejected, match=exactly(message)):
            await start_investigation(
                connection, OWNER_A, OFFLINE, mine.incident_id, KEY, start_request()
            )
        assert (await counts(connection, OWNER_A))[0] == QUEUE_LIMIT // 2


async def test_only_queued_work_counts_toward_the_limit(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        await queue(connection, OWNER_A, seeded.incident_id, QUEUE_LIMIT)
        async with owner_transaction(connection, OWNER_A):
            await connection.execute(
                "UPDATE investigations SET status = 'RETRY_WAIT'"
                " WHERE id = (SELECT id FROM investigations LIMIT 1)"
            )

        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        assert started.created


async def test_admission_waits_for_a_concurrent_start_and_counts_it(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as first, database.runtime() as second:
        seeded = await seed(first, OWNER_A)
        await queue(first, OWNER_A, seeded.incident_id, QUEUE_LIMIT - 1)
        async with owner_transaction(first, OWNER_A):
            # Admit the last place, as start_investigation does, but don't commit yet.
            await first.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
            await first.execute(
                "INSERT INTO investigations (owner_id, id, incident_id, snapshot_id, mode,"
                " model_profile, window_start, window_end, allowed_services, max_tool_calls,"
                " max_calls_per_tool, max_model_requests, max_recovery_attempts,"
                " deadline_seconds, finalization_reserve_seconds, spend_cap_micro_usd,"
                " config_sha256) SELECT owner_id, gen_random_uuid(), incident_id, snapshot_id,"
                " mode, model_profile, window_start, window_end, allowed_services,"
                " max_tool_calls, max_calls_per_tool, max_model_requests, max_recovery_attempts,"
                " deadline_seconds, finalization_reserve_seconds, spend_cap_micro_usd,"
                " config_sha256 FROM investigations LIMIT 1"
            )
            late = asyncio.create_task(
                start_investigation(
                    second, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
                )
            )
            assert await still_blocked(late)

        with pytest.raises(Rejected):
            await late
        assert (await counts(first, OWNER_A))[0] == QUEUE_LIMIT


async def test_racing_starts_never_exceed_the_limit(database: ScratchDatabase) -> None:
    async with database.runtime() as filler:
        seeded = await seed(filler, OWNER_A)
        await queue(filler, OWNER_A, seeded.incident_id, QUEUE_LIMIT - 2)
    async with database.runtime() as a, database.runtime() as b, database.runtime() as c:
        results = await asyncio.gather(
            *(
                start_investigation(
                    conn, OWNER_A, OFFLINE, seeded.incident_id, f"race-{i}", start_request()
                )
                for i, conn in enumerate((a, b, c))
            ),
            return_exceptions=True,
        )
        assert sorted(type(r).__name__ for r in results) == [
            "Rejected",
            "StartedInvestigation",
            "StartedInvestigation",
        ]
        assert (await counts(a, OWNER_A))[0] == QUEUE_LIMIT


async def test_another_owners_investigation_is_invisible(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        async with owner_transaction(connection, OWNER_B):
            assert await get_investigation(connection, started.investigation.id) is None


async def test_a_changed_stored_config_is_detected(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        investigation_id = started.investigation.id
        async with owner_transaction(connection, OWNER_A):
            await connection.execute("UPDATE investigations SET max_tool_calls = 14")
            message = (
                f"investigation {investigation_id} no longer matches the configuration it was"
                " admitted with"
            )
            with pytest.raises(InvestigationIntegrityError, match=exactly(message)):
                await get_investigation(connection, investigation_id)


async def test_an_investigation_cannot_point_at_another_owners_incident(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        started = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        theirs = await seed(connection, OWNER_B, key="theirs")
        with pytest.raises(errors.ForeignKeyViolation, match="investigations_incident_fkey"):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(
                    "UPDATE investigations SET incident_id = %s WHERE id = %s",
                    (theirs.incident_id, started.investigation.id),
                )


async def test_investigations_are_never_deleted(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        with pytest.raises(
            errors.InsufficientPrivilege, match=r"^permission denied for table investigations"
        ):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute("DELETE FROM investigations")


async def test_the_stored_fingerprint_is_the_documented_one(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT request_sha256 FROM idempotency_requests WHERE operation = %s", (OPERATION,)
            )
            assert await cursor.fetchone() == (
                fingerprint(OWNER_A, seeded.incident_id, start_request()),
            )


async def test_the_same_key_for_another_incident_conflicts(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        first = await seed(connection, OWNER_A, key="first")
        second = await create_incident(connection, OWNER_A, "second", incident_request())
        await start_investigation(
            connection, OWNER_A, OFFLINE, first.incident_id, KEY, start_request()
        )

        with pytest.raises(IdempotencyConflict):
            await start_investigation(
                connection, OWNER_A, OFFLINE, second.incident.id, KEY, start_request()
            )


async def test_every_stored_value_reads_back(database: ScratchDatabase) -> None:
    limits = InvestigationLimits(
        tool_calls=9,
        calls_per_tool=4,
        model_requests=7,
        recovery_attempts=1,
        deadline_seconds=120,
        finalization_reserve_seconds=11,
        spend_cap_micro_usd=250_000,
    )
    config = FrozenConfig(
        mode=RunMode.LIVE,
        model_profile="single-investigator-default",
        snapshot_id=MANIFEST.snapshot_id,
        window=MANIFEST.incident.window,
        allowed_services=["checkout-api"],
        limits=limits,
    )
    attempt = uuid4()
    async with database.runtime() as connection:
        seeded = await seed(connection, OWNER_A)
        parent = await start_investigation(
            connection, OWNER_A, OFFLINE, seeded.incident_id, KEY, start_request()
        )
        child_id = InvestigationId(uuid4())
        async with owner_transaction(connection, OWNER_A):
            await insert_investigation(connection, child_id, seeded.incident_id, config)
            await connection.execute(
                "UPDATE investigations SET parent_investigation_id = %s, current_attempt_id = %s,"
                " lease_epoch = 3, next_event_seq = 5 WHERE id = %s",
                (parent.investigation.id, attempt, child_id),
            )
            child = await get_investigation(connection, child_id)

    assert child is not None
    assert child.config == config
    assert (child.parent_investigation_id, child.current_attempt_id) == (
        parent.investigation.id,
        attempt,
    )
    assert (child.lease_epoch, child.next_event_seq) == (3, 5)
