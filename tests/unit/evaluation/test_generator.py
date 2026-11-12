from functools import cache
from pathlib import Path
from random import Random

import pytest

from incidentpilot.adapters.fixture_bundle import (
    HistogramSeries,
    ObservationBundle,
    ScalarSeries,
    SpanSummary,
)
from incidentpilot.domain.window import TimeWindow
from incidentpilot.evaluation.cases import (
    BASELINE,
    CASES,
    INCIDENT,
    GeneratedCase,
    snapshot_id_for,
    synchronous_dependency,
)
from incidentpilot.evaluation.generate import encode, main, write_case
from incidentpilot.evaluation.oracle import Oracle
from incidentpilot.evaluation.telemetry import LATENCY_BOUNDS_MS, latencies

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = REPO_ROOT / "scenarios"


@cache
def case(seed: int = 7) -> GeneratedCase:
    return synchronous_dependency(seed)


# Reproducibility (the acceptance test)


def test_the_same_seed_writes_identical_bytes() -> None:
    again = synchronous_dependency(7)

    assert encode(again.bundle) == encode(case().bundle)
    assert encode(again.oracle) == encode(case().oracle)


def test_a_different_seed_writes_different_telemetry() -> None:
    other = case(8)

    assert other.bundle.manifest.snapshot_id != case().bundle.manifest.snapshot_id
    assert other.bundle.metrics != case().bundle.metrics


def test_written_files_parse_back_to_the_same_case() -> None:
    assert ObservationBundle.model_validate_json(encode(case().bundle)) == case().bundle
    assert Oracle.model_validate_json(encode(case().oracle)) == case().oracle


def test_snapshot_ids_are_opaque_and_stable() -> None:
    snapshot_id = snapshot_id_for("synchronous-dependency", 7)

    assert snapshot_id == case().bundle.manifest.snapshot_id
    assert snapshot_id.startswith("snap-")
    assert "synchronous" not in snapshot_id
    assert snapshot_id != snapshot_id_for("synchronous-dependency", 8)
    assert snapshot_id != snapshot_id_for("other-family", 7)


def test_the_committed_files_match_a_fresh_run() -> None:
    expected = {}
    for build, seed in CASES:
        generated = build(seed)
        name = f"{generated.bundle.manifest.snapshot_id}.json"
        expected[f"observations/{name}"] = encode(generated.bundle)
        expected[f"oracle/{name}"] = encode(generated.oracle)
    committed = {
        str(path.relative_to(SCENARIOS)): path.read_bytes() for path in SCENARIOS.glob("*/*.json")
    }

    assert committed.keys() == expected.keys(), "run `make fixtures` and commit the result"
    for name, content in expected.items():
        assert committed[name] == content, f"{name} is stale: run `make fixtures`"


def test_main_writes_every_case(tmp_path: Path) -> None:
    assert main(["--root", str(tmp_path)]) == 0

    written = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.glob("*/*.json"))
    assert written == sorted(
        str(path.relative_to(SCENARIOS)) for path in SCENARIOS.glob("*/*.json")
    )


def test_write_case_pairs_a_bundle_with_its_oracle(tmp_path: Path) -> None:
    root = tmp_path / "new" / "scenarios"
    write_case(case(), root)
    write_case(case(), root)
    name = f"{case().bundle.manifest.snapshot_id}.json"

    assert (root / "observations" / name).read_bytes() == encode(case().bundle)
    assert (root / "oracle" / name).read_bytes() == encode(case().oracle)


def test_main_writes_to_scenarios_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0
    # Compared by name, because macOS paths ignore case.
    assert [path.name for path in tmp_path.iterdir()] == ["scenarios"]
    assert sorted(path.name for path in (tmp_path / "scenarios").iterdir()) == [
        "observations",
        "oracle",
    ]


def test_main_explains_itself(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "Write the committed cases" in capsys.readouterr().out


def test_latencies_count_every_sample_including_overflow() -> None:
    rng = Random(1)  # noqa: S311 - a fixed seed, not a secret
    series = latencies(rng, BASELINE, lambda t: 5000, lambda t: 50, service="checkout-api")
    point = series.points[0]

    assert point.overflow > 0
    assert sum(bucket.count for bucket in point.buckets) + point.overflow == 50
    assert [bucket.le for bucket in point.buckets] == list(LATENCY_BOUNDS_MS)


def test_latencies_put_fast_samples_in_the_first_bucket() -> None:
    rng = Random(1)  # noqa: S311 - a fixed seed, not a secret
    series = latencies(rng, BASELINE, lambda t: 1, lambda t: 50, service="checkout-api")

    assert series.points[0].buckets[0].count == 50
    assert series.points[0].overflow == 0


def test_the_oracle_names_the_bundles_snapshot() -> None:
    assert case().oracle.snapshot_id == case().bundle.manifest.snapshot_id
    assert case().oracle.answerable


# The signals case 1 is meant to carry (spec §14.1)


def p95(series: HistogramSeries, window: TimeWindow) -> float:
    """The bucket bound below which 95% of the window's samples fall."""
    points = [p for p in series.points if window.start_at <= p.t < window.end_at]
    totals = [sum(p.buckets[i].count for p in points) for i in range(len(points[0].buckets))]
    overflow = sum(p.overflow for p in points)
    target = 0.95 * (sum(totals) + overflow)
    running = 0
    for bucket, total in zip(points[0].buckets, totals, strict=True):
        running += total
        if running >= target:
            return bucket.le
    return float("inf")


def rate(series: ScalarSeries, window: TimeWindow) -> float:
    points = [p.v for p in series.points if window.start_at <= p.t < window.end_at]
    return sum(points) / len(points)


def histogram(service: str, dependency: str | None = None) -> HistogramSeries:
    for series in case().bundle.metrics:
        if (
            isinstance(series, HistogramSeries)
            and series.service == service
            and series.dependency == dependency
        ):
            return series
    raise LookupError(service)


def counter(service: str, metric: str, dependency: str | None = None) -> ScalarSeries:
    for series in case().bundle.metrics:
        if isinstance(series, ScalarSeries) and (
            series.service,
            series.metric,
            series.dependency,
        ) == (service, metric, dependency):
            return series
    raise LookupError(metric)


def segment_p95(summary: SpanSummary, downstream: str | None) -> float:
    return next(s.p95_ms for s in summary.segments if s.downstream == downstream)


def test_checkout_slows_down_during_the_incident() -> None:
    checkout = histogram("checkout-api")

    assert p95(checkout, INCIDENT) >= 2 * p95(checkout, BASELINE)


@pytest.mark.parametrize(
    ("service", "dependency"),
    [
        ("inventory", None),
        ("checkout-api", "inventory"),
        ("checkout-api", "payments"),
        ("payments", None),
    ],
    ids=["inventory-itself", "each-inventory-call", "each-payments-call", "payments-itself"],
)
def test_the_dependencies_stay_fast(service: str, dependency: str | None) -> None:
    series = histogram(service, dependency)

    assert p95(series, INCIDENT) == p95(series, BASELINE)


def test_checkout_calls_inventory_more_often() -> None:
    calls = counter("checkout-api", "request_count", "inventory")
    requests = counter("checkout-api", "request_count")

    assert rate(calls, INCIDENT) >= 3 * rate(calls, BASELINE)
    assert rate(requests, INCIDENT) == pytest.approx(rate(requests, BASELINE), rel=0.1)


def test_spans_show_checkout_waiting_on_inventory() -> None:
    baseline, incident = case().bundle.spans

    assert (baseline.window, incident.window) == (BASELINE, INCIDENT)
    assert segment_p95(incident, "inventory") >= 5 * segment_p95(baseline, "inventory")
    assert segment_p95(incident, "payments") == pytest.approx(
        segment_p95(baseline, "payments"), rel=0.15
    )
    assert segment_p95(incident, None) == pytest.approx(segment_p95(baseline, None), rel=0.15)


def test_a_decoy_deploy_precedes_the_real_one() -> None:
    decoy, real = case().bundle.deployments

    assert (decoy.service, real.service) == ("payments", "checkout-api")
    assert decoy.deployed_at < real.deployed_at < INCIDENT.start_at
    assert real.deployed_at > BASELINE.end_at


def test_payments_errors_are_flat() -> None:
    errors = counter("payments", "error_count")

    assert rate(errors, INCIDENT) < 1
    assert rate(errors, BASELINE) < 1


def test_warnings_appear_only_during_the_incident() -> None:
    warnings = [log for log in case().bundle.logs if log.severity == "warn"]

    assert warnings
    assert all(INCIDENT.start_at <= log.timestamp < INCIDENT.end_at for log in warnings)
