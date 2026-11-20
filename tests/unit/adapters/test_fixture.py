import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from incidentpilot.adapters.fixture import FixtureMismatch, FixtureTelemetry, open_fixture
from incidentpilot.adapters.fixture_bundle import bundle_sha256
from incidentpilot.domain.enums import EvidenceKind, LogSeverity, ToolErrorCode
from incidentpilot.domain.evidence import EvidenceDraft
from incidentpilot.domain.telemetry import (
    DependencyQuery,
    DeploymentsQuery,
    LogQuery,
    MetricsQuery,
    QueryRejected,
    SourceResult,
    TelemetryProvider,
    TraceQuery,
)
from incidentpilot.domain.tools import MAX_GAPS, Coverage
from incidentpilot.domain.window import TimeWindow
from tests.unit.adapters.test_fixture_bundle import (
    END,
    START,
    bundle,
    deployment,
    histogram_point,
    log,
    parse,
    scalar,
    span,
)

OBSERVATIONS = Path("scenarios/observations")


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def span_of(start: float, end: float) -> TimeWindow:
    return TimeWindow(start_at=at(start), end_at=at(end))


def adapter(**changes: Any) -> FixtureTelemetry:
    return FixtureTelemetry(parse(bundle(**changes)))


def complete(samples: int) -> Coverage:
    return Coverage(fraction=1.0, samples=samples, gaps=[], clock_caveat=None)


def stamp(minutes: float) -> str:
    return at(minutes).isoformat().replace("+00:00", "Z")


async def rejected(code: ToolErrorCode, message: str, answer: Any) -> None:
    with pytest.raises(QueryRejected, match=f"^{re.escape(message)}$") as raised:
        await answer
    assert raised.value.code is code


def metrics(
    *names: str, start: float = 0, end: float = 2, service: str = "checkout-api"
) -> MetricsQuery:
    return MetricsQuery(
        service=service, window=span_of(start, end), metrics=list(names), resolution_seconds=60
    )


def logs(**changes: Any) -> LogQuery:
    fields: dict[str, Any] = {
        "service": "checkout-api",
        "window": span_of(0, 120),
        "min_severity": LogSeverity.DEBUG,
        "contains": None,
        "limit": 10,
        "cursor": None,
    }
    return LogQuery(**(fields | changes))


def test_the_fixture_is_a_telemetry_provider() -> None:
    provider: TelemetryProvider = adapter()

    assert provider.adapter_version == "fixture-v1"


# Metrics


async def test_a_series_comes_back_as_one_row_with_its_points() -> None:
    result = await adapter().get_metrics(metrics("request_count"))

    assert result == SourceResult(
        records=(
            EvidenceDraft(
                kind=EvidenceKind.METRIC_SERIES_WINDOW,
                source_ref=f"metric/checkout-api/request_count/60s/{stamp(0)}/{stamp(2)}",
                adapter_version="fixture-v1",
                window=span_of(0, 2),
                content={
                    "service": "checkout-api",
                    "dependency": None,
                    "metric": "request_count",
                    "kind": "counter",
                    "unit": "count",
                    "resolution_seconds": 60,
                    "points": [
                        {"t": stamp(0), "v": 240.0},
                        {"t": stamp(1), "v": 250.0},
                    ],
                },
            ),
        ),
        coverage=complete(2),
        truncated=False,
        next_cursor=None,
    )


async def test_only_points_inside_the_window_come_back() -> None:
    result = await adapter().get_metrics(metrics("request_count", start=1, end=2))

    (record,) = result.records
    assert record.content["points"] == [{"t": stamp(1), "v": 250.0}]
    assert result.coverage == complete(1)


async def test_each_missing_point_is_a_gap() -> None:
    # The histogram has points at minutes 0 and 2, so minute 1 is missing.
    result = await adapter().get_metrics(metrics("request_latency", start=0, end=4))

    assert result.coverage == Coverage(
        fraction=0.5, samples=2, gaps=[span_of(1, 2), span_of(3, 4)], clock_caveat=None
    )
    (record,) = result.records
    assert record.content["points"] == [
        histogram_point(at(0)) | {"t": stamp(0)},
        histogram_point(at(2)) | {"t": stamp(2)},
    ]


async def test_several_metrics_share_one_coverage() -> None:
    result = await adapter().get_metrics(metrics("request_latency", "request_count", end=3))

    assert [record.content["metric"] for record in result.records] == [
        "request_latency",
        "request_count",
    ]
    assert result.coverage == Coverage(
        fraction=4 / 6, samples=4, gaps=[span_of(1, 3)], clock_caveat=None
    )


async def test_a_window_outside_the_snapshot_has_no_data_and_no_coverage() -> None:
    result = await adapter().get_metrics(metrics("request_count", start=120, end=122))

    assert result.records == ()
    assert result.coverage == Coverage(
        fraction=0.0, samples=0, gaps=[span_of(120, 122)], clock_caveat=None
    )


async def test_gaps_beyond_the_limit_fold_into_the_last() -> None:
    every_other = [{"t": at(n), "v": 1} for n in range(0, 120, 2)]
    fixture = adapter(metrics=[scalar(points=every_other)])

    result = await fixture.get_metrics(metrics("request_count", end=120))

    gaps = [span_of(n, n + 1) for n in range(1, 2 * MAX_GAPS - 2, 2)]
    assert result.coverage.gaps == [*gaps, span_of(2 * MAX_GAPS - 1, 120)]
    assert len(result.coverage.gaps) == MAX_GAPS
    assert result.coverage.fraction == 0.5


async def test_a_window_off_the_grid_is_refused() -> None:
    query = MetricsQuery(
        service="checkout-api",
        window=TimeWindow(start_at=at(0), end_at=at(1) + timedelta(seconds=30)),
        metrics=["request_count"],
        resolution_seconds=60,
    )

    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        f"request_count is recorded every 60s from {stamp(0)}:"
        " the window must start and end on that grid",
        adapter().get_metrics(query),
    )


async def test_a_window_with_too_many_points_is_refused() -> None:
    fits = metrics("request_count", end=10_000)
    await adapter().get_metrics(fits)

    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "the window holds 10001 points of request_count; at most 10000 fit",
        adapter().get_metrics(metrics("request_count", end=10_001)),
    )


async def test_a_metric_the_service_lacks_is_refused_with_the_ones_it_has() -> None:
    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "checkout-api records no error_count. Its metrics: request_count, request_latency",
        adapter().get_metrics(metrics("error_count")),
    )


async def test_a_service_with_no_metrics_says_so() -> None:
    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "inventory records no request_count. Its metrics: none",
        adapter().get_metrics(metrics("request_count", service="inventory")),
    )


async def test_another_resolution_is_refused() -> None:
    query = metrics("request_count").model_copy(update={"resolution_seconds": 300})

    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "request_count is recorded every 60s",
        adapter().get_metrics(query),
    )


async def test_a_refusal_lists_every_recorded_resolution() -> None:
    five = scalar(resolution_seconds=300, points=[{"t": at(0), "v": 1200}])
    query = metrics("request_count").model_copy(update={"resolution_seconds": 120})

    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "request_count is recorded every 300s, 60s",
        adapter(metrics=[scalar(), five]).get_metrics(query),
    )


async def test_the_series_at_the_asked_resolution_is_chosen() -> None:
    five = scalar(resolution_seconds=300, points=[{"t": at(0), "v": 1200}])
    fixture = adapter(metrics=[scalar(), five])

    query = metrics("request_count", end=5).model_copy(update={"resolution_seconds": 300})
    result = await fixture.get_metrics(query)

    (record,) = result.records
    assert record.content["points"] == [{"t": stamp(0), "v": 1200.0}]
    assert record.source_ref.startswith("metric/checkout-api/request_count/300s/")
    assert result.coverage == complete(1)


async def test_a_service_outside_the_graph_is_out_of_scope() -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        "billing isn't in this snapshot's service graph",
        adapter().get_metrics(metrics("request_count", service="billing")),
    )


# Dependency health


def dependency(callee: str, *names: str, service: str = "checkout-api") -> DependencyQuery:
    return DependencyQuery(
        service=service,
        dependency=callee,
        window=span_of(0, 2),
        metrics=list(names),
        resolution_seconds=60,
    )


async def test_dependency_health_reads_the_callers_series_for_that_callee() -> None:
    result = await adapter().dependency_health(dependency("payments", "error_count"))

    (record,) = result.records
    assert record.source_ref == (
        f"dependency/checkout-api/payments/error_count/60s/{stamp(0)}/{stamp(2)}"
    )
    assert (record.content["service"], record.content["dependency"]) == (
        "checkout-api",
        "payments",
    )
    assert result.coverage == complete(2)


async def test_dependency_series_stay_apart_from_the_services_own() -> None:
    await rejected(
        ToolErrorCode.INVALID_ARGUMENT,
        "checkout-api calling payments records no request_count. Its metrics: error_count",
        adapter().dependency_health(dependency("payments", "request_count")),
    )


@pytest.mark.parametrize(
    ("service", "callee"),
    [("checkout-api", "billing"), ("inventory", "checkout-api"), ("payments", "inventory")],
)
async def test_only_an_edge_in_the_graph_is_in_scope(service: str, callee: str) -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        f"{callee} isn't a dependency of {service}",
        adapter().dependency_health(dependency(callee, "error_count", service=service)),
    )


async def test_dependency_health_checks_the_caller_first() -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        "billing isn't in this snapshot's service graph",
        adapter().dependency_health(dependency("payments", "error_count", service="billing")),
    )


# Logs


def five_logs() -> FixtureTelemetry:
    return adapter(
        logs=[
            log(record_id="log-c", timestamp=at(10), severity="error", message="Pool EXHAUSTED"),
            log(record_id="log-a", timestamp=at(5), severity="info", message="Health check"),
            log(record_id="log-b", timestamp=at(10), severity="warn", message="pool low"),
            log(record_id="log-d", timestamp=at(30), severity="debug", message="tick"),
            log(record_id="log-e", service="payments", timestamp=at(10), message="pool low"),
        ]
    )


def ids(result: SourceResult) -> list[str]:
    return [str(record.content["record_id"]) for record in result.records]


async def test_a_log_record_is_one_row() -> None:
    result = await adapter().search_logs(logs())

    assert result.records == (
        EvidenceDraft(
            kind=EvidenceKind.LOG_RECORD,
            source_ref="log/checkout-api/log-0001",
            adapter_version="fixture-v1",
            window=None,
            content={
                "record_id": "log-0001",
                "service": "checkout-api",
                "timestamp": stamp(65),
                "severity": "warn",
                "message": "Upstream call slow",
                "attributes": {"route": "/checkout"},
            },
        ),
    )
    assert result.coverage == complete(1)
    assert (result.truncated, result.next_cursor) == (False, None)


async def test_logs_come_in_time_order_then_by_id_for_one_service() -> None:
    result = await five_logs().search_logs(logs())

    assert ids(result) == ["log-a", "log-b", "log-c", "log-d"]


async def test_logs_below_the_severity_are_left_out() -> None:
    result = await five_logs().search_logs(logs(min_severity=LogSeverity.WARN))

    assert ids(result) == ["log-b", "log-c"]


async def test_the_filter_matches_literally_ignoring_case() -> None:
    assert ids(await five_logs().search_logs(logs(contains="pool"))) == ["log-b", "log-c"]
    assert ids(await five_logs().search_logs(logs(contains="pool.*"))) == []


async def test_the_window_is_half_open() -> None:
    result = await five_logs().search_logs(logs(window=span_of(5, 10)))

    assert ids(result) == ["log-a"]


async def test_logs_page_through_with_a_cursor() -> None:
    fixture = five_logs()
    pages: list[list[str]] = []
    query = logs(limit=2)
    while True:
        result = await fixture.search_logs(query)
        pages.append(ids(result))
        assert result.truncated is (result.next_cursor is not None)
        if result.next_cursor is None:
            break
        query = query.model_copy(update={"cursor": result.next_cursor})

    assert pages == [["log-a", "log-b"], ["log-c", "log-d"]]


async def test_a_page_that_ends_exactly_has_no_next_page() -> None:
    result = await five_logs().search_logs(logs(limit=4))

    assert len(result.records) == 4
    assert (result.truncated, result.next_cursor) == (False, None)


async def test_a_cursor_only_continues_its_own_query() -> None:
    fixture = five_logs()
    first = await fixture.search_logs(logs(limit=1))
    assert first.next_cursor is not None

    other = logs(limit=1, min_severity=LogSeverity.WARN, cursor=first.next_cursor)
    await rejected(
        ToolErrorCode.CURSOR_INVALID,
        "the cursor doesn't continue this query",
        fixture.search_logs(other),
    )


async def test_a_cursor_only_continues_in_its_own_snapshot() -> None:
    first = await five_logs().search_logs(logs(limit=1))
    assert first.next_cursor is not None
    elsewhere = bundle()
    elsewhere["manifest"]["snapshot_id"] = "snap-other"
    fixture = FixtureTelemetry(parse(elsewhere))

    await rejected(
        ToolErrorCode.CURSOR_INVALID,
        "the cursor doesn't continue this query",
        fixture.search_logs(logs(limit=1, cursor=first.next_cursor)),
    )


@pytest.mark.parametrize("cursor", ["not-a-cursor", "abc_1", "1"])
async def test_a_cursor_from_elsewhere_is_refused(cursor: str) -> None:
    await rejected(
        ToolErrorCode.CURSOR_INVALID,
        "the cursor doesn't continue this query",
        five_logs().search_logs(logs(cursor=cursor)),
    )


@pytest.mark.parametrize("position", ["", "x", "-1", "1_2"])
async def test_a_cursor_with_its_scope_still_needs_a_position(position: str) -> None:
    fixture = five_logs()
    first = await fixture.search_logs(logs(limit=1))
    assert first.next_cursor is not None
    scope = first.next_cursor.split("_")[0]

    await rejected(
        ToolErrorCode.CURSOR_INVALID,
        "the cursor doesn't continue this query",
        fixture.search_logs(logs(limit=1, cursor=f"{scope}_{position}")),
    )


async def test_the_cursor_format_is_fixed() -> None:
    # Pinned so that a change to how cursors are scoped is a deliberate one.
    first = await five_logs().search_logs(logs(limit=2))

    assert first.next_cursor == "862ada7efa5f2a53a17f04c18c2598f1_2"


async def test_a_cursor_past_the_end_continues_with_nothing() -> None:
    fixture = five_logs()
    first = await fixture.search_logs(logs(limit=1))
    assert first.next_cursor is not None
    scope = first.next_cursor.split("_")[0]

    result = await fixture.search_logs(logs(limit=1, cursor=f"{scope}_99"))

    assert (result.records, result.truncated, result.next_cursor) == ((), False, None)


async def test_log_coverage_is_the_part_of_the_window_the_snapshot_holds() -> None:
    result = await five_logs().search_logs(logs(window=span_of(-30, 30)))

    assert ids(result) == ["log-a", "log-b", "log-c"]
    assert result.coverage == Coverage(
        fraction=0.5, samples=3, gaps=[span_of(-30, 0)], clock_caveat=None
    )


async def test_logs_after_the_snapshot_are_a_gap_at_the_end() -> None:
    result = await five_logs().search_logs(logs(window=span_of(110, 130)))

    assert result.coverage == Coverage(
        fraction=0.5, samples=0, gaps=[span_of(120, 130)], clock_caveat=None
    )


@pytest.mark.parametrize("window", [span_of(120, 130), span_of(-20, 0)], ids=["after", "before"])
async def test_logs_entirely_outside_the_snapshot_have_no_coverage(window: TimeWindow) -> None:
    result = await five_logs().search_logs(logs(window=window))

    assert result.records == ()
    assert result.coverage == Coverage(fraction=0.0, samples=0, gaps=[window], clock_caveat=None)


async def test_logs_for_a_service_outside_the_graph_are_out_of_scope() -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        "billing isn't in this snapshot's service graph",
        adapter().search_logs(logs(service="billing")),
    )


# Deployments


def deployments(**changes: Any) -> DeploymentsQuery:
    fields: dict[str, Any] = {
        "service": "checkout-api",
        "window": span_of(0, 120),
        "limit": 10,
        "cursor": None,
    }
    return DeploymentsQuery(**(fields | changes))


async def test_a_deployment_is_one_row() -> None:
    result = await adapter().get_deployments(deployments())

    assert result.records == (
        EvidenceDraft(
            kind=EvidenceKind.DEPLOYMENT_RECORD,
            source_ref="deployment/checkout-api/dep-0001",
            adapter_version="fixture-v1",
            window=None,
            content={
                "record_id": "dep-0001",
                "service": "checkout-api",
                "revision": "r42",
                "previous_revision": "r41",
                "deployed_at": stamp(55),
                "change_summary": "Refactor order validation",
            },
        ),
    )
    assert result.coverage == complete(1)


async def test_deployments_page_in_time_order_for_one_service() -> None:
    fixture = adapter(
        deployments=[
            deployment(record_id="dep-2", deployed_at=at(50), revision="r2"),
            deployment(record_id="dep-1", deployed_at=at(20), revision="r1"),
            deployment(record_id="dep-3", deployed_at=at(80), revision="r3"),
            deployment(record_id="dep-x", service="payments", deployed_at=at(30)),
            deployment(record_id="dep-4", deployed_at=at(119), revision="r4"),
        ]
    )

    first = await fixture.get_deployments(deployments(window=span_of(20, 119), limit=2))
    assert ids(first) == ["dep-1", "dep-2"]
    second = await fixture.get_deployments(
        deployments(window=span_of(20, 119), limit=2, cursor=first.next_cursor)
    )
    assert ids(second) == ["dep-3"]
    assert (second.truncated, second.next_cursor) == (False, None)


async def test_deployments_for_a_service_outside_the_graph_are_out_of_scope() -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        "billing isn't in this snapshot's service graph",
        adapter().get_deployments(deployments(service="billing")),
    )


# Traces


def traces(**changes: Any) -> TraceQuery:
    fields: dict[str, Any] = {
        "service": "checkout-api",
        "window": span_of(60, 120),
        "limit": 10,
        "cursor": None,
    }
    return TraceQuery(**(fields | changes))


async def test_a_trace_summary_is_one_row_with_its_own_window() -> None:
    result = await adapter().trace_summary(traces(window=span_of(60, 90)))

    (record,) = result.records
    assert record == EvidenceDraft(
        kind=EvidenceKind.SPAN_SUMMARY,
        source_ref="span/checkout-api/span-0001",
        adapter_version="fixture-v1",
        window=span_of(60, 90),
        content=record.content,
    )
    assert record.content["span_count"] == 1200
    assert result.coverage == complete(1)


async def test_trace_coverage_is_only_where_summaries_exist() -> None:
    fixture = adapter(
        spans=[
            span(record_id="span-2", window={"start_at": at(90), "end_at": at(100)}),
            span(record_id="span-1", window={"start_at": at(30), "end_at": at(70)}),
            span(
                record_id="span-x",
                service="payments",
                segments=[{"downstream": None, "p50_ms": 5, "p95_ms": 9}],
            ),
            span(record_id="span-0", window={"start_at": at(0), "end_at": at(30)}),
        ]
    )

    result = await fixture.trace_summary(traces())

    assert ids(result) == ["span-1", "span-2"]
    assert result.coverage == Coverage(
        fraction=20 / 60,
        samples=2,
        gaps=[span_of(70, 90), span_of(100, 120)],
        clock_caveat=None,
    )


async def test_overlapping_summaries_count_once() -> None:
    fixture = adapter(
        spans=[
            span(record_id="span-1", window={"start_at": at(60), "end_at": at(100)}),
            span(record_id="span-2", window={"start_at": at(80), "end_at": at(120)}),
        ]
    )

    result = await fixture.trace_summary(traces())

    assert result.coverage == complete(2)


async def test_trace_coverage_stops_at_the_snapshot() -> None:
    fixture = adapter(spans=[span(window={"start_at": at(90), "end_at": END})])

    result = await fixture.trace_summary(traces(window=span_of(90, 150)))

    assert result.coverage == Coverage(
        fraction=0.5, samples=1, gaps=[span_of(120, 150)], clock_caveat=None
    )


async def test_traces_entirely_outside_the_snapshot_have_no_coverage() -> None:
    result = await adapter().trace_summary(traces(window=span_of(120, 150)))

    assert result.coverage == Coverage(
        fraction=0.0, samples=0, gaps=[span_of(120, 150)], clock_caveat=None
    )


async def test_trace_summaries_page_with_a_cursor() -> None:
    fixture = adapter(
        spans=[
            span(record_id="span-1", window={"start_at": at(60), "end_at": at(80)}),
            span(record_id="span-2", window={"start_at": at(80), "end_at": at(100)}),
        ]
    )

    first = await fixture.trace_summary(traces(limit=1))
    second = await fixture.trace_summary(traces(limit=1, cursor=first.next_cursor))

    assert (ids(first), first.truncated) == (["span-1"], True)
    assert (ids(second), second.truncated) == (["span-2"], False)
    # Coverage describes the asked-for window, whichever page it comes with.
    assert first.coverage.fraction == second.coverage.fraction == 2 / 3


async def test_no_summaries_means_no_trace_coverage() -> None:
    result = await adapter(spans=[]).trace_summary(traces())

    assert result.records == ()
    assert result.coverage == Coverage(
        fraction=0.0, samples=0, gaps=[span_of(60, 120)], clock_caveat=None
    )


async def test_traces_for_a_service_outside_the_graph_are_out_of_scope() -> None:
    await rejected(
        ToolErrorCode.OUT_OF_SCOPE,
        "billing isn't in this snapshot's service graph",
        adapter().trace_summary(traces(service="billing")),
    )


# Opening a snapshot


@pytest.mark.parametrize("path", sorted(OBSERVATIONS.glob("snap-*.json")), ids=lambda p: p.stem)
async def test_each_imported_snapshot_opens_and_answers(path: Path) -> None:
    bundle_ = parse(json.loads(path.read_text()))

    fixture = open_fixture(OBSERVATIONS, path.stem, bundle_sha256(bundle_))

    incident = bundle_.manifest.incident
    result = await fixture.search_logs(
        logs(service=incident.service, window=bundle_.manifest.observation_window, limit=100)
    )
    assert result.coverage.complete
    assert result.records


async def test_a_bundle_changed_since_its_import_is_refused() -> None:
    with pytest.raises(
        FixtureMismatch, match=r"^the bundle for snapshot snap-9cbd373fc274 changed"
    ):
        open_fixture(OBSERVATIONS, "snap-9cbd373fc274", "0" * 64)


async def test_a_missing_bundle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FixtureMismatch, match=r"^snapshot snap-gone has no valid bundle in "):
        open_fixture(tmp_path, "snap-gone", "0" * 64)


async def test_an_invalid_bundle_is_refused_without_quoting_it(tmp_path: Path) -> None:
    (tmp_path / "snap-bad.json").write_text('{"secret": "do not echo"}')

    with pytest.raises(FixtureMismatch) as raised:
        open_fixture(tmp_path, "snap-bad", "0" * 64)

    assert "do not echo" not in str(raised.value)
    assert raised.value.__cause__ is None


async def test_a_bundle_filed_under_another_id_is_refused(tmp_path: Path) -> None:
    source = OBSERVATIONS / "snap-9cbd373fc274.json"
    (tmp_path / "snap-renamed.json").write_bytes(source.read_bytes())
    digest = bundle_sha256(parse(json.loads(source.read_text())))

    with pytest.raises(FixtureMismatch, match=r"^the bundle for snapshot snap-renamed changed"):
        open_fixture(tmp_path, "snap-renamed", digest)


def test_a_next_page_means_a_truncated_result() -> None:
    with pytest.raises(ValueError, match=r"^a result with a next page is truncated$"):
        SourceResult((), complete(0), truncated=False, next_cursor="abc")
