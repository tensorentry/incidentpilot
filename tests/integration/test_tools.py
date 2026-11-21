"""The five P0 tools through the SDK and the gateway (spec §8.1; errata E13, E16, E20):
each asks its own source method with exactly the model's arguments, and the gateway
checks what the query reaches before anything is reserved."""

import json
from typing import Any

import pytest
from agents import Agent, Runner

from incidentpilot.adapters.fixture import FixtureTelemetry
from incidentpilot.application.investigations import start_investigation
from incidentpilot.application.snapshots import read_bundle
from incidentpilot.domain.enums import EvidenceKind, ToolErrorCode, ToolName, ToolStatus
from incidentpilot.domain.telemetry import (
    DependencyQuery,
    DeploymentsQuery,
    LogQuery,
    MetricsQuery,
    Query,
    SourceResult,
    TraceQuery,
)
from incidentpilot.domain.tools import ToolResponse
from incidentpilot.model_runtime.sdk import run_config
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import Lease
from incidentpilot.tools.catalog import TOOLS
from incidentpilot.tools.gateway import ToolGateway
from incidentpilot.workflows.claim import claim_next
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import (
    FIXTURE,
    OFFLINE,
    claimed,
    seed,
    start_request,
    update,
)
from tests.integration.test_gateway import gateway, rows
from tests.sdk_compat.fake import FakeModel, message, tool_call

INCIDENT = {"start_time": "2026-11-04T18:15:00Z", "end_time": "2026-11-04T18:45:00Z"}
# The whole time the snapshot observes, which holds the deployment and the baseline span.
OBSERVED = {"start_time": "2026-11-04T17:00:00Z", "end_time": "2026-11-04T19:00:00Z"}


class Recording(FixtureTelemetry):
    """The fixture source, noting which method was asked what."""

    def __init__(self) -> None:
        super().__init__(read_bundle(FIXTURE))
        self.asked: list[tuple[str, Query]] = []

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        self.asked.append(("get_metrics", query))
        return await super().get_metrics(query)

    async def search_logs(self, query: LogQuery) -> SourceResult:
        self.asked.append(("search_logs", query))
        return await super().search_logs(query)

    async def get_deployments(self, query: DeploymentsQuery) -> SourceResult:
        self.asked.append(("get_deployments", query))
        return await super().get_deployments(query)

    async def dependency_health(self, query: DependencyQuery) -> SourceResult:
        self.asked.append(("dependency_health", query))
        return await super().dependency_health(query)

    async def trace_summary(self, query: TraceQuery) -> SourceResult:
        self.asked.append(("trace_summary", query))
        return await super().trace_summary(query)


async def claimed_over(connection: Connection, times: dict[str, str]) -> Lease:
    """A claimed investigation whose frozen window is `times`."""
    seeded = await seed(connection, OWNER_A)
    window = {"start_at": times["start_time"], "end_at": times["end_time"]}
    await start_investigation(
        connection, OWNER_A, OFFLINE, seeded.incident_id, "wide", start_request(time_window=window)
    )
    lease = await claim_next(connection, "worker", OFFLINE)
    assert lease is not None
    return lease


async def run(
    tools: ToolGateway, *calls: tuple[str, dict[str, Any]], first: int = 1
) -> list[ToolResponse]:
    """One turn of tool calls. Call IDs start at `first`, so later turns don't reuse them."""
    turn = [tool_call(name, arguments, n) for n, (name, arguments) in enumerate(calls, first)]
    model = FakeModel([turn, [message("done")]])
    agent = Agent[ToolGateway](name="investigator", model=model, tools=list(TOOLS))
    result = await Runner.run(agent, "investigate", context=tools, run_config=run_config())
    outputs = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
    return [ToolResponse.model_validate_json(str(output)) for output in outputs]


async def tool_quotas(connection: Connection, lease: Lease) -> dict[str, tuple[int, int]]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            "SELECT tool_name, reserved, dispatched FROM investigation_tool_quota"
            " WHERE investigation_id = %s",
            (lease.investigation_id,),
        )
        return {
            name: (reserved, dispatched) for name, reserved, dispatched in await cursor.fetchall()
        }


def logs(**changes: Any) -> dict[str, Any]:
    fields = {
        "service": "payments",
        **INCIDENT,
        "min_severity": "error",
        "contains": None,
        "limit": 100,
        "cursor": None,
    }
    return fields | changes


def deployments(**changes: Any) -> dict[str, Any]:
    return {"service": "checkout-api", **OBSERVED, "limit": 100, "cursor": None} | changes


def dependency(**changes: Any) -> dict[str, Any]:
    fields = {
        "service": "checkout-api",
        "dependency": "inventory",
        **INCIDENT,
        "metrics": ["request_latency", "request_count"],
        "resolution_seconds": 60,
    }
    return fields | changes


def traces(**changes: Any) -> dict[str, Any]:
    return {"service": "checkout-api", **INCIDENT, "limit": 100, "cursor": None} | changes


def query(kind: type[Query], fields: dict[str, Any]) -> Query:
    fields = dict(fields)
    fields["window"] = {"start_at": fields.pop("start_time"), "end_at": fields.pop("end_time")}
    return kind.model_validate_json(json.dumps(fields))


CASES = [
    pytest.param(
        "search_logs",
        logs(),
        ("search_logs", query(LogQuery, logs())),
        EvidenceKind.LOG_RECORD,
        15,
        id="logs",
    ),
    pytest.param(
        "search_logs",
        logs(service="checkout-api", min_severity="info", contains="LATENCY BUDGET", limit=4),
        (
            "search_logs",
            query(
                LogQuery,
                logs(
                    service="checkout-api",
                    min_severity="info",
                    contains="LATENCY BUDGET",
                    limit=4,
                ),
            ),
        ),
        EvidenceKind.LOG_RECORD,
        4,
        id="filtered logs",
    ),
    pytest.param(
        "get_deployments",
        deployments(),
        ("get_deployments", query(DeploymentsQuery, deployments())),
        EvidenceKind.DEPLOYMENT_RECORD,
        1,
        id="deployments",
    ),
    pytest.param(
        "get_dependency_health",
        dependency(),
        ("dependency_health", query(DependencyQuery, dependency())),
        EvidenceKind.METRIC_SERIES_WINDOW,
        2,
        id="dependency",
    ),
    pytest.param(
        "get_trace_summary",
        traces(),
        ("trace_summary", query(TraceQuery, traces())),
        EvidenceKind.SPAN_SUMMARY,
        1,
        id="traces",
    ),
]


@pytest.mark.parametrize(("tool", "arguments", "asked", "kind", "count"), CASES)
async def test_each_tool_asks_its_own_source_and_commits_what_it_shows(
    database: ScratchDatabase,
    tool: str,
    arguments: dict[str, Any],
    asked: tuple[str, Query],
    kind: EvidenceKind,
    count: int,
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed_over(connection, OBSERVED)
        source = Recording()
        tools = await gateway(connection, keeping, lease, source)

        (response,) = await run(tools, (tool, arguments))

        assert source.asked == [asked]
        assert response.status is ToolStatus.OK
        assert [ref.kind for ref in response.evidence_refs] == [kind] * count
        async with owner_transaction(connection, OWNER_A):
            shown = {ref.evidence_id for ref in response.evidence_refs}
            visible = await visible_evidence(connection, lease.investigation_id)
            assert {row.evidence_id for row in visible} == shown
        assert await rows(connection, lease) == [(tool, "COMPLETED", "ok", None, True)]
        spent = {
            name: used
            for name, used in (await tool_quotas(connection, lease)).items()
            if used != (0, 0)
        }
        assert spent == {tool: (1, 1)}


async def test_the_filters_reach_the_records(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed_over(connection, OBSERVED)
        tools = await gateway(connection, keeping, lease)

        errors, warnings = await run(
            tools,
            ("search_logs", logs()),
            ("search_logs", logs(service="checkout-api", min_severity="warn")),
        )

        assert {record["content"]["severity"] for record in records(errors)} == {"error"}
        assert {record["content"]["service"] for record in records(warnings)} == {"checkout-api"}
        assert {record["content"]["severity"] for record in records(warnings)} == {"warn"}


def records(response: ToolResponse) -> list[dict[str, Any]]:
    assert isinstance(response.data, dict)
    found = response.data["records"]
    assert isinstance(found, list)
    return [record for record in found if isinstance(record, dict)]


async def test_a_page_continues_with_its_cursor(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed_over(connection, OBSERVED)
        tools = await gateway(connection, keeping, lease)

        (first,) = await run(tools, ("search_logs", logs(limit=10)))
        assert first.truncated
        assert first.next_cursor is not None
        (second,) = await run(
            tools, ("search_logs", logs(limit=10, cursor=first.next_cursor)), first=2
        )

        assert len(first.evidence_refs) == 10
        assert len(second.evidence_refs) == 5
        assert not second.truncated
        assert second.next_cursor is None
        assert not {ref.evidence_id for ref in first.evidence_refs} & {
            ref.evidence_id for ref in second.evidence_refs
        }
        assert (await tool_quotas(connection, lease))["search_logs"] == (2, 2)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_logs", logs(cursor="someone-elses")),
        ("get_deployments", deployments(cursor="someone-elses")),
        ("get_trace_summary", traces(cursor="someone-elses")),
    ],
    ids=["logs", "deployments", "traces"],
)
async def test_each_paged_tool_hands_its_cursor_to_the_source(
    database: ScratchDatabase, tool: str, arguments: dict[str, Any]
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed_over(connection, OBSERVED)
        source = Recording()
        tools = await gateway(connection, keeping, lease, source)

        (response,) = await run(tools, (tool, arguments))

        ((_, asked),) = source.asked
        assert getattr(asked, "cursor", None) == "someone-elses"
        assert response.error is not None
        assert response.error.code is ToolErrorCode.CURSOR_INVALID
        assert await rows(connection, lease) == [(tool, "FAILED", "error", "CURSOR_INVALID", True)]


async def test_a_window_with_no_traces_is_not_ok(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed_over(connection, OBSERVED)
        tools = await gateway(connection, keeping, lease)

        (response,) = await run(
            tools,
            (
                "get_trace_summary",
                traces(start_time="2026-11-04T17:00:00Z", end_time="2026-11-04T17:30:00Z"),
            ),
        )

        assert response.status is ToolStatus.PARTIAL
        assert response.evidence_refs == []
        assert response.coverage is not None
        assert not response.coverage.complete


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"dependency": "billing"}, "billing is outside this investigation's scope"),
        ({"service": "billing"}, "billing is outside this investigation's scope"),
        (
            {"service": "inventory", "dependency": "payments"},
            "payments isn't a dependency of inventory",
        ),
        (
            {"service": "inventory", "dependency": "checkout-api"},
            "checkout-api isn't a dependency of inventory",
        ),
        (
            {"start_time": "2026-11-04T18:00:00Z"},
            "the window must lie within 2026-11-04T18:15:00+00:00 to 2026-11-04T18:45:00+00:00",
        ),
    ],
    ids=["unknown dependency", "unknown caller", "no edge", "reversed edge", "window"],
)
async def test_a_dependency_outside_the_graph_costs_nothing(
    database: ScratchDatabase, changes: dict[str, Any], refusal: str
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        source = Recording()
        tools = await gateway(connection, keeping, lease, source)

        (response,) = await run(tools, ("get_dependency_health", dependency(**changes)))

        assert response.error is not None
        assert (response.error.code, response.error.message) == (
            ToolErrorCode.OUT_OF_SCOPE,
            refusal,
        )
        assert source.asked == []
        assert await rows(connection, lease) == []
        assert set((await tool_quotas(connection, lease)).values()) == {(0, 0)}


@pytest.mark.parametrize(
    ("tool", "arguments", "refusal"),
    [
        (
            "search_logs",
            logs(min_severity="loud"),
            "min_severity: Input should be 'debug', 'info', 'warn', 'error' or 'fatal'",
        ),
        ("search_logs", logs(contains=""), "contains: String should have at least 1 character"),
        ("search_logs", logs(limit=0), "limit: Input should be greater than or equal to 1"),
        ("get_deployments", deployments(limit=101), "limit: Input should be less than or equal"),
        ("get_deployments", deployments(cursor="no spaces"), "cursor: String should match"),
        ("get_trace_summary", traces(service="Checkout"), "service: String should match"),
        ("get_dependency_health", dependency(metrics=[]), "metrics: List should have at least 1"),
    ],
    ids=[
        "severity",
        "empty filter",
        "no limit",
        "big limit",
        "bad cursor",
        "bad name",
        "no metrics",
    ],
)
async def test_arguments_that_make_no_query_cost_nothing(
    database: ScratchDatabase, tool: str, arguments: dict[str, Any], refusal: str
) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        tools = await gateway(connection, keeping, lease)

        (response,) = await run(tools, (tool, arguments))

        assert response.error is not None
        assert response.error.code is ToolErrorCode.INVALID_ARGUMENT
        assert response.error.message.startswith(refusal)
        assert await rows(connection, lease) == []


async def test_each_tool_has_its_own_quota(database: ScratchDatabase) -> None:
    async with database.runtime() as connection, database.runtime() as keeping:
        lease = await claimed(connection)
        await update(
            connection,
            OWNER_A,
            "UPDATE investigation_tool_quota SET cap = 1"
            " WHERE investigation_id = %s AND tool_name = 'search_logs'",
            lease.investigation_id,
        )
        tools = await gateway(connection, keeping, lease)

        first, second, other = await run(
            tools,
            ("search_logs", logs()),
            ("search_logs", logs(min_severity="info")),
            ("get_dependency_health", dependency()),
        )

        assert first.status is ToolStatus.OK
        assert second.error is not None
        assert second.error.code is ToolErrorCode.QUOTA_EXCEEDED
        assert other.status is ToolStatus.OK


# The schemas the model sees


SCHEMAS = {
    ToolName.GET_SERVICE_METRICS: [
        "service",
        "start_time",
        "end_time",
        "metrics",
        "resolution_seconds",
    ],
    ToolName.SEARCH_LOGS: [
        "service",
        "start_time",
        "end_time",
        "min_severity",
        "contains",
        "limit",
        "cursor",
    ],
    ToolName.GET_DEPLOYMENTS: ["service", "start_time", "end_time", "limit", "cursor"],
    ToolName.GET_DEPENDENCY_HEALTH: [
        "service",
        "dependency",
        "start_time",
        "end_time",
        "metrics",
        "resolution_seconds",
    ],
    ToolName.GET_TRACE_SUMMARY: ["service", "start_time", "end_time", "limit", "cursor"],
}
TRUSTED = ("owner", "investigation", "attempt", "epoch", "snapshot", "budget", "lease")


def test_the_five_p0_tools_are_registered_and_nothing_else() -> None:
    assert [tool.name for tool in TOOLS] == list(ToolName)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda tool: tool.name)
def test_each_schema_takes_only_model_writable_arguments(tool: Any) -> None:
    schema = tool.params_json_schema

    assert list(schema["properties"]) == SCHEMAS[ToolName(tool.name)]
    assert schema["required"] == SCHEMAS[ToolName(tool.name)]
    assert schema["additionalProperties"] is False
    assert tool.strict_json_schema
    text = json.dumps(schema).casefold()
    assert [word for word in TRUSTED if word in text] == []
