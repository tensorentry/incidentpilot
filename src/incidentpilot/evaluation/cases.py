"""Scenario cases (spec §14.1). Each one returns an observation bundle and its oracle.

Names here describe the cause, which is why they stay on the evaluator side: what
reaches the investigator is an opaque snapshot ID and telemetry. All three cases
share one incident report, service graph and set of series, so nothing but the
telemetry itself tells them apart.
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
from incidentpilot.evaluation.telemetry import (
    Curve,
    Presence,
    always,
    counts,
    jitter,
    latencies,
    minute_within,
)

START = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
OBSERVED = TimeWindow(start_at=START, end_at=START + timedelta(hours=2))
BASELINE = TimeWindow(start_at=START + timedelta(minutes=30), end_at=START + timedelta(hours=1))
INCIDENT = TimeWindow(start_at=START + timedelta(minutes=75), end_at=START + timedelta(minutes=105))
CHECKOUT = "checkout-api"
INVENTORY = "inventory"
PAYMENTS = "payments"
TRAFFIC = 240.0
SLOW_REQUEST = "Request exceeded latency budget"

LogLine = tuple[datetime, str, LogSeverity, str]


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    bundle: ObservationBundle
    oracle: Oracle


@dataclass(frozen=True, slots=True)
class Behaviour:
    """How each standard series moves over the window. Rates are per minute."""

    checkout_errors: Curve
    checkout_ms: Curve
    inventory_calls: Curve
    inventory_call_ms: Curve
    payments_call_ms: Curve
    inventory_ms: Curve
    payments_ms: Curve
    payments_errors: Curve
    downstream_visible: Presence = always


def snapshot_id_for(family: str, seed: int) -> SnapshotId:
    """Opaque, but stable for a family and seed."""
    return SnapshotId("snap-" + hashlib.sha256(f"{family}:{seed}".encode()).hexdigest()[:12])


def step(at: datetime, before: float, after: float) -> Curve:
    return lambda t: after if t >= at else before


def flat(value: float) -> Curve:
    return lambda t: value


def standard_metrics(rng: Random, b: Behaviour) -> list[ScalarSeries | HistogramSeries]:
    """Checkout's own series, its calls to each dependency, and each dependency's own."""
    seen = b.downstream_visible
    return [
        counts(rng, OBSERVED, flat(TRAFFIC), service=CHECKOUT, metric="request_count"),
        counts(rng, OBSERVED, b.checkout_errors, service=CHECKOUT, metric="error_count"),
        latencies(rng, OBSERVED, b.checkout_ms, flat(TRAFFIC), service=CHECKOUT),
        counts(
            rng,
            OBSERVED,
            b.inventory_calls,
            service=CHECKOUT,
            dependency=INVENTORY,
            metric="request_count",
            present=seen,
        ),
        latencies(
            rng,
            OBSERVED,
            b.inventory_call_ms,
            b.inventory_calls,
            service=CHECKOUT,
            dependency=INVENTORY,
            present=seen,
        ),
        latencies(
            rng,
            OBSERVED,
            b.payments_call_ms,
            flat(TRAFFIC),
            service=CHECKOUT,
            dependency=PAYMENTS,
            present=seen,
        ),
        latencies(
            rng, OBSERVED, b.inventory_ms, b.inventory_calls, service=INVENTORY, present=seen
        ),
        latencies(rng, OBSERVED, b.payments_ms, flat(TRAFFIC), service=PAYMENTS, present=seen),
        counts(
            rng, OBSERVED, b.payments_errors, service=PAYMENTS, metric="error_count", present=seen
        ),
    ]


def checkout_spans(
    rng: Random,
    record_id: str,
    window: TimeWindow,
    inventory_ms: tuple[float, float],
    payments_ms: tuple[float, float],
) -> SpanSummary:
    return SpanSummary(
        record_id=record_id,
        service=CHECKOUT,
        operation="POST /checkout",
        window=window,
        span_count=round(TRAFFIC * 30),
        segments=[
            SpanSegment(downstream=None, p50_ms=jitter(rng, 55), p95_ms=jitter(rng, 100)),
            SpanSegment(
                downstream=INVENTORY,
                p50_ms=jitter(rng, inventory_ms[0]),
                p95_ms=jitter(rng, inventory_ms[1]),
            ),
            SpanSegment(
                downstream=PAYMENTS,
                p50_ms=jitter(rng, payments_ms[0]),
                p95_ms=jitter(rng, payments_ms[1]),
            ),
        ],
    )


def logs_with(extra: list[LogLine]) -> list[LogRecord]:
    """Routine health checks from every service, plus the case's own lines."""
    routine: list[LogLine] = [
        (
            START + timedelta(minutes=5 + 15 * index),
            service,
            LogSeverity.INFO,
            "Health check passed",
        )
        for index in range(8)
        for service in (CHECKOUT, INVENTORY, PAYMENTS)
    ]
    return [
        LogRecord(
            record_id=f"log-{index:04d}",
            service=service,
            timestamp=t,
            severity=severity,
            message=message,
            attributes={"route": "/checkout"} if service == CHECKOUT else {},
        )
        for index, (t, service, severity, message) in enumerate(sorted(routine + extra), start=1)
    ]


def deployment(
    record_id: str, service: str, revision: int, at: datetime, change_summary: str
) -> Deployment:
    return Deployment(
        record_id=record_id,
        service=service,
        revision=f"r{revision}",
        previous_revision=f"r{revision - 1}",
        deployed_at=at,
        change_summary=change_summary,
    )


def bundle(
    snapshot_id: SnapshotId,
    metrics: list[ScalarSeries | HistogramSeries],
    logs: list[LogRecord],
    deployments: list[Deployment],
    spans: list[SpanSummary],
) -> ObservationBundle:
    return ObservationBundle(
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
        deployments=deployments,
        spans=spans,
    )


def oracle(
    snapshot_id: SnapshotId,
    causes: list[OracleCause],
    acceptable: list[DiagnosticOutcome],
    tests: list[str],
) -> Oracle:
    return Oracle(
        schema_version="1",
        snapshot_id=snapshot_id,
        causes=causes,
        acceptable_outcomes=acceptable,
        discriminating_tests=tests,
    )


def cause(component: str, category: CauseCategory) -> OracleCause:
    return OracleCause(component=component, category=category, role=CausalRole.TRIGGER)


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
    metrics = standard_metrics(
        rng,
        Behaviour(
            checkout_errors=step(deployed_at, 0.5, 0.6),
            checkout_ms=step(deployed_at, 110, 430),
            inventory_calls=step(deployed_at, TRAFFIC, TRAFFIC * 4),
            inventory_call_ms=step(deployed_at, 35, 36),
            payments_call_ms=flat(70),
            inventory_ms=step(deployed_at, 30, 31),
            payments_ms=flat(60),
            payments_errors=flat(0.2),
        ),
    )
    slow = [minute_within(rng, INCIDENT) for _ in range(12)]
    logs = logs_with(
        [(decoy_at, PAYMENTS, LogSeverity.INFO, "Loaded currency rounding rules")]
        + [(t, CHECKOUT, LogSeverity.WARN, SLOW_REQUEST) for t in slow]
    )
    deployments = [
        deployment("dep-0001", PAYMENTS, 18, decoy_at, "Update currency rounding rules"),
        deployment(
            "dep-0002",
            CHECKOUT,
            42,
            deployed_at,
            "Check stock for each cart item before confirming the order",
        ),
    ]
    spans = [
        checkout_spans(rng, "span-0001", BASELINE, (14, 30), (65, 130)),
        checkout_spans(rng, "span-0002", INCIDENT, (150, 320), (65, 130)),
    ]
    return GeneratedCase(
        bundle=bundle(snapshot_id, metrics, logs, deployments, spans),
        oracle=oracle(
            snapshot_id,
            [cause(CHECKOUT, CauseCategory.DEPLOYMENT_REGRESSION)],
            [DiagnosticOutcome.DIAGNOSED, DiagnosticOutcome.PARTIAL],
            [
                "Checkout time waiting on inventory rises after the checkout deploy",
                "Checkout calls inventory more often per request, at unchanged per-call latency",
                "Payments latency and errors are unchanged after the payments deploy",
            ],
        ),
    )


def dependency_degradation(seed: int) -> GeneratedCase:
    """Case 2 (`case-4K8` in §14.1): payments degrades on its own, with no deploy.

    A harmless checkout deploy lands minutes earlier, so the obvious story is wrong.
    Payments' own latency and errors rise, checkout's calls to it slow down, and
    inventory is untouched. Checkout's call volume to each dependency doesn't change.
    """
    rng = Random(seed)  # noqa: S311 - reproducible synthetic data, never a secret
    snapshot_id = snapshot_id_for("dependency-degradation", seed)
    decoy_at = START + timedelta(minutes=68)
    degraded_at = START + timedelta(minutes=72)
    metrics = standard_metrics(
        rng,
        Behaviour(
            checkout_errors=step(degraded_at, 0.5, 4),
            checkout_ms=step(degraded_at, 110, 380),
            inventory_calls=flat(TRAFFIC),
            inventory_call_ms=flat(35),
            payments_call_ms=step(degraded_at, 70, 300),
            inventory_ms=flat(30),
            payments_ms=step(degraded_at, 60, 280),
            payments_errors=step(degraded_at, 0.2, 6),
        ),
    )
    timeouts = [minute_within(rng, INCIDENT) for _ in range(15)]
    slow = [minute_within(rng, INCIDENT) for _ in range(10)]
    logs = logs_with(
        [(decoy_at, CHECKOUT, LogSeverity.INFO, "Loaded order email templates")]
        + [(t, PAYMENTS, LogSeverity.ERROR, "Card processor request timed out") for t in timeouts]
        + [(t, CHECKOUT, LogSeverity.WARN, SLOW_REQUEST) for t in slow]
    )
    deployments = [
        deployment(
            "dep-0001", CHECKOUT, 42, decoy_at, "Update the order confirmation email template"
        )
    ]
    spans = [
        checkout_spans(rng, "span-0001", BASELINE, (14, 30), (65, 130)),
        checkout_spans(rng, "span-0002", INCIDENT, (14, 30), (260, 560)),
    ]
    return GeneratedCase(
        bundle=bundle(snapshot_id, metrics, logs, deployments, spans),
        oracle=oracle(
            snapshot_id,
            [cause(PAYMENTS, CauseCategory.DEPENDENCY_DEGRADATION)],
            [DiagnosticOutcome.DIAGNOSED, DiagnosticOutcome.PARTIAL],
            [
                "Payments' own latency and errors rise without a payments deploy",
                "Checkout's wait on payments rises while its wait on inventory is unchanged",
                "Checkout's call volume to each dependency is unchanged after its deploy",
            ],
        ),
    )


def missing_dependency_telemetry(seed: int) -> GeneratedCase:
    """Case 3 (`case-9J1` in §14.1): the evidence that would decide it is missing.

    Checkout slows after a deploy that touches every outbound call, but dependency
    telemetry stops shortly before the incident and no traces cover it. Nothing
    visible separates a checkout regression from a slow inventory or payments, and
    the fixture records no cause at all: what happened is in the missing minutes.
    The right answer is to abstain, or report partially with the missing tests named.
    """
    rng = Random(seed)  # noqa: S311 - reproducible synthetic data, never a secret
    snapshot_id = snapshot_id_for("missing-dependency-telemetry", seed)
    deployed_at = START + timedelta(minutes=68)
    slowed_at = START + timedelta(minutes=70)
    lost_at = START + timedelta(minutes=65)
    metrics = standard_metrics(
        rng,
        Behaviour(
            checkout_errors=flat(0.5),
            checkout_ms=step(slowed_at, 110, 400),
            inventory_calls=flat(TRAFFIC),
            inventory_call_ms=flat(35),
            payments_call_ms=flat(70),
            inventory_ms=flat(30),
            payments_ms=flat(60),
            payments_errors=flat(0.2),
            downstream_visible=lambda t: t < lost_at,
        ),
    )
    slow = [minute_within(rng, INCIDENT) for _ in range(12)]
    logs = logs_with(
        [(lost_at, CHECKOUT, LogSeverity.WARN, "Downstream metrics exporter is falling behind")]
        + [(t, CHECKOUT, LogSeverity.WARN, SLOW_REQUEST) for t in slow]
    )
    deployments = [
        deployment("dep-0001", CHECKOUT, 42, deployed_at, "Upgrade the shared HTTP client library")
    ]
    spans = [checkout_spans(rng, "span-0001", BASELINE, (14, 30), (65, 130))]
    return GeneratedCase(
        bundle=bundle(snapshot_id, metrics, logs, deployments, spans),
        oracle=oracle(
            snapshot_id,
            [],
            [DiagnosticOutcome.INSUFFICIENT, DiagnosticOutcome.PARTIAL],
            [
                "Dependency latency during the incident window",
                "Span timings for checkout during the incident window",
            ],
        ),
    )


# The committed cases: regenerate them with `make fixtures`.
CASES: list[tuple[Callable[[int], GeneratedCase], int]] = [
    (synchronous_dependency, 7),
    (dependency_degradation, 7),
    (missing_dependency_telemetry, 7),
]
