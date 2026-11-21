"""The tool gateway against PostgreSQL (spec §8.4, D11; errata E9, E13 and E18): evidence
commits before the model sees an ID, and both quotas match the ledger rows."""

import json
from datetime import timedelta
from typing import Any
from uuid import uuid5

import pytest
from agents import Agent, Runner
from psycopg import errors

from incidentpilot.adapters.fixture import FixtureTelemetry, open_fixture
from incidentpilot.adapters.fixture_bundle import bundle_sha256
from incidentpilot.application.investigations import cancel_investigation
from incidentpilot.application.snapshots import read_bundle
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    AttemptStatus,
    EvidenceKind,
    FailureCategory,
    ToolErrorCode,
    ToolName,
    ToolStatus,
)
from incidentpilot.domain.evidence import MetricSeriesWindowSelector
from incidentpilot.domain.ids import InvocationId
from incidentpilot.domain.telemetry import (
    MetricsQuery,
    QueryRejected,
    SourceResult,
    TelemetryProvider,
)
from incidentpilot.domain.tools import ToolResponse
from incidentpilot.domain.window import TimeWindow
from incidentpilot.model_runtime.sdk import run_config
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.persistence.tool_ledger import (
    complete_tool_call,
    dispatch_tool_call,
    reserve_tool_call,
)
from incidentpilot.tools.catalog import TOOLS
from incidentpilot.tools.gateway import ToolGateway, load_scope
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.recovery import close_attempt, sweep_once
from incidentpilot.workflows.scripted import abstain
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import (
    FIXTURE,
    MANIFEST,
    OBSERVATIONS,
    OFFLINE,
    claimed,
    expire,
    update,
)
from tests.integration.test_evidence import CANARY, REDACTED
from tests.integration.test_publication import publish, submit
from tests.sdk_compat.fake import FakeModel, message, tool_call

INCIDENT = MANIFEST.incident.window
START = INCIDENT.start_at


def window(start: int = 0, end: int = 5) -> TimeWindow:
    return TimeWindow(
        start_at=START + timedelta(minutes=start), end_at=START + timedelta(minutes=end)
    )


def metrics_query(
    *names: str, service: str = "checkout-api", start: int = 0, end: int = 5
) -> MetricsQuery:
    return MetricsQuery(
        service=service,
        window=window(start, end),
        metrics=list(names or ["request_count"]),
        resolution_seconds=60,
    )


def telemetry() -> FixtureTelemetry:
    digest = bundle_sha256(read_bundle(FIXTURE))
    return open_fixture(OBSERVATIONS, MANIFEST.snapshot_id, digest)


async def gateway(
    connection: Connection,
    keeping: Connection,
    lease: Lease,
    source: TelemetryProvider | None = None,
) -> ToolGateway:
    keeper = LeaseKeeper(keeping, lease)
    scope = await load_scope(connection, lease)
    return ToolGateway(connection, lease, keeper, source or telemetry(), scope)


async def ask_metrics(
    tools: ToolGateway, query: MetricsQuery, call: str = "call-1"
) -> ToolResponse:
    return await tools.call(call, query)


async def rows(connection: Connection, lease: Lease) -> list[tuple[Any, ...]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT tool_name, state, result_status, error_code, dispatched_at IS NOT NULL"
            " FROM tool_calls WHERE investigation_id = %s ORDER BY created_at",
            (lease.investigation_id,),
        )
        return list(await cursor.fetchall())


async def quotas(connection: Connection, lease: Lease) -> tuple[Any, ...]:
    """(global reserved, global dispatched, metrics reserved, metrics dispatched)."""
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT i.tool_calls_reserved, i.tool_calls_dispatched, q.reserved, q.dispatched"
            " FROM investigations AS i JOIN investigation_tool_quota AS q"
            " ON q.investigation_id = i.id AND q.tool_name = 'get_service_metrics'"
            " WHERE i.id = %s",
            (lease.investigation_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return tuple(row)


async def tool_events(connection: Connection, lease: Lease) -> list[tuple[str, dict[str, Any]]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT event_type, payload FROM run_events WHERE investigation_id = %s"
            " AND (event_type LIKE 'TOOL_CALL_%%' OR event_type = 'EVIDENCE_COMMITTED')"
            " ORDER BY seq",
            (lease.investigation_id,),
        )
        return list(await cursor.fetchall())


async def linked(connection: Connection, lease: Lease) -> list[Any]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT tool_evidence.evidence_id FROM tool_evidence"
            " JOIN tool_calls ON tool_calls.id = tool_evidence.tool_call_id"
            " WHERE tool_calls.investigation_id = %s ORDER BY 1",
            (lease.investigation_id,),
        )
        return [evidence_id for (evidence_id,) in await cursor.fetchall()]


async def steal(connection: Connection, lease: Lease) -> None:
    """Another worker's sweeper takes the investigation away from this lease."""
    await expire(connection, lease)
    assert await sweep_once(connection) is not None


async def test_each_tool_starts_with_its_own_quota(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT tool_name, cap, reserved, dispatched FROM investigation_tool_quota"
                " WHERE investigation_id = %s ORDER BY tool_name",
                (lease.investigation_id,),
            )
            quota = await cursor.fetchall()

    assert quota == sorted((tool.value, 6, 0, 0) for tool in ToolName)


async def test_a_call_commits_its_evidence_before_answering(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)

        response = await ask_metrics(tools, metrics_query("request_count", "error_count"))

        assert response.status is ToolStatus.OK
        assert response.invocation_id == tools.invocation("call-1")
        assert response.source is not None
        assert (response.source.adapter, response.source.version) == ("fixture", "fixture-v1")
        assert response.source.snapshot_id == MANIFEST.snapshot_id
        async with owner_transaction(connection, OWNER_A):
            visible = await visible_evidence(connection, lease.investigation_id)
        by_id = {record.evidence_id: record for record in visible}
        assert [ref.evidence_id for ref in response.evidence_refs] == list(by_id)
        assert response.evidence_refs == [
            MetricSeriesWindowSelector(
                kind=EvidenceKind.METRIC_SERIES_WINDOW, evidence_id=evidence_id, window=window()
            )
            for evidence_id in by_id
        ]
        assert response.data is not None
        assert response.data["records"] == [
            {"evidence": ref.model_dump(mode="json"), "content": by_id[ref.evidence_id].content}
            for ref in response.evidence_refs
        ]
        assert await linked(connection, lease) == sorted(by_id)
        assert await rows(connection, lease) == [
            ("get_service_metrics", "COMPLETED", "ok", None, True)
        ]
        assert await quotas(connection, lease) == (1, 1, 1, 1)


async def only_call_id(database: ScratchDatabase, lease: Lease) -> Any:
    async with database.runtime() as connection, owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT id FROM tool_calls WHERE investigation_id = %s", (lease.investigation_id,)
        )
        (call_id,) = await cursor.fetchone() or (None,)
        return call_id


async def test_a_call_records_what_it_was_asked(database: ScratchDatabase) -> None:
    query = metrics_query("request_count", "error_count")
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await ask_metrics(await gateway(connection, keeping, lease), query, call="model-7")
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT arguments_sha256, provider_call_id, invocation_id, attempt_id"
                " FROM tool_calls WHERE investigation_id = %s",
                (lease.investigation_id,),
            )
            row = await cursor.fetchone()

    assert row == (
        sha256_digest(query.model_dump()),
        "model-7",
        uuid5(lease.attempt_id, "model-7"),
        lease.attempt_id,
    )


async def test_a_call_leaves_a_trail_of_events(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        response = await ask_metrics(await gateway(connection, keeping, lease), metrics_query())
        events = await tool_events(connection, lease)

    (ref,) = response.evidence_refs
    call_id = str(await only_call_id(database, lease))
    assert events == [
        ("TOOL_CALL_DISPATCHED", {"tool_call_id": call_id, "tool_name": "get_service_metrics"}),
        (
            "EVIDENCE_COMMITTED",
            {"evidence_id": str(ref.evidence_id), "kind": "metric_series_window", "reused": False},
        ),
        (
            "TOOL_CALL_COMPLETED",
            {"tool_call_id": call_id, "tool_name": "get_service_metrics", "status": "ok"},
        ),
    ]


class Leaky(FixtureTelemetry):
    """A source whose record holds a secret."""

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        answer = await super().get_metrics(query)
        (record,) = answer.records
        leaked = record.model_copy(update={"content": {**record.content, "note": f"key {CANARY}"}})
        return SourceResult((leaked,), answer.coverage, answer.truncated, answer.next_cursor)


async def test_the_model_sees_only_the_sanitized_content(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        leaky = Leaky(read_bundle(FIXTURE))
        tools = ToolGateway(
            connection,
            lease,
            LeaseKeeper(keeping, lease),
            leaky,
            await load_scope(connection, lease),
        )

        response = await ask_metrics(tools, metrics_query())

    shown = response.model_dump_json()
    assert CANARY not in shown
    (record,) = json.loads(shown)["data"]["records"]
    assert record["content"]["note"] == f"key {REDACTED}"


async def test_a_recovery_attempt_gives_the_same_call_a_new_invocation(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        first = await gateway(connection, keeping, lease)
        await steal(connection, lease)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET eligible_at = pg_catalog.clock_timestamp() WHERE id = %s",
            lease.investigation_id,
        )
        retry = await claim_next(connection, "rescuer", OFFLINE)
        assert retry is not None
        second = await gateway(connection, keeping, retry)

        response = await ask_metrics(second, metrics_query(), call="call-1")

    assert first.invocation("call-1") == uuid5(lease.attempt_id, "call-1")
    assert response.invocation_id == uuid5(retry.attempt_id, "call-1")
    assert response.invocation_id != first.invocation("call-1")


class Silent(FixtureTelemetry):
    """A source that holds nothing for the query, and says so with full coverage."""

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        answer = await super().get_metrics(query)
        return SourceResult((), answer.coverage, truncated=False, next_cursor=None)


class Wordy(FixtureTelemetry):
    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        raise QueryRejected(ToolErrorCode.SOURCE_UNAVAILABLE, "x" * 600)


class Paged(FixtureTelemetry):
    """A source with more to give than one page holds."""

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        answer = await super().get_metrics(query)
        return SourceResult(answer.records, answer.coverage, truncated=True, next_cursor="page-2")


async def answer_from(
    database: ScratchDatabase, source: FixtureTelemetry
) -> tuple[Lease, ToolResponse, list[Any]]:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        scope = await load_scope(connection, lease)
        tools = ToolGateway(connection, lease, LeaseKeeper(keeping, lease), source, scope)
        response = await ask_metrics(tools, metrics_query())
        return lease, response, await rows(connection, lease)


async def test_a_complete_answer_with_nothing_in_it_is_empty(database: ScratchDatabase) -> None:
    _, response, recorded = await answer_from(database, Silent(read_bundle(FIXTURE)))

    assert (response.status, response.evidence_refs) == (ToolStatus.EMPTY, [])
    assert recorded == [("get_service_metrics", "COMPLETED", "empty", None, True)]


async def test_a_long_refusal_is_cut_to_fit_the_envelope(database: ScratchDatabase) -> None:
    _, response, recorded = await answer_from(database, Wordy(read_bundle(FIXTURE)))

    assert response.error is not None
    assert response.error.message == "x" * 500
    assert response.error.retryable is True
    assert recorded == [("get_service_metrics", "FAILED", "error", "SOURCE_UNAVAILABLE", True)]


async def test_a_page_passes_its_cursor_to_the_model(database: ScratchDatabase) -> None:
    _, response, _ = await answer_from(database, Paged(read_bundle(FIXTURE)))

    assert (response.status, response.truncated, response.next_cursor) == (
        ToolStatus.OK,
        True,
        "page-2",
    )


async def test_coverage_with_gaps_is_a_partial_answer(database: ScratchDatabase) -> None:
    # The snapshot ends at 19:00, so a window past it has no points there.
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET window_end = %s WHERE id = %s",
            MANIFEST.observation_window.end_at + timedelta(minutes=10),
            lease.investigation_id,
        )
        tools = await gateway(connection, keeping, lease)
        query = MetricsQuery(
            service="checkout-api",
            window=TimeWindow(
                start_at=MANIFEST.observation_window.end_at - timedelta(minutes=5),
                end_at=MANIFEST.observation_window.end_at + timedelta(minutes=5),
            ),
            metrics=["request_count"],
            resolution_seconds=60,
        )

        response = await ask_metrics(tools, query)

        assert response.status is ToolStatus.PARTIAL
        assert response.coverage is not None
        assert response.coverage.fraction == 0.5
        assert await rows(connection, lease) == [
            ("get_service_metrics", "COMPLETED", "partial", None, True)
        ]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (metrics_query(service="billing"), "billing is outside this investigation's scope"),
        (
            metrics_query(start=-30, end=-25),
            "the window must lie within 2026-11-04T18:15:00+00:00 to 2026-11-04T18:45:00+00:00",
        ),
        (
            metrics_query(start=25, end=35),
            "the window must lie within 2026-11-04T18:15:00+00:00 to 2026-11-04T18:45:00+00:00",
        ),
    ],
    ids=["service", "before", "after"],
)
async def test_a_query_outside_the_scope_costs_nothing(
    database: ScratchDatabase, query: MetricsQuery, message: str
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        response = await ask_metrics(await gateway(connection, keeping, lease), query)

        assert response.error is not None
        assert (response.error.code, response.error.message) == (
            ToolErrorCode.OUT_OF_SCOPE,
            message,
        )
        assert await rows(connection, lease) == []
        assert await quotas(connection, lease) == (0, 0, 0, 0)


async def test_a_query_the_source_refuses_fails_and_keeps_its_quota(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        response = await ask_metrics(
            await gateway(connection, keeping, lease), metrics_query("cpu_seconds")
        )

        assert response.status is ToolStatus.ERROR
        assert response.error is not None
        assert response.error.code is ToolErrorCode.INVALID_ARGUMENT
        assert response.error.message.startswith("checkout-api records no cpu_seconds.")
        assert await rows(connection, lease) == [
            ("get_service_metrics", "FAILED", "error", "INVALID_ARGUMENT", True)
        ]
        # It was sent, so it counts (E18).
        assert await quotas(connection, lease) == (1, 1, 1, 1)
        assert [event for event, _ in await tool_events(connection, lease)] == [
            "TOOL_CALL_DISPATCHED",
            "TOOL_CALL_FAILED",
        ]


async def test_a_spent_tool_quota_refuses_without_a_row(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigation_tool_quota SET cap = 1 WHERE investigation_id = %s",
            lease.investigation_id,
        )
        tools = await gateway(connection, keeping, lease)
        await ask_metrics(tools, metrics_query(), call="first")

        response = await ask_metrics(tools, metrics_query(), call="second")

        assert response.error is not None
        assert response.error.code is ToolErrorCode.QUOTA_EXCEEDED
        assert response.error.message == (
            f"investigation {lease.investigation_id} has spent its get_service_metrics quota"
        )
        assert len(await rows(connection, lease)) == 1
        # The global reservation the denied call took was rolled back with it.
        assert await quotas(connection, lease) == (1, 1, 1, 1)


async def test_a_spent_global_quota_refuses_without_a_row(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET tool_calls_reserved = max_tool_calls WHERE id = %s",
            lease.investigation_id,
        )

        response = await ask_metrics(await gateway(connection, keeping, lease), metrics_query())

        assert response.error is not None
        assert response.error.code is ToolErrorCode.QUOTA_EXCEEDED
        assert response.error.message == (
            f"investigation {lease.investigation_id} has spent its tool quota"
        )
        assert await rows(connection, lease) == []
        assert await quotas(connection, lease) == (15, 0, 0, 0)


async def test_a_lost_lease_reserves_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)
        await steal(connection, lease)

        with pytest.raises(StaleLease):
            await ask_metrics(tools, metrics_query())

        assert await rows(connection, lease) == []
        assert await quotas(connection, lease) == (0, 0, 0, 0)


async def test_a_lease_the_keeper_lost_stops_the_call_before_it_reserves(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        keeper = LeaseKeeper(keeping, lease)
        keeper.lost.set()
        tools = ToolGateway(
            connection, lease, keeper, telemetry(), await load_scope(connection, lease)
        )

        with pytest.raises(StaleLease):
            await ask_metrics(tools, metrics_query())

        assert await rows(connection, lease) == []


class Stealing(FixtureTelemetry):
    """A source that loses the lease while it answers, then answers anyway."""

    def __init__(self, connection: Connection, lease: Lease) -> None:
        super().__init__(read_bundle(FIXTURE))
        self.connection = connection
        self.lease = lease

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        await steal(self.connection, self.lease)
        return await super().get_metrics(query)


class Crashing(FixtureTelemetry):
    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        raise RuntimeError("adapter bug")


async def test_a_lease_lost_while_the_source_answers_shows_no_evidence(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        source = Stealing(connection, lease)
        tools = await gateway(connection, keeping, lease, source)

        with pytest.raises(StaleLease):
            await ask_metrics(tools, metrics_query())

        async with owner_transaction(connection, OWNER_A):
            assert await visible_evidence(connection, lease.investigation_id) == []
        # The sweeper closed the attempt while the call was out: sent, so UNKNOWN.
        assert await rows(connection, lease) == [
            ("get_service_metrics", "UNKNOWN", None, None, True)
        ]
        assert await quotas(connection, lease) == (1, 1, 1, 1)


async def test_a_source_that_crashes_raises_and_leaves_the_call_sent(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease, Crashing(read_bundle(FIXTURE)))

        with pytest.raises(RuntimeError, match=r"^adapter bug$"):
            await ask_metrics(tools, metrics_query())
        assert await rows(connection, lease) == [
            ("get_service_metrics", "DISPATCHED", None, None, True)
        ]

        await steal(connection, lease)
        assert await rows(connection, lease) == [
            ("get_service_metrics", "UNKNOWN", None, None, True)
        ]
        assert [event for event, _ in await tool_events(connection, lease)] == [
            "TOOL_CALL_DISPATCHED",
            "TOOL_CALL_UNKNOWN",
        ]


async def reserve_only(connection: Connection, lease: Lease) -> None:
    async with fenced_transaction(connection, lease):
        await reserve_tool_call(
            connection,
            lease,
            ToolName.GET_SERVICE_METRICS,
            InvocationId(lease.attempt_id),
            "call-1",
            "0" * 64,
        )


async def test_an_unsent_call_fails_and_releases_when_its_attempt_is_lost(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)
        assert await quotas(connection, lease) == (1, 0, 1, 0)

        await steal(connection, lease)

        assert await rows(connection, lease) == [
            ("get_service_metrics", "FAILED", "error", "STALE_LEASE", False)
        ]
        assert await quotas(connection, lease) == (0, 0, 0, 0)
        (failed,) = [payload for event, payload in await tool_events(connection, lease)]
        assert (failed["error_code"], failed["sent"]) == ("STALE_LEASE", False)


async def test_an_unsent_call_fails_as_internal_when_its_attempt_fails(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)
        async with owner_transaction(connection, OWNER_A):
            await close_attempt(
                connection, lease.attempt_id, AttemptStatus.FAILED, FailureCategory.TELEMETRY_ERROR
            )

        assert await rows(connection, lease) == [
            ("get_service_metrics", "FAILED", "error", "INTERNAL_ERROR", False)
        ]


async def test_a_call_of_another_attempt_cannot_be_moved(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        other = await claimed(connection, tag="other")
        await reserve_only(connection, lease)
        call_id = await only_call_id(database, lease)

        with pytest.raises(
            LookupError, match=f"^tool call {call_id} doesn't belong to attempt {other.attempt_id}$"
        ):
            async with fenced_transaction(connection, other):
                await dispatch_tool_call(connection, other, call_id)


async def test_a_completed_call_must_have_answered(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)
        call_id = await only_call_id(database, lease)

        with pytest.raises(
            ValueError, match=r"^a completed call answered ok, empty or partial, not error$"
        ):
            async with fenced_transaction(connection, lease):
                await complete_tool_call(connection, lease, call_id, ToolStatus.ERROR, [])


async def test_an_unsent_call_fails_as_cancelled_when_the_investigation_is(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)

        await cancel_investigation(connection, OWNER_A, lease.investigation_id)

        assert await rows(connection, lease) == [
            ("get_service_metrics", "FAILED", "error", "CANCELLED", False)
        ]
        assert await quotas(connection, lease) == (0, 0, 0, 0)


async def test_one_invocation_is_one_row_per_attempt(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)

        with pytest.raises(errors.UniqueViolation):
            await reserve_only(connection, lease)

        assert await quotas(connection, lease) == (1, 0, 1, 0)


async def test_the_schema_refuses_an_inconsistent_tool_call(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)
        for change in (
            "state = 'COMPLETED'",
            "state = 'FAILED'",
            "result_status = 'ok'",
            "error_code = 'TIMEOUT'",
            "dispatched_at = pg_catalog.now()",
        ):
            with pytest.raises(errors.CheckViolation):
                await update(
                    connection,
                    OWNER_A,
                    f"UPDATE tool_calls SET {change} WHERE investigation_id = %s",  # noqa: S608
                    lease.investigation_id,
                )


async def test_the_schema_holds_the_quota_to_its_cap(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        for change in (
            "UPDATE investigation_tool_quota SET reserved = cap + 1 WHERE investigation_id = %s",
            "UPDATE investigation_tool_quota SET dispatched = 1 WHERE investigation_id = %s",
            "UPDATE investigations SET tool_calls_dispatched = 1 WHERE id = %s",
            "UPDATE investigations SET tool_calls_reserved = max_tool_calls + 1 WHERE id = %s",
        ):
            with pytest.raises(errors.CheckViolation):
                await update(connection, OWNER_A, change, lease.investigation_id)


async def test_a_report_counts_the_calls_that_completed(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)
        await ask_metrics(tools, metrics_query(), call="answered")
        await ask_metrics(tools, metrics_query("cpu_seconds"), call="refused")
        await ask_metrics(tools, metrics_query(service="billing"), call="out-of-scope")
        async with owner_transaction(connection, OWNER_A):
            evidence = await visible_evidence(connection, lease.investigation_id)

        async with fenced_transaction(connection, lease):
            candidate_id = await submit(connection, lease, abstain(evidence))
        published = await publish(connection, lease, candidate_id)

    assert published.report.execution.usage.tool_calls_completed == 1


# Through the SDK


async def run_tool(tools: ToolGateway, arguments: dict[str, Any]) -> tuple[ToolResponse, FakeModel]:
    model = FakeModel([[tool_call("get_service_metrics", arguments, 1)], [message("done")]])
    agent = Agent[ToolGateway](name="investigator", model=model, tools=list(TOOLS))
    result = await Runner.run(agent, "investigate", context=tools, run_config=run_config())
    (output,) = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
    return ToolResponse.model_validate_json(str(output)), model


def arguments(**changes: Any) -> dict[str, Any]:
    fields = {
        "service": "checkout-api",
        "start_time": "2026-11-04T18:15:00Z",
        "end_time": "2026-11-04T18:20:00Z",
        "metrics": ["request_latency"],
        "resolution_seconds": 60,
    }
    return fields | changes


async def test_the_sdk_tool_runs_through_the_gateway(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)

        response, model = await run_tool(tools, arguments())

        assert response.status is ToolStatus.OK
        assert response.invocation_id == tools.invocation("call-1")
        assert len(response.evidence_refs) == 1
        assert model.requests == 2
        assert await rows(connection, lease) == [
            ("get_service_metrics", "COMPLETED", "ok", None, True)
        ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"end_time": "2026-11-04T18:10:00Z"},
            "window: Value error, window must end after it starts",
        ),
        ({"start_time": "yesterday"}, "window.start_at: Input should be a valid datetime"),
        ({"metrics": []}, "metrics: List should have at least 1 item after validation, not 0"),
        ({"service": "Checkout API"}, "service: String should match pattern"),
    ],
    ids=["backwards", "not a time", "no metrics", "bad name"],
)
async def test_arguments_that_make_no_query_are_refused_for_free(
    database: ScratchDatabase, changes: dict[str, Any], message: str
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)

        response, _ = await run_tool(tools, arguments(**changes))

        assert response.error is not None
        assert response.error.code is ToolErrorCode.INVALID_ARGUMENT
        assert response.error.message.startswith(message)
        assert await rows(connection, lease) == []
        assert await quotas(connection, lease) == (0, 0, 0, 0)
