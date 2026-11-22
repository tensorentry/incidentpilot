"""The briefing read back from PostgreSQL (spec §6.3, §9.1): what committed is all a
recovery attempt needs, and what the budget has spent shows as spent."""

import json
from datetime import timedelta

from incidentpilot.domain.enums import EvidenceKind
from incidentpilot.model_runtime.briefing import load_briefing
from incidentpilot.persistence.database import owner_transaction
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import fenced_transaction
from incidentpilot.persistence.ledger import reserve_call
from incidentpilot.workflows.claim import claim_next
from incidentpilot.workflows.recovery import sweep_once
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import MANIFEST, OFFLINE, claimed, expire, update
from tests.integration.test_gateway import ask_metrics, gateway, metrics_query, reserve_only

BUDGET = 60_000


async def test_a_fresh_investigation_is_briefed_with_its_whole_budget(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)

        briefing = await load_briefing(connection, lease, BUDGET)

        brief = json.loads(briefing.text)
        assert brief["incident"]["service"] == MANIFEST.incident.service
        assert brief["incident"]["description"] == MANIFEST.incident.summary
        assert brief["scope"]["services"] == sorted(MANIFEST.service_graph.services)
        assert brief["scope"]["dependencies"] == [
            [edge.caller, edge.callee] for edge in MANIFEST.service_graph.dependencies
        ]
        assert brief["remaining"]["tool_calls"] == 15
        assert set(brief["remaining"]["calls_per_tool"].values()) == {6}
        assert len(brief["remaining"]["calls_per_tool"]) == 5
        assert brief["remaining"]["model_requests"] == 10
        assert 0 < brief["remaining"]["seconds"] <= 180
        assert brief["evidence"] == []


async def test_spent_budget_and_committed_evidence_show_up(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)
        await ask_metrics(tools, metrics_query("request_count", "error_count"))
        async with fenced_transaction(connection, lease):
            await reserve_call(connection, lease, 1_000)

        briefing = await load_briefing(connection, lease, BUDGET)

        brief = json.loads(briefing.text)
        assert brief["remaining"]["tool_calls"] == 14
        assert brief["remaining"]["calls_per_tool"]["get_service_metrics"] == 5
        assert brief["remaining"]["calls_per_tool"]["search_logs"] == 6
        assert brief["remaining"]["model_requests"] == 9
        assert [item["cite"]["kind"] for item in brief["evidence"]] == [
            "metric_series_window",
            "metric_series_window",
        ]
        async with owner_transaction(connection, OWNER_A):
            visible = await visible_evidence(connection, lease.investigation_id)
        assert briefing.shown == tuple(record.evidence_id for record in visible)


async def test_reserved_calls_count_as_spent_before_they_are_sent(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await reserve_only(connection, lease)
        async with fenced_transaction(connection, lease):
            await reserve_call(connection, lease, 1_000)

        brief = json.loads((await load_briefing(connection, lease, BUDGET)).text)

        assert brief["remaining"]["tool_calls"] == 14
        assert brief["remaining"]["calls_per_tool"]["get_service_metrics"] == 5
        assert brief["remaining"]["model_requests"] == 9


async def test_a_passed_deadline_leaves_no_seconds(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()"
            " - interval '1 minute' WHERE id = %s",
            lease.investigation_id,
        )

        brief = json.loads((await load_briefing(connection, lease, BUDGET)).text)

        assert brief["remaining"]["seconds"] == 0


async def test_a_recovery_attempt_is_briefed_with_what_committed_before_it(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await ask_metrics(await gateway(connection, keeping, lease), metrics_query())
        before = await load_briefing(connection, lease, BUDGET)
        await expire(connection, lease)
        assert await sweep_once(connection, backoff=timedelta(0)) is not None

        recovered = await claim_next(connection, "second-worker", OFFLINE)

        assert recovered is not None
        assert recovered.attempt_id != lease.attempt_id
        after = await load_briefing(connection, recovered, BUDGET)
        assert after.shown == before.shown
        assert json.loads(after.text)["evidence"] == json.loads(before.text)["evidence"]


async def test_a_small_budget_leaves_evidence_out_and_says_so(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await ask_metrics(
            await gateway(connection, keeping, lease),
            metrics_query("request_count", "error_count", "request_latency"),
        )
        full = await load_briefing(connection, lease, BUDGET)

        smaller = await load_briefing(connection, lease, len(full.text.encode()) - 1)

        assert len(smaller.shown) < len(full.shown)
        assert smaller.omitted == {
            EvidenceKind.METRIC_SERIES_WINDOW: len(full.shown) - len(smaller.shown)
        }
