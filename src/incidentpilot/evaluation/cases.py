"""Scenario cases (spec §14.1). Each one returns an observation bundle and its oracle.

Names here describe the cause, which is why they stay on the evaluator side: what
reaches the investigator is an opaque snapshot ID and telemetry.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random

from incidentpilot.adapters.fixture_bundle import (
    Deployment,
    HistogramSeries,
    LogRecord,
    ObservationBundle,
    ScalarSeries,
    SpanSegment,
    SpanSummary,
)
from incidentpilot.domain.enums import CausalRole, CauseCategory, DiagnosticOutcome, LogSeverity
from incidentpilot.domain.ids import SnapshotId
from incidentpilot.domain.snapshot import (
    Dependency,
    Incident,
    ServiceGraph,
    SnapshotManifest,
)
from incidentpilot.domain.window import TimeWindow
from incidentpilot.evaluation.oracle import Oracle, OracleCause
from incidentpilot.evaluation.telemetry import counts, jitter, latencies, minute_within

START = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
OBSERVED = TimeWindow(start_at=START, end_at=START + timedelta(hours=2))
BASELINE = TimeWindow(start_at=START + timedelta(minutes=30), end_at=START + timedelta(hours=1))
INCIDENT = TimeWindow(start_at=START + timedelta(minutes=75), end_at=START + timedelta(minutes=105))
CHECKOUT = "checkout-api"
INVENTORY = "inventory"
PAYMENTS = "payments"


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    bundle: ObservationBundle
    oracle: Oracle


def snapshot_id_for(family: str, seed: int) -> SnapshotId:
    """Opaque, but stable for a family and seed."""
    return SnapshotId("snap-" + hashlib.sha256(f"{family}:{seed}".encode()).hexdigest()[:12])


def synchronous_dependency(seed: int) -> GeneratedCase:
    """Case 1 (`case-7Q2` in §14.1): a checkout deploy adds a stock check per cart item.

    Checkout now waits on inventory several times per request. Inventory itself stays
    healthy: each call is as fast as before, there are just more of them. An unrelated
    payments deploy earlier in the window is the decoy. Span timings and dependency
    call volume discriminate; the deploy's timing alone does not prove the cause.
    """
    rng = Random(seed)  # noqa: S311 - reproducible synthetic data, never a secret
    snapshot_id = snapshot_id_for("synchronous-dependency", seed)
    deployed_at = START + timedelta(minutes=70)
    decoy_at = START + timedelta(minutes=40)

    def changed(before: float, after: float) -> Callable[[datetime], float]:
        return lambda t: after if t >= deployed_at else before

    traffic = 240.0
    metrics: list[ScalarSeries | HistogramSeries] = [
        counts(rng, OBSERVED, changed(traffic, traffic), service=CHECKOUT, metric="request_count"),
        counts(rng, OBSERVED, changed(0.5, 0.6), service=CHECKOUT, metric="error_count"),
        latencies(rng, OBSERVED, changed(110, 430), changed(traffic, traffic), service=CHECKOUT),
        counts(
            rng,
            OBSERVED,
            changed(traffic, traffic * 4),
            service=CHECKOUT,
            dependency=INVENTORY,
            metric="request_count",
        ),
        latencies(
            rng,
            OBSERVED,
            changed(35, 36),
            changed(traffic, traffic * 4),
            service=CHECKOUT,
            dependency=INVENTORY,
        ),
        latencies(
            rng,
            OBSERVED,
            changed(70, 70),
            changed(traffic, traffic),
            service=CHECKOUT,
            dependency=PAYMENTS,
        ),
        latencies(rng, OBSERVED, changed(30, 31), changed(traffic, traffic * 4), service=INVENTORY),
        latencies(rng, OBSERVED, changed(60, 60), changed(traffic, traffic), service=PAYMENTS),
        counts(rng, OBSERVED, changed(0.2, 0.2), service=PAYMENTS, metric="error_count"),
    ]

    def spans(record_id: str, window: TimeWindow, inventory_ms: tuple[float, float]) -> SpanSummary:
        return SpanSummary(
            record_id=record_id,
            service=CHECKOUT,
            operation="POST /checkout",
            window=window,
            span_count=round(traffic * 30),
            segments=[
                SpanSegment(downstream=None, p50_ms=jitter(rng, 55), p95_ms=jitter(rng, 100)),
                SpanSegment(
                    downstream=INVENTORY,
                    p50_ms=jitter(rng, inventory_ms[0]),
                    p95_ms=jitter(rng, inventory_ms[1]),
                ),
                SpanSegment(downstream=PAYMENTS, p50_ms=jitter(rng, 65), p95_ms=jitter(rng, 130)),
            ],
        )

    slow = [minute_within(rng, INCIDENT) for _ in range(12)]
    routine = [
        (START + timedelta(minutes=5 + 15 * index), service)
        for index in range(8)
        for service in (CHECKOUT, INVENTORY, PAYMENTS)
    ]
    log_lines = sorted(
        [(t, service, LogSeverity.INFO, "Health check passed") for t, service in routine]
        + [(decoy_at, PAYMENTS, LogSeverity.INFO, "Loaded currency rounding rules")]
        + [(t, CHECKOUT, LogSeverity.WARN, "Request exceeded latency budget") for t in slow]
    )
    logs = [
        LogRecord(
            record_id=f"log-{index:04d}",
            service=service,
            timestamp=t,
            severity=severity,
            message=message,
            attributes={"route": "/checkout"} if service == CHECKOUT else {},
        )
        for index, (t, service, severity, message) in enumerate(log_lines, start=1)
    ]
    bundle = ObservationBundle(
        manifest=SnapshotManifest(
            schema_version="1",
            snapshot_id=snapshot_id,
            adapter_version="fixture-v1",
            parent_snapshot_id=None,
            observation_window=OBSERVED,
            incident=Incident(
                service=CHECKOUT, window=INCIDENT, summary="Checkout requests are slow"
            ),
            service_graph=ServiceGraph(
                services=[CHECKOUT, INVENTORY, PAYMENTS],
                dependencies=[
                    Dependency(caller=CHECKOUT, callee=INVENTORY),
                    Dependency(caller=CHECKOUT, callee=PAYMENTS),
                ],
            ),
        ),
        metrics=metrics,
        logs=logs,
        deployments=[
            Deployment(
                record_id="dep-0001",
                service=PAYMENTS,
                revision="r18",
                previous_revision="r17",
                deployed_at=decoy_at,
                change_summary="Update currency rounding rules",
            ),
            Deployment(
                record_id="dep-0002",
                service=CHECKOUT,
                revision="r42",
                previous_revision="r41",
                deployed_at=deployed_at,
                change_summary="Check stock for each cart item before confirming the order",
            ),
        ],
        spans=[spans("span-0001", BASELINE, (14, 30)), spans("span-0002", INCIDENT, (150, 320))],
    )
    oracle = Oracle(
        schema_version="1",
        snapshot_id=snapshot_id,
        causes=[
            OracleCause(
                component=CHECKOUT,
                category=CauseCategory.DEPLOYMENT_REGRESSION,
                role=CausalRole.TRIGGER,
            )
        ],
        acceptable_outcomes=[DiagnosticOutcome.DIAGNOSED, DiagnosticOutcome.PARTIAL],
        discriminating_tests=[
            "Checkout time waiting on inventory rises after the checkout deploy",
            "Checkout calls inventory more often per request, at unchanged per-call latency",
            "Payments latency and errors are unchanged after the payments deploy",
        ],
    )
    return GeneratedCase(bundle=bundle, oracle=oracle)


# The committed cases: regenerate them with `make fixtures`.
CASES: list[tuple[Callable[[int], GeneratedCase], int]] = [(synchronous_dependency, 7)]
