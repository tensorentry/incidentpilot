"""The investigator runner against PostgreSQL (spec §9.1, D07, §11.3; E5): one agent run
reads the committed briefing, passes every request and tool call through the gates, and
ends with a candidate or a typed stop."""

import asyncio
import json
from typing import Any

import httpx2
import openai
import pytest
from agents import ModelResponse

from incidentpilot.domain.enums import StopReason
from incidentpilot.domain.report import ReportCandidate, Stop
from incidentpilot.model_runtime.briefing import load_briefing
from incidentpilot.model_runtime.pricing import Pricing
from incidentpilot.model_runtime.prompt import INSTRUCTIONS
from incidentpilot.model_runtime.runner import DETAILS, InvestigatorRunner, RunnerLimits
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.fence import Lease, StaleLease
from incidentpilot.persistence.ledger import BudgetDenied
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.scripted import abstain
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import claimed, update
from tests.integration.test_gateway import telemetry
from tests.sdk_compat.fake import FakeModel, Turn, message, refusal, tool_call

PRICING = Pricing("test-1", 2_000_000, 500_000, 2_500_000, 8_000_000)
LIMITS = RunnerLimits(max_turns=4, max_output_tokens=2_000, briefing_tokens=60_000)
CANDIDATE = abstain([])
METRICS = {
    "service": "checkout-api",
    "start_time": "2026-11-04T18:15:00Z",
    "end_time": "2026-11-04T18:20:00Z",
    "metrics": ["request_count"],
    "resolution_seconds": 60,
}
LOGS = {
    "service": "payments",
    "start_time": "2026-11-04T18:15:00Z",
    "end_time": "2026-11-04T18:45:00Z",
    "min_severity": "error",
    "contains": None,
    "limit": 10,
    "cursor": None,
}


class Provider(FakeModel):
    """A fake provider that keeps what each request carried."""

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__(turns)
        self.instructions: list[str | None] = []
        self.inputs: list[Any] = []
        self.outputs: list[Any] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.instructions.append(args[0])
        self.inputs.append(args[1])
        self.outputs.append(args[4])
        return await super().get_response(*args, **kwargs)


def without_clock(text: str) -> dict[str, Any]:
    """A briefing without its seconds left, which tick while the test runs."""
    briefing = json.loads(text)
    del briefing["remaining"]["seconds"]
    assert isinstance(briefing, dict)
    return briefing


def answer(candidate: ReportCandidate = CANDIDATE) -> Turn:
    return [message(candidate.model_dump_json())]


async def investigate(
    connection: Connection, keeping: Connection, lease: Lease, provider: FakeModel
) -> ReportCandidate | Stop:
    runner = InvestigatorRunner(provider, PRICING, telemetry(), LIMITS)
    return await runner.investigate(connection, lease, LeaseKeeper(keeping, lease))


async def ledger(connection: Connection, lease: Lease) -> tuple[list[str], list[str]]:
    async with owner_transaction(connection, lease.owner_id):
        model = await connection.execute(
            "SELECT state FROM model_calls WHERE investigation_id = %s ORDER BY created_at",
            (lease.investigation_id,),
        )
        tools = await connection.execute(
            "SELECT tool_name FROM tool_calls WHERE investigation_id = %s ORDER BY created_at",
            (lease.investigation_id,),
        )
        return [s for (s,) in await model.fetchall()], [t for (t,) in await tools.fetchall()]


async def test_a_run_reads_the_briefing_calls_tools_and_returns_the_candidate(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        briefing = await load_briefing(connection, lease, LIMITS.briefing_tokens)
        provider = Provider([[tool_call("get_service_metrics", METRICS, 1)], answer()])

        result = await investigate(connection, keeping, lease, provider)

        assert result == CANDIDATE
        assert provider.instructions == [INSTRUCTIONS, INSTRUCTIONS]
        ((sent,),) = [provider.inputs[0]]
        assert sent["role"] == "user"
        assert without_clock(sent["content"]) == without_clock(briefing.text)
        assert provider.outputs[0] is not None
        assert provider.outputs[0].json_schema()["title"] == "ReportCandidate"
        assert await ledger(connection, lease) == (
            ["SETTLED", "SETTLED"],
            ["get_service_metrics"],
        )


async def test_tool_calls_from_one_response_run_one_after_another(
    database: ScratchDatabase,
) -> None:
    later = METRICS | {"start_time": "2026-11-04T18:20:00Z", "end_time": "2026-11-04T18:25:00Z"}
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        provider = Provider(
            [
                [
                    tool_call("get_service_metrics", METRICS, 1),
                    tool_call("search_logs", LOGS, 2),
                    tool_call("get_service_metrics", later, 3),
                ],
                answer(),
            ]
        )

        result = await investigate(connection, keeping, lease, provider)

        assert result == CANDIDATE
        assert await ledger(connection, lease) == (
            ["SETTLED", "SETTLED"],
            ["get_service_metrics", "search_logs", "get_service_metrics"],
        )


def stop(reason: StopReason) -> Stop:
    return Stop(reason=reason, detail=DETAILS[reason])


@pytest.mark.parametrize(
    ("turns", "reason"),
    [
        ([[refusal("no")]], StopReason.MODEL_REFUSED),
        ([[message("not a report")]], StopReason.PROVIDER_FAILURE),
        (
            [
                openai.InternalServerError(
                    "down",
                    response=httpx2.Response(
                        500, request=httpx2.Request("POST", "https://provider.test")
                    ),
                    body=None,
                )
            ],
            StopReason.PROVIDER_FAILURE,
        ),
        (
            [
                [tool_call("get_service_metrics", METRICS | {"end_time": t}, n)]
                for n, t in [
                    (1, "2026-11-04T18:20:00Z"),
                    (2, "2026-11-04T18:21:00Z"),
                    (3, "2026-11-04T18:22:00Z"),
                    (4, "2026-11-04T18:23:00Z"),
                    (5, "2026-11-04T18:24:00Z"),
                ]
            ],
            StopReason.TURN_LIMIT_REACHED,
        ),
        ([BudgetDenied(OWNER_A, past_deadline=True)], StopReason.DEADLINE_REACHED),  # type: ignore[arg-type]
    ],
    ids=["refusal", "not the schema", "provider error", "turn limit", "past the deadline"],
)
async def test_each_ending_without_a_candidate_is_a_typed_stop(
    database: ScratchDatabase, turns: list[Turn], reason: StopReason
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)

        result = await investigate(connection, keeping, lease, Provider(turns))

        assert result == stop(reason)


async def test_a_spent_budget_stops_before_any_request(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET spend_cap_micro_usd = 1 WHERE id = %s",
            lease.investigation_id,
        )
        provider = Provider([answer()])

        result = await investigate(connection, keeping, lease, provider)

        assert result == stop(StopReason.BUDGET_EXHAUSTED)
        assert provider.requests == 0


async def test_no_run_starts_inside_the_finalization_reserve(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()"
            " + make_interval(secs => finalization_reserve_seconds - 1) WHERE id = %s",
            lease.investigation_id,
        )
        provider = Provider([answer()])

        result = await investigate(connection, keeping, lease, provider)

        assert result == stop(StopReason.FINALIZATION_RESERVE_REACHED)
        assert provider.requests == 0


class Slow(Provider):
    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(30)
        return await super().get_response(*args, **kwargs)


async def test_a_run_stops_when_only_the_reserve_is_left(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigations SET deadline_at = pg_catalog.clock_timestamp()"
            " + make_interval(secs => finalization_reserve_seconds + 1) WHERE id = %s",
            lease.investigation_id,
        )

        result = await asyncio.wait_for(
            investigate(connection, keeping, lease, Slow([answer()])), timeout=10
        )

        assert result == stop(StopReason.FINALIZATION_RESERVE_REACHED)
        # The request was sent and never answered; closing the attempt settles it (E9).
        assert await ledger(connection, lease) == (["DISPATCHED"], [])


@pytest.mark.parametrize(
    "error",
    [RuntimeError("client bug"), TimeoutError("a socket timed out")],
    ids=["bug", "timeout"],
)
async def test_failures_the_runtime_does_not_recognise_are_raised(
    database: ScratchDatabase, error: Exception
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)

        with pytest.raises(type(error)) as raised:
            await investigate(connection, keeping, lease, Provider([error]))

        assert raised.value is error


async def test_a_lost_lease_is_raised(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        keeper = LeaseKeeper(keeping, lease)
        keeper.lost.set()
        runner = InvestigatorRunner(Provider([answer()]), PRICING, telemetry(), LIMITS)

        with pytest.raises(StaleLease):
            await runner.investigate(connection, lease, keeper)
