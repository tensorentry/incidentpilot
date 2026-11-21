"""The fixture telemetry adapter: answers queries from one observation bundle (T4.2).

It reads only its bundle, and only after checking the bundle against the digest stored
when the snapshot was imported, so a file edited since then is refused.

Every answer comes in E15 units, one evidence draft per citable record, with coverage
that says what the bundle didn't have. Missing data is a gap, never a zero (§8.3):
- A metric series reports each missing point on its grid as a gap.
- Logs, deployments and traces report the parts of the window the snapshot doesn't
  cover.
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import JsonValue, ValidationError

from incidentpilot.adapters.fixture_bundle import (
    MAX_POINTS,
    Deployment,
    LogRecord,
    MetricSeries,
    ObservationBundle,
    SpanSummary,
    bundle_sha256,
)
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import EvidenceKind, LogSeverity, ToolErrorCode
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.domain.telemetry import (
    DependencyQuery,
    DeploymentsQuery,
    LogQuery,
    MetricsQuery,
    QueryRejected,
    SourceResult,
    TraceQuery,
)
from incidentpilot.domain.tools import MAX_GAPS, Coverage
from incidentpilot.domain.window import TimeWindow

_SEVERITY = list(LogSeverity)

type Key = tuple[datetime, str]


class FixtureMismatch(Exception):
    """The bundle on disk isn't the snapshot that was imported."""


def open_fixture(root: Path, snapshot_id: str, content_sha256: str) -> "FixtureTelemetry":
    """The adapter for one imported snapshot, read from `root`."""
    path = root / f"{snapshot_id}.json"
    try:
        bundle = ObservationBundle.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        # Validation errors would quote the file's content.
        raise FixtureMismatch(f"snapshot {snapshot_id} has no valid bundle in {root}") from None
    if bundle.manifest.snapshot_id != snapshot_id or bundle_sha256(bundle) != content_sha256:
        raise FixtureMismatch(f"the bundle for snapshot {snapshot_id} changed since its import")
    return FixtureTelemetry(bundle)


class FixtureTelemetry:
    def __init__(self, bundle: ObservationBundle) -> None:
        self._bundle = bundle
        self._manifest = bundle.manifest

    @property
    def adapter(self) -> str:
        return "fixture"

    @property
    def adapter_version(self) -> str:
        return self._manifest.adapter_version

    async def get_metrics(self, query: MetricsQuery) -> SourceResult:
        self._require_service(query.service)
        return self._series(
            query.service, None, query.window, query.metrics, query.resolution_seconds
        )

    async def dependency_health(self, query: DependencyQuery) -> SourceResult:
        self._require_service(query.service)
        if not self._manifest.service_graph.allows(query.service, query.dependency):
            raise QueryRejected(
                ToolErrorCode.OUT_OF_SCOPE,
                f"{query.dependency} isn't a dependency of {query.service}",
            )
        return self._series(
            query.service, query.dependency, query.window, query.metrics, query.resolution_seconds
        )

    async def search_logs(self, query: LogQuery) -> SourceResult:
        self._require_service(query.service)
        floor = _SEVERITY.index(query.min_severity)
        needle = None if query.contains is None else query.contains.casefold()
        matches = [
            log
            for log in self._bundle.logs
            if log.service == query.service
            and _inside(log.timestamp, query.window)
            and _SEVERITY.index(log.severity) >= floor
            and (needle is None or needle in log.message.casefold())
        ]
        return self._page(query, matches, _log_key, _log_draft, self._observed(query.window))

    async def get_deployments(self, query: DeploymentsQuery) -> SourceResult:
        self._require_service(query.service)
        matches = [
            deployment
            for deployment in self._bundle.deployments
            if deployment.service == query.service and _inside(deployment.deployed_at, query.window)
        ]
        return self._page(
            query, matches, _deployment_key, _deployment_draft, self._observed(query.window)
        )

    async def trace_summary(self, query: TraceQuery) -> SourceResult:
        self._require_service(query.service)
        matches = [
            summary
            for summary in self._bundle.spans
            if summary.service == query.service and _overlap(summary.window, query.window)
        ]
        # A summary covers only its own window, so the rest of the asked-for window has
        # no trace data, however many summaries there are.
        covered = [
            clipped
            for observed in self._observed(query.window)
            for summary in matches
            if (clipped := _overlap(summary.window, observed))
        ]
        return self._page(query, matches, _span_key, _span_draft, covered)

    def _observed(self, window: TimeWindow) -> list[TimeWindow]:
        """The part of `window` this snapshot holds telemetry for, if any."""
        observed = _overlap(self._manifest.observation_window, window)
        return [] if observed is None else [observed]

    def _require_service(self, service: str) -> None:
        if service not in self._manifest.service_graph.services:
            raise QueryRejected(
                ToolErrorCode.OUT_OF_SCOPE, f"{service} isn't in this snapshot's service graph"
            )

    def _series(
        self,
        service: str,
        dependency: str | None,
        window: TimeWindow,
        metrics: Sequence[str],
        resolution: int,
    ) -> SourceResult:
        step = timedelta(seconds=resolution)
        recorded = [
            series
            for series in self._bundle.metrics
            if series.service == service and series.dependency == dependency
        ]
        records: list[EvidenceDraft] = []
        present = expected = 0
        missing: list[TimeWindow] = []
        for metric in metrics:
            series = self._one_series(recorded, service, dependency, metric, resolution)
            anchor = series.window.start_at
            if (window.start_at - anchor) % step or (window.end_at - anchor) % step:
                raise QueryRejected(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"{metric} is recorded every {resolution}s from {_stamp(anchor)}:"
                    " the window must start and end on that grid",
                )
            slots = (window.end_at - window.start_at) // step
            if slots > MAX_POINTS:
                raise QueryRejected(
                    ToolErrorCode.INVALID_ARGUMENT,
                    f"the window holds {slots} points of {metric}; at most {MAX_POINTS} fit",
                )
            points = [point for point in series.points if _inside(point.t, window)]
            have = {point.t for point in points}
            expected += slots
            present += len(points)
            missing += [
                TimeWindow(start_at=t, end_at=t + step)
                for t in (window.start_at + n * step for n in range(slots))
                if t not in have
            ]
            if points:
                records.append(_series_draft(series, window, points, self.adapter_version))
        gaps = _merged(missing)
        coverage = Coverage(
            fraction=present / expected,
            samples=present,
            gaps=_bounded(gaps),
            clock_caveat=None,
        )
        return SourceResult(tuple(records), coverage, truncated=False, next_cursor=None)

    @staticmethod
    def _one_series(
        recorded: Sequence[MetricSeries],
        service: str,
        dependency: str | None,
        metric: str,
        resolution: int,
    ) -> MetricSeries:
        named = [series for series in recorded if series.metric == metric]
        if not named:
            known = ", ".join(sorted({series.metric for series in recorded})) or "none"
            source = service if dependency is None else f"{service} calling {dependency}"
            raise QueryRejected(
                ToolErrorCode.INVALID_ARGUMENT,
                f"{source} records no {metric}. Its metrics: {known}",
            )
        for series in named:
            if series.resolution_seconds == resolution:
                return series
        every = ", ".join(sorted(f"{series.resolution_seconds}s" for series in named))
        raise QueryRejected(ToolErrorCode.INVALID_ARGUMENT, f"{metric} is recorded every {every}")

    def _page[T](
        self,
        query: LogQuery | DeploymentsQuery | TraceQuery,
        matches: Sequence[T],
        key: Callable[[T], Key],
        draft: Callable[[T, str], EvidenceDraft],
        covered: Sequence[TimeWindow],
    ) -> SourceResult:
        # The snapshot never changes and the order is total, so a position in the
        # sorted matches is a stable place to continue from.
        scope = self._cursor_scope(query)
        start = 0 if query.cursor is None else _read_cursor(query.cursor, scope)
        ordered = sorted(matches, key=key)
        page = ordered[start : start + query.limit]
        end = start + len(page)
        more = end < len(ordered)
        return SourceResult(
            tuple(draft(item, self.adapter_version) for item in page),
            _time_coverage(query.window, covered, len(page)),
            truncated=more,
            next_cursor=f"{scope}_{end}" if more else None,
        )

    def _cursor_scope(self, query: Contract) -> str:
        # A cursor only continues the query that issued it, in the same snapshot.
        asked = query.model_dump(exclude={"cursor"})
        return sha256_digest({"snapshot": self._manifest.snapshot_id, "query": asked})[:32]


def _inside(t: datetime, window: TimeWindow) -> bool:
    return window.start_at <= t < window.end_at


def _overlap(a: TimeWindow, b: TimeWindow) -> TimeWindow | None:
    start, end = max(a.start_at, b.start_at), min(a.end_at, b.end_at)
    return TimeWindow(start_at=start, end_at=end) if start < end else None


def _merged(windows: Iterable[TimeWindow]) -> list[TimeWindow]:
    merged: list[TimeWindow] = []
    for window in sorted(windows, key=lambda w: (w.start_at, w.end_at)):
        if merged and window.start_at <= merged[-1].end_at:
            last = merged.pop()
            window = TimeWindow(start_at=last.start_at, end_at=max(last.end_at, window.end_at))
        merged.append(window)
    return merged


def _bounded(gaps: list[TimeWindow]) -> list[TimeWindow]:
    """At most MAX_GAPS gaps. The last one widens to cover the rest, so data that is
    there may be reported missing, but missing data is never reported there."""
    kept, rest = gaps[: MAX_GAPS - 1], gaps[MAX_GAPS - 1 :]
    if not rest:
        return kept
    return [*kept, TimeWindow(start_at=rest[0].start_at, end_at=rest[-1].end_at)]


def _time_coverage(window: TimeWindow, covered: Sequence[TimeWindow], samples: int) -> Coverage:
    held = _merged(covered)
    gaps: list[TimeWindow] = []
    start = window.start_at
    for part in held:
        if start < part.start_at:
            gaps.append(TimeWindow(start_at=start, end_at=part.start_at))
        start = part.end_at
    if start < window.end_at:
        gaps.append(TimeWindow(start_at=start, end_at=window.end_at))
    total = window.end_at - window.start_at
    held_time = sum((part.end_at - part.start_at for part in held), timedelta())
    return Coverage(
        fraction=held_time / total, samples=samples, gaps=_bounded(gaps), clock_caveat=None
    )


def _stamp(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _read_cursor(cursor: str, scope: str) -> int:
    position = cursor.removeprefix(f"{scope}_")
    if position == cursor or not position.isdigit():
        raise QueryRejected(ToolErrorCode.CURSOR_INVALID, "the cursor doesn't continue this query")
    return int(position)


def _log_key(log: LogRecord) -> Key:
    return log.timestamp, log.record_id


def _deployment_key(deployment: Deployment) -> Key:
    return deployment.deployed_at, deployment.record_id


def _span_key(summary: SpanSummary) -> Key:
    return summary.window.start_at, summary.record_id


def _content(record: Contract) -> dict[str, JsonValue]:
    return record.model_dump(mode="json")


def _log_draft(log: LogRecord, adapter_version: str) -> EvidenceDraft:
    return EvidenceDraft(
        kind=EvidenceKind.LOG_RECORD,
        source_ref=f"log/{log.service}/{log.record_id}",
        adapter_version=adapter_version,
        window=None,
        content=_content(log),
    )


def _deployment_draft(deployment: Deployment, adapter_version: str) -> EvidenceDraft:
    return EvidenceDraft(
        kind=EvidenceKind.DEPLOYMENT_RECORD,
        source_ref=f"deployment/{deployment.service}/{deployment.record_id}",
        adapter_version=adapter_version,
        window=None,
        content=_content(deployment),
    )


def _span_draft(summary: SpanSummary, adapter_version: str) -> EvidenceDraft:
    return EvidenceDraft(
        kind=EvidenceKind.SPAN_SUMMARY,
        source_ref=f"span/{summary.service}/{summary.record_id}",
        adapter_version=adapter_version,
        window=summary.window,
        content=_content(summary),
    )


def _series_draft(
    series: MetricSeries, window: TimeWindow, points: Sequence[Contract], adapter_version: str
) -> EvidenceDraft:
    """One row per series and window (E15), holding its points so they can be
    recomputed without going back to the source."""
    path = series.service if series.dependency is None else f"{series.service}/{series.dependency}"
    family = "metric" if series.dependency is None else "dependency"
    return EvidenceDraft(
        kind=EvidenceKind.METRIC_SERIES_WINDOW,
        source_ref=(
            f"{family}/{path}/{series.metric}/{series.resolution_seconds}s"
            f"/{_stamp(window.start_at)}/{_stamp(window.end_at)}"
        ),
        adapter_version=adapter_version,
        window=window,
        content={
            "service": series.service,
            "dependency": series.dependency,
            "metric": series.metric,
            "kind": series.kind.value,
            "unit": series.unit.value,
            "resolution_seconds": series.resolution_seconds,
            "points": [_content(point) for point in points],
        },
    )
