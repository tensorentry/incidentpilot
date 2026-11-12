"""The observation bundle: everything an investigator may ever see for one snapshot.

One bundle is one file under `scenarios/observations/`. It holds source records in
the units of erratum E15; the fixture adapter (T4.2) turns query results into
evidence rows. Oracle labels never appear here: they live under `scenarios/oracle/`
and only `incidentpilot.evaluation` reads them (E12).
"""

from collections.abc import Iterator
from datetime import timedelta
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import LogSeverity, MeasurementUnit, MetricKind
from incidentpilot.domain.names import LocalId, MetricName, ServiceName
from incidentpilot.domain.snapshot import SnapshotManifest
from incidentpilot.domain.window import TimeWindow, UtcDatetime

MAX_POINTS = 10_000
MAX_BUCKETS = 64
MAX_RECORDS = 5_000
MAX_MESSAGE = 2_000
MAX_TEXT = 500

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0)]


class ScalarPoint(Contract):
    t: UtcDatetime
    v: FiniteFloat


class Bucket(Contract):
    le: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    count: Count


class HistogramPoint(Contract):
    """Buckets are counts, not cumulative; `overflow` counts values above the last bound."""

    t: UtcDatetime
    buckets: Annotated[list[Bucket], Field(min_length=1, max_length=MAX_BUCKETS)]
    overflow: Count

    @model_validator(mode="after")
    def _bounds_increase(self) -> Self:
        bounds = [bucket.le for bucket in self.buckets]
        if any(later <= earlier for earlier, later in pairwise(bounds)):
            raise ValueError("bucket bounds must increase")
        return self


class _Series(Contract):
    """One E15 metric row. `dependency` names the callee for dependency health."""

    service: ServiceName
    dependency: ServiceName | None
    metric: MetricName
    unit: MeasurementUnit
    resolution_seconds: Annotated[int, Field(ge=1, le=3600)]
    window: TimeWindow

    def _require_aligned(self, times: list[UtcDatetime]) -> None:
        # Missing points are gaps in coverage; points off the grid would hide them.
        step = timedelta(seconds=self.resolution_seconds)
        if any(later <= earlier for earlier, later in pairwise(times)):
            raise ValueError("points must be in time order without repeats")
        for t in times:
            if not self.window.start_at <= t < self.window.end_at:
                raise ValueError("points must fall inside the series window")
            if (t - self.window.start_at) % step:
                raise ValueError("points must sit on the resolution grid")


class ScalarSeries(_Series):
    kind: Literal[MetricKind.COUNTER, MetricKind.GAUGE]
    points: Annotated[list[ScalarPoint], Field(max_length=MAX_POINTS)]

    @model_validator(mode="after")
    def _points_fit(self) -> Self:
        self._require_aligned([point.t for point in self.points])
        return self


class HistogramSeries(_Series):
    kind: Literal[MetricKind.HISTOGRAM]
    points: Annotated[list[HistogramPoint], Field(max_length=MAX_POINTS)]

    @model_validator(mode="after")
    def _points_fit(self) -> Self:
        self._require_aligned([point.t for point in self.points])
        # Histograms only aggregate across matching bounds (§8.3).
        if len({tuple(bucket.le for bucket in point.buckets) for point in self.points}) > 1:
            raise ValueError("every point in a histogram needs the same bucket bounds")
        return self


MetricSeries = Annotated[ScalarSeries | HistogramSeries, Field(discriminator="kind")]


class LogRecord(Contract):
    record_id: LocalId
    service: ServiceName
    timestamp: UtcDatetime
    severity: LogSeverity
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE)]
    attributes: Annotated[dict[str, JsonValue], Field(max_length=32)]


class Deployment(Contract):
    record_id: LocalId
    service: ServiceName
    revision: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
    previous_revision: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")] | None
    deployed_at: UtcDatetime
    change_summary: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


class SpanSegment(Contract):
    """Time spent in the service itself (`downstream` is null) or waiting on a callee."""

    downstream: ServiceName | None
    p50_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    p95_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.p95_ms < self.p50_ms:
            raise ValueError("p95 can't be below p50")
        return self


class SpanSummary(Contract):
    record_id: LocalId
    service: ServiceName
    operation: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,127}$")]
    window: TimeWindow
    span_count: Count
    segments: Annotated[list[SpanSegment], Field(min_length=1, max_length=32)]


class ObservationBundle(Contract):
    manifest: SnapshotManifest
    metrics: Annotated[list[MetricSeries], Field(max_length=MAX_RECORDS)]
    logs: Annotated[list[LogRecord], Field(max_length=MAX_RECORDS)]
    deployments: Annotated[list[Deployment], Field(max_length=MAX_RECORDS)]
    spans: Annotated[list[SpanSummary], Field(max_length=MAX_RECORDS)]

    @model_validator(mode="after")
    def _records_fit_the_manifest(self) -> Self:
        graph = self.manifest.service_graph
        observed = self.manifest.observation_window
        for service, callee in self._edges():
            if service not in graph.services:
                raise ValueError("every record's service must be in the graph")
            if callee is not None and not graph.allows(service, callee):
                raise ValueError("every dependency in a record must be an edge in the graph")
        for window in [
            *(series.window for series in self.metrics),
            *(s.window for s in self.spans),
        ]:
            if not observed.contains(window):
                raise ValueError("every window must lie inside the observation window")
        for t in [
            *(log.timestamp for log in self.logs),
            *(d.deployed_at for d in self.deployments),
        ]:
            if not observed.start_at <= t < observed.end_at:
                raise ValueError("every timestamp must lie inside the observation window")
        ids = [
            r.record_id for records in (self.logs, self.deployments, self.spans) for r in records
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("record IDs must be unique within a bundle")
        return self

    def _edges(self) -> Iterator[tuple[str, str | None]]:
        for series in self.metrics:
            yield series.service, series.dependency
        for log in self.logs:
            yield log.service, None
        for deployment in self.deployments:
            yield deployment.service, None
        for summary in self.spans:
            yield summary.service, None
            for segment in summary.segments:
                yield summary.service, segment.downstream


def bundle_sha256(bundle: ObservationBundle) -> str:
    """The snapshot's `content_sha256` (E7). Independent of key order and time offsets."""
    return sha256_digest(bundle.model_dump())
