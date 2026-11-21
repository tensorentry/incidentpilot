"""The tool gateway: every tool call passes through here (spec §8.1, §8.4, D11).

For each call, in order:
1. The query is checked against the investigation's frozen scope: its services, the
   dependencies between them, and its window. A refusal costs no quota.
2. The global and per-tool quotas are reserved, behind the lease fence.
3. DISPATCHED commits, behind the fence, before the source is asked.
4. The source answers, or refuses the query.
5. The evidence, its visibility and the call's completion commit together, behind the
   fence.
6. Only then does the model see the result, with selectors for evidence that now
   exists.

A lost lease raises StaleLease at whichever fenced step finds it, so the model never
sees an evidence ID that didn't commit. Any other failure of the source also raises:
the attempt then closes and leaves the sent call UNKNOWN (E9).

The query decides everything else: which tool it is, which source method answers it,
and what it reaches. A tool passes only the query, so it can't name one service to the
scope check and ask the source about another.
"""

import json
from dataclasses import dataclass
from typing import Final
from uuid import uuid5

from pydantic import JsonValue

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import EvidenceKind, ToolErrorCode, ToolName, ToolStatus
from incidentpilot.domain.evidence import EVIDENCE_SELECTOR_ADAPTER, EvidenceDraft, EvidenceSelector
from incidentpilot.domain.ids import EvidenceId, InvocationId, SnapshotId
from incidentpilot.domain.snapshot import ServiceGraph
from incidentpilot.domain.telemetry import (
    DependencyQuery,
    DeploymentsQuery,
    LogQuery,
    MetricsQuery,
    Query,
    QueryRejected,
    SourceResult,
    TelemetryProvider,
    TraceQuery,
)
from incidentpilot.domain.tools import ToolError, ToolResponse, ToolSource
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.evidence import commit_evidence
from incidentpilot.persistence.fence import Lease, fenced_transaction
from incidentpilot.persistence.tool_ledger import (
    QuotaDenied,
    complete_tool_call,
    dispatch_tool_call,
    fail_tool_call,
    reserve_tool_call,
)
from incidentpilot.workflows.lease import LeaseKeeper

_SCOPE = (
    "SELECT i.snapshot_id, i.allowed_services, i.window_start, i.window_end,"
    " s.manifest -> 'service_graph'"
    " FROM investigations AS i"
    " JOIN snapshots AS s ON s.owner_id = i.owner_id AND s.id = i.snapshot_id"
    " WHERE i.id = %s"
)

_TOOL: Final[dict[type[Query], ToolName]] = {
    MetricsQuery: ToolName.GET_SERVICE_METRICS,
    LogQuery: ToolName.SEARCH_LOGS,
    DeploymentsQuery: ToolName.GET_DEPLOYMENTS,
    DependencyQuery: ToolName.GET_DEPENDENCY_HEALTH,
    TraceQuery: ToolName.GET_TRACE_SUMMARY,
}


@dataclass(frozen=True)
class Scope:
    """What an investigation may query, fixed when it started (§8.3)."""

    snapshot_id: SnapshotId
    services: frozenset[str]
    graph: ServiceGraph
    window: TimeWindow


async def load_scope(connection: Connection, lease: Lease) -> Scope:
    async with owner_transaction(connection, lease.owner_id):
        row = await (await connection.execute(_SCOPE, (lease.investigation_id,))).fetchone()
    assert row is not None  # noqa: S101 (a lease's investigation always exists)
    snapshot_id, services, start_at, end_at, graph = row
    return Scope(
        snapshot_id,
        frozenset(services),
        # Through JSON, so strict validation reads it as it does from files.
        ServiceGraph.model_validate_json(json.dumps(graph)),
        TimeWindow(start_at=start_at, end_at=end_at),
    )


def selector(draft: EvidenceDraft, evidence_id: EvidenceId) -> EvidenceSelector:
    """A selector for a whole committed row. Only a series narrows by window."""
    fields: dict[str, object] = {"kind": draft.kind, "evidence_id": evidence_id}
    if draft.kind is EvidenceKind.METRIC_SERIES_WINDOW:
        fields["window"] = draft.window
    return EVIDENCE_SELECTOR_ADAPTER.validate_python(fields)


class ToolGateway:
    def __init__(
        self,
        connection: Connection,
        lease: Lease,
        keeper: LeaseKeeper,
        telemetry: TelemetryProvider,
        scope: Scope,
    ) -> None:
        self._connection = connection
        self._lease = lease
        self._keeper = keeper
        self._telemetry = telemetry
        self._scope = scope

    def invocation(self, provider_call_id: str) -> InvocationId:
        """The call's identity, stable for the same model call within this attempt."""
        return InvocationId(uuid5(self._lease.attempt_id, provider_call_id))

    def refused(self, provider_call_id: str, code: ToolErrorCode, message: str) -> ToolResponse:
        """An error answer for a call that reserved nothing."""
        return _error(self.invocation(provider_call_id), code, message)

    async def call(self, provider_call_id: str, query: Query) -> ToolResponse:
        """Run one call of the tool `query` belongs to."""
        tool = _TOOL[type(query)]
        invocation_id = self.invocation(provider_call_id)
        outside = self._outside_scope(query)
        if outside is not None:
            return _error(invocation_id, ToolErrorCode.OUT_OF_SCOPE, outside)
        connection, lease = self._connection, self._lease
        self._keeper.require()
        try:
            async with fenced_transaction(connection, lease):
                call_id = await reserve_tool_call(
                    connection,
                    lease,
                    tool,
                    invocation_id,
                    provider_call_id,
                    sha256_digest(query.model_dump()),
                )
        except QuotaDenied as denied:
            return _error(invocation_id, ToolErrorCode.QUOTA_EXCEEDED, str(denied))
        self._keeper.require()
        async with fenced_transaction(connection, lease):
            await dispatch_tool_call(connection, lease, call_id)
        try:
            result = await self._ask(query)
        except QueryRejected as rejected:
            async with fenced_transaction(connection, lease):
                await fail_tool_call(connection, lease, call_id, rejected.code)
            return _error(invocation_id, rejected.code, str(rejected))
        status = _status(result)
        refs: list[EvidenceSelector] = []
        records: list[JsonValue] = []
        async with fenced_transaction(connection, lease):
            for draft in result.records:
                stored = await commit_evidence(connection, lease, draft)
                refs.append(selector(draft, stored.evidence_id))
                records.append(
                    {"evidence": refs[-1].model_dump(mode="json"), "content": stored.content}
                )
            await complete_tool_call(
                connection, lease, call_id, status, [ref.evidence_id for ref in refs]
            )
        return ToolResponse(
            schema_version="3.0",
            invocation_id=invocation_id,
            status=status,
            source=ToolSource(
                adapter=self._telemetry.adapter,
                version=self._telemetry.adapter_version,
                snapshot_id=self._scope.snapshot_id,
            ),
            data={"records": records},
            coverage=result.coverage,
            evidence_refs=refs,
            truncated=result.truncated,
            next_cursor=result.next_cursor,
            error=None,
        )

    async def _ask(self, query: Query) -> SourceResult:
        source = self._telemetry
        match query:
            case MetricsQuery():
                return await source.get_metrics(query)
            case LogQuery():
                return await source.search_logs(query)
            case DeploymentsQuery():
                return await source.get_deployments(query)
            case DependencyQuery():
                return await source.dependency_health(query)
            case TraceQuery():
                return await source.trace_summary(query)

    def _outside_scope(self, query: Query) -> str | None:
        services = [query.service]
        if isinstance(query, DependencyQuery):
            services.append(query.dependency)
        for service in services:
            if service not in self._scope.services:
                return f"{service} is outside this investigation's scope"
        if isinstance(query, DependencyQuery) and not self._scope.graph.allows(
            query.service, query.dependency
        ):
            return f"{query.dependency} isn't a dependency of {query.service}"
        if not self._scope.window.contains(query.window):
            allowed = self._scope.window
            return (
                f"the window must lie within {allowed.start_at.isoformat()}"
                f" to {allowed.end_at.isoformat()}"
            )
        return None


def _status(result: SourceResult) -> ToolStatus:
    if not result.coverage.complete:
        return ToolStatus.PARTIAL
    return ToolStatus.OK if result.records else ToolStatus.EMPTY


def _error(invocation_id: InvocationId, code: ToolErrorCode, message: str) -> ToolResponse:
    return ToolResponse(
        schema_version="3.0",
        invocation_id=invocation_id,
        status=ToolStatus.ERROR,
        source=None,
        data=None,
        coverage=None,
        evidence_refs=[],
        truncated=False,
        next_cursor=None,
        error=ToolError(
            code=code, retryable=code.retryable, message=message[:500], retry_after_ms=None
        ),
    )
