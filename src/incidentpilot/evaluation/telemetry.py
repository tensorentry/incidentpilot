"""Building blocks for synthetic telemetry. Every draw comes from the caller's `Random`.

Only `Random.random`, `gauss` and `lognormvariate` are used, which are stable for a
given seed, so a seed always yields the same bundle.
"""

import math
from bisect import bisect_left
from collections.abc import Callable
from datetime import datetime, timedelta
from random import Random

from incidentpilot.adapters.fixture_bundle import (
    Bucket,
    HistogramPoint,
    HistogramSeries,
    ScalarPoint,
    ScalarSeries,
)
from incidentpilot.domain.enums import MeasurementUnit, MetricKind
from incidentpilot.domain.window import TimeWindow

RESOLUTION = timedelta(minutes=1)
LATENCY_BOUNDS_MS = (25, 50, 100, 200, 400, 800, 1600, 3200)
LATENCY_SPREAD = 0.35

Curve = Callable[[datetime], float]


def grid(window: TimeWindow, step: timedelta = RESOLUTION) -> list[datetime]:
    count = (window.end_at - window.start_at) // step
    return [window.start_at + index * step for index in range(count)]


def counts(
    rng: Random,
    window: TimeWindow,
    rate_per_minute: Curve,
    *,
    service: str,
    metric: str,
    dependency: str | None = None,
) -> ScalarSeries:
    """A counter whose per-minute values scatter around `rate_per_minute`."""
    points = []
    for t in grid(window):
        expected = rate_per_minute(t)
        value = max(0, round(rng.gauss(expected, math.sqrt(expected))))
        points.append(ScalarPoint(t=t, v=value))
    return ScalarSeries(
        kind=MetricKind.COUNTER,
        service=service,
        dependency=dependency,
        metric=metric,
        unit=MeasurementUnit.COUNT,
        resolution_seconds=int(RESOLUTION.total_seconds()),
        window=window,
        points=points,
    )


def latencies(
    rng: Random,
    window: TimeWindow,
    median_ms: Curve,
    samples_per_minute: Curve,
    *,
    service: str,
    dependency: str | None = None,
) -> HistogramSeries:
    """A latency histogram of log-normal draws around `median_ms`."""
    points = []
    for t in grid(window):
        tally = [0] * (len(LATENCY_BOUNDS_MS) + 1)
        mu = math.log(median_ms(t))
        for _ in range(round(samples_per_minute(t))):
            tally[bisect_left(LATENCY_BOUNDS_MS, rng.lognormvariate(mu, LATENCY_SPREAD))] += 1
        buckets = [Bucket(le=le, count=tally[i]) for i, le in enumerate(LATENCY_BOUNDS_MS)]
        points.append(HistogramPoint(t=t, buckets=buckets, overflow=tally[-1]))
    return HistogramSeries(
        kind=MetricKind.HISTOGRAM,
        service=service,
        dependency=dependency,
        metric="request_latency",
        unit=MeasurementUnit.MILLISECONDS,
        resolution_seconds=int(RESOLUTION.total_seconds()),
        window=window,
        points=points,
    )


def jitter(rng: Random, value: float, fraction: float = 0.05) -> float:
    """`value` moved by up to `fraction` either way, rounded to a tenth."""
    return round(value * (1 + fraction * (2 * rng.random() - 1)), 1)


def minute_within(rng: Random, window: TimeWindow) -> datetime:
    """A whole minute inside `window`, drawn uniformly."""
    minutes = grid(window)
    return minutes[int(rng.random() * len(minutes))]
