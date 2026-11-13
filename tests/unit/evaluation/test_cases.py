"""Each case carries the signals spec §14.1 describes, and only those."""

from collections.abc import Callable
from functools import cache

import pytest

from incidentpilot.adapters.fixture_bundle import (
    HistogramSeries,
    ObservationBundle,
    ScalarSeries,
    SpanSummary,
)
from incidentpilot.domain.enums import CauseCategory, DiagnosticOutcome, LogSeverity
from incidentpilot.domain.window import TimeWindow
from incidentpilot.evaluation.cases import (
    BASELINE,
    CASES,
    INCIDENT,
    GeneratedCase,
    dependency_degradation,
    missing_dependency_telemetry,
    synchronous_dependency,
)

FLAT = 0.15


@cache
def first() -> GeneratedCase:
    return synchronous_dependency(7)


@cache
def second() -> GeneratedCase:
    return dependency_degradation(7)


@cache
def third() -> GeneratedCase:
    return missing_dependency_telemetry(7)


def p95(series: HistogramSeries, window: TimeWindow) -> float:
    """The bucket bound below which 95% of the window's samples fall."""
    points = [p for p in series.points if window.start_at <= p.t < window.end_at]
    totals = [sum(p.buckets[i].count for p in points) for i in range(len(points[0].buckets))]
    target = 0.95 * (sum(totals) + sum(p.overflow for p in points))
    running = 0
    for bucket, total in zip(points[0].buckets, totals, strict=True):
        running += total
        if running >= target:
            return bucket.le
    return float("inf")


def rate(series: ScalarSeries, window: TimeWindow) -> float:
    points = [p.v for p in series.points if window.start_at <= p.t < window.end_at]
    return sum(points) / len(points)


def points_in(series: ScalarSeries | HistogramSeries, window: TimeWindow) -> int:
    return sum(1 for p in series.points if window.start_at <= p.t < window.end_at)


def histogram(
    bundle: ObservationBundle, service: str, dependency: str | None = None
) -> HistogramSeries:
    for series in bundle.metrics:
        if isinstance(series, HistogramSeries) and (series.service, series.dependency) == (
            service,
            dependency,
        ):
            return series
    raise LookupError(service)


def counter(
    bundle: ObservationBundle, service: str, metric: str, dependency: str | None = None
) -> ScalarSeries:
    for series in bundle.metrics:
        if isinstance(series, ScalarSeries) and (
            series.service,
            series.metric,
            series.dependency,
        ) == (
            service,
            metric,
            dependency,
        ):
            return series
    raise LookupError(metric)


def segment_p95(summary: SpanSummary, downstream: str | None) -> float:
    return next(s.p95_ms for s in summary.segments if s.downstream == downstream)


def unchanged(series: HistogramSeries) -> bool:
    return p95(series, INCIDENT) == p95(series, BASELINE)


# What every case shares


def test_every_committed_case_is_built() -> None:
    assert [build for build, _ in CASES] == [
        synchronous_dependency,
        dependency_degradation,
        missing_dependency_telemetry,
    ]


@pytest.mark.parametrize("build", [first, second, third], ids=["first", "second", "third"])
def test_checkout_slows_down_in_every_case(build: Callable[[], GeneratedCase]) -> None:
    bundle = build().bundle
    checkout = histogram(bundle, "checkout-api")

    assert p95(checkout, INCIDENT) >= 2 * p95(checkout, BASELINE)


def test_the_cases_look_alike_from_outside() -> None:
    manifests = [case().bundle.manifest for case in (first, second, third)]
    shared = [m.model_dump(exclude={"snapshot_id"}) for m in manifests]

    assert shared[0] == shared[1] == shared[2]
    assert len({m.snapshot_id for m in manifests}) == 3


@pytest.mark.parametrize("build", [first, second, third], ids=["first", "second", "third"])
def test_every_case_has_a_deploy_before_the_incident(build: Callable[[], GeneratedCase]) -> None:
    deployments = build().bundle.deployments

    assert any(d.deployed_at < INCIDENT.start_at for d in deployments)


def test_each_oracle_names_its_bundles_snapshot() -> None:
    for case in (first, second, third):
        assert case().oracle.snapshot_id == case().bundle.manifest.snapshot_id


# Case 1: a checkout deploy adds a stock check per cart item


def test_first_case_points_at_the_checkout_deploy() -> None:
    (expected,) = first().oracle.causes

    assert (expected.component, expected.category) == (
        "checkout-api",
        CauseCategory.DEPLOYMENT_REGRESSION,
    )


@pytest.mark.parametrize(
    ("service", "dependency"),
    [
        ("inventory", None),
        ("checkout-api", "inventory"),
        ("checkout-api", "payments"),
        ("payments", None),
    ],
)
def test_first_case_dependencies_stay_fast(service: str, dependency: str | None) -> None:
    assert unchanged(histogram(first().bundle, service, dependency))


def test_first_case_checkout_calls_inventory_more_often() -> None:
    calls = counter(first().bundle, "checkout-api", "request_count", "inventory")
    requests = counter(first().bundle, "checkout-api", "request_count")

    assert rate(calls, INCIDENT) >= 3 * rate(calls, BASELINE)
    assert rate(requests, INCIDENT) == pytest.approx(rate(requests, BASELINE), rel=0.1)


def test_first_case_spans_show_checkout_waiting_on_inventory() -> None:
    baseline, incident = first().bundle.spans

    assert (baseline.window, incident.window) == (BASELINE, INCIDENT)
    assert segment_p95(incident, "inventory") >= 5 * segment_p95(baseline, "inventory")
    assert segment_p95(incident, "payments") == pytest.approx(
        segment_p95(baseline, "payments"), rel=FLAT
    )
    assert segment_p95(incident, None) == pytest.approx(segment_p95(baseline, None), rel=FLAT)


def test_first_case_decoy_deploy_precedes_the_real_one() -> None:
    decoy, real = first().bundle.deployments

    assert (decoy.service, real.service) == ("payments", "checkout-api")
    assert decoy.deployed_at < real.deployed_at < INCIDENT.start_at
    assert real.deployed_at > BASELINE.end_at


def test_first_case_payments_errors_are_flat() -> None:
    errors = counter(first().bundle, "payments", "error_count")

    assert rate(errors, INCIDENT) < 1
    assert rate(errors, BASELINE) < 1


def test_first_case_warns_only_during_the_incident() -> None:
    warnings = [log for log in first().bundle.logs if log.severity is LogSeverity.WARN]

    assert warnings
    assert all(INCIDENT.start_at <= log.timestamp < INCIDENT.end_at for log in warnings)


# Case 2: payments degrades on its own, beside a harmless checkout deploy


def test_second_case_points_at_payments() -> None:
    (expected,) = second().oracle.causes

    assert (expected.component, expected.category) == (
        "payments",
        CauseCategory.DEPENDENCY_DEGRADATION,
    )


@pytest.mark.parametrize(
    ("service", "dependency"), [("payments", None), ("checkout-api", "payments")]
)
def test_second_case_payments_slows_down(service: str, dependency: str | None) -> None:
    series = histogram(second().bundle, service, dependency)

    assert p95(series, INCIDENT) >= 2 * p95(series, BASELINE)


@pytest.mark.parametrize(
    ("service", "dependency"), [("inventory", None), ("checkout-api", "inventory")]
)
def test_second_case_inventory_stays_fast(service: str, dependency: str | None) -> None:
    assert unchanged(histogram(second().bundle, service, dependency))


def test_second_case_payments_errors_rise() -> None:
    errors = counter(second().bundle, "payments", "error_count")

    assert rate(errors, INCIDENT) >= 10 * rate(errors, BASELINE)


def test_second_case_call_volume_is_unchanged_after_the_deploy() -> None:
    calls = counter(second().bundle, "checkout-api", "request_count", "inventory")

    assert rate(calls, INCIDENT) == pytest.approx(rate(calls, BASELINE), rel=0.1)


def test_second_case_spans_show_checkout_waiting_on_payments() -> None:
    baseline, incident = second().bundle.spans

    assert segment_p95(incident, "payments") >= 3 * segment_p95(baseline, "payments")
    assert segment_p95(incident, "inventory") == pytest.approx(
        segment_p95(baseline, "inventory"), rel=FLAT
    )


def test_second_case_only_deploy_is_checkouts_and_precedes_the_fault() -> None:
    (only,) = second().bundle.deployments

    assert only.service == "checkout-api"
    assert BASELINE.end_at < only.deployed_at < INCIDENT.start_at


def test_second_case_payments_logs_errors_during_the_incident() -> None:
    errors = [log for log in second().bundle.logs if log.severity is LogSeverity.ERROR]

    assert errors
    assert {log.service for log in errors} == {"payments"}
    assert all(INCIDENT.start_at <= log.timestamp < INCIDENT.end_at for log in errors)


# Case 3: the evidence that would decide it is missing


def test_third_case_is_unanswerable() -> None:
    assert third().oracle.answerable is False
    assert set(third().oracle.acceptable_outcomes) == {
        DiagnosticOutcome.INSUFFICIENT,
        DiagnosticOutcome.PARTIAL,
    }


def test_third_case_has_no_downstream_telemetry_during_the_incident() -> None:
    downstream = [
        series
        for series in third().bundle.metrics
        if series.dependency is not None or series.service != "checkout-api"
    ]

    assert len(downstream) == 6
    for series in downstream:
        assert points_in(series, INCIDENT) == 0
        assert points_in(series, BASELINE) == 30


def test_third_case_keeps_checkouts_own_telemetry() -> None:
    own = [
        series
        for series in third().bundle.metrics
        if series.service == "checkout-api" and series.dependency is None
    ]

    assert len(own) == 3
    assert all(points_in(series, INCIDENT) == 30 for series in own)


def test_third_case_has_no_traces_of_the_incident() -> None:
    assert [summary.window for summary in third().bundle.spans] == [BASELINE]


def test_third_case_explains_the_gap() -> None:
    messages = [log.message for log in third().bundle.logs]

    assert "Downstream metrics exporter is falling behind" in messages
