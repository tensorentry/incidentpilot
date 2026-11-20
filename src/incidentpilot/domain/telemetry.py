"""What a telemetry source is asked and what it answers (spec §8.1, §8.3, §9.1, E16).

A query holds only what the model may choose. The gateway (T4.3) checks it against
the investigation's scope before any source sees it. A source answers with evidence
drafts in E15 units and honest coverage, and raises `QueryRejected` for a query it
can't answer as asked.
"""

from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import AfterValidator, Field

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import LogSeverity, ToolErrorCode
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.domain.measurement import distinct
from incidentpilot.domain.names import MetricName, ServiceName
from incidentpilot.domain.tools import Coverage
from incidentpilot.domain.window import TimeWindow

MAX_PAGE = 100
MAX_METRICS = 8
MAX_FILTER = 100

Cursor = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,512}$")]
Metrics = Annotated[
    list[MetricName], Field(min_length=1, max_length=MAX_METRICS), AfterValidator(distinct)
]
Resolution = Annotated[int, Field(ge=1, le=3600)]
Limit = Annotated[int, Field(ge=1, le=MAX_PAGE)]


class MetricsQuery(Contract):
    service: ServiceName
    window: TimeWindow
    metrics: Metrics
    resolution_seconds: Resolution


class DependencyQuery(Contract):
    """Metrics `service` recorded for its calls to `dependency`."""

    service: ServiceName
    dependency: ServiceName
    window: TimeWindow
    metrics: Metrics
    resolution_seconds: Resolution


class LogQuery(Contract):
    service: ServiceName
    window: TimeWindow
    min_severity: LogSeverity
    # Matched literally and ignoring case. There is no query language (§8.3).
    contains: Annotated[str, Field(min_length=1, max_length=MAX_FILTER)] | None
    limit: Limit
    cursor: Cursor | None


class DeploymentsQuery(Contract):
    service: ServiceName
    window: TimeWindow
    limit: Limit
    cursor: Cursor | None


class TraceQuery(Contract):
    service: ServiceName
    window: TimeWindow
    limit: Limit
    cursor: Cursor | None


class QueryRejected(Exception):
    """The source can't answer this query as asked. `code` is the tool error code."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceResult:
    """One page of records, and how much of the asked-for data the source had."""

    records: tuple[EvidenceDraft, ...]
    coverage: Coverage
    truncated: bool
    next_cursor: str | None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not self.truncated:
            raise ValueError("a result with a next page is truncated")


class TelemetryProvider(Protocol):
    """A read-only telemetry source bound to one snapshot."""

    @property
    def adapter_version(self) -> str: ...

    async def get_metrics(self, query: MetricsQuery) -> SourceResult: ...

    async def search_logs(self, query: LogQuery) -> SourceResult: ...

    async def get_deployments(self, query: DeploymentsQuery) -> SourceResult: ...

    async def dependency_health(self, query: DependencyQuery) -> SourceResult: ...

    async def trace_summary(self, query: TraceQuery) -> SourceResult: ...
