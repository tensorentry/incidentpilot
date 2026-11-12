import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from incidentpilot.adapters.fixture_bundle import (
    HistogramSeries,
    ObservationBundle,
    ScalarSeries,
    bundle_sha256,
)

START = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
END = START + timedelta(hours=2)
MINUTE = timedelta(minutes=1)


def window(start: datetime = START, end: datetime = END) -> dict[str, datetime]:
    return {"start_at": start, "end_at": end}


def manifest() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "snapshot_id": "snap-3f9a",
        "adapter_version": "fixture-v1",
        "parent_snapshot_id": None,
        "observation_window": window(),
        "incident": {
            "service": "checkout-api",
            "window": window(START + timedelta(hours=1), START + timedelta(minutes=90)),
            "summary": "Checkout requests are slow",
        },
        "service_graph": {
            "services": ["checkout-api", "inventory", "payments"],
            "dependencies": [
                {"caller": "checkout-api", "callee": "inventory"},
                {"caller": "checkout-api", "callee": "payments"},
            ],
        },
    }


def scalar(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "kind": "counter",
        "service": "checkout-api",
        "dependency": None,
        "metric": "request_count",
        "unit": "count",
        "resolution_seconds": 60,
        "window": window(),
        "points": [{"t": START, "v": 240}, {"t": START + MINUTE, "v": 250}],
    }
    return fields | changes


def histogram_point(t: datetime, bounds: tuple[float, ...] = (50, 100, 250)) -> dict[str, Any]:
    return {"t": t, "buckets": [{"le": le, "count": 10} for le in bounds], "overflow": 1}


def histogram(**changes: Any) -> dict[str, Any]:
    fields = scalar(
        kind="histogram",
        metric="request_latency",
        unit="ms",
        points=[histogram_point(START), histogram_point(START + 2 * MINUTE)],
    )
    return fields | changes


def log(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "record_id": "log-0001",
        "service": "checkout-api",
        "timestamp": START + timedelta(minutes=65),
        "severity": "warn",
        "message": "Upstream call slow",
        "attributes": {"route": "/checkout"},
    }
    return fields | changes


def deployment(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "record_id": "dep-0001",
        "service": "checkout-api",
        "revision": "r42",
        "previous_revision": "r41",
        "deployed_at": START + timedelta(minutes=55),
        "change_summary": "Refactor order validation",
    }
    return fields | changes


def span(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "record_id": "span-0001",
        "service": "checkout-api",
        "operation": "POST /checkout",
        "window": window(START + timedelta(hours=1), START + timedelta(minutes=90)),
        "span_count": 1200,
        "segments": [
            {"downstream": None, "p50_ms": 20, "p95_ms": 40},
            {"downstream": "inventory", "p50_ms": 300, "p95_ms": 700},
        ],
    }
    return fields | changes


def bundle(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "manifest": manifest(),
        "metrics": [scalar(), histogram(), scalar(dependency="payments", metric="error_count")],
        "logs": [log()],
        "deployments": [deployment()],
        "spans": [span()],
    }
    return fields | changes


def exactly(message: str) -> str:
    """Match the whole validation message, so a changed message fails the test."""
    return rf"Value error, {re.escape(message)} \["


def text(fields: dict[str, Any]) -> str:
    return json.dumps(to_jsonable_python(fields))


def parse(fields: dict[str, Any]) -> ObservationBundle:
    return ObservationBundle.model_validate_json(text(fields))


def test_a_bundle_is_accepted() -> None:
    parsed = parse(bundle())

    assert isinstance(parsed.metrics[0], ScalarSeries)
    assert isinstance(parsed.metrics[1], HistogramSeries)


def test_an_empty_bundle_is_accepted() -> None:
    parse(bundle(metrics=[], logs=[], deployments=[], spans=[]))


def test_a_bundle_round_trips_through_python() -> None:
    parsed = parse(bundle())

    assert ObservationBundle.model_validate(parsed.model_dump()) == parsed


# The snapshot digest (E7)


def test_the_digest_ignores_key_order_and_time_offsets() -> None:
    reordered = json.loads(text(bundle()))
    reordered = {key: reordered[key] for key in reversed(reordered)}
    reordered["logs"][0]["timestamp"] = "2026-11-04T19:05:00+01:00"

    assert bundle_sha256(ObservationBundle.model_validate_json(json.dumps(reordered))) == (
        bundle_sha256(parse(bundle()))
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"logs": [log(message="Upstream call slow!")]},
        {"metrics": [scalar(points=[{"t": START, "v": 241}])]},
        {"deployments": []},
        {"spans": [span(span_count=1201)]},
    ],
    ids=["log-text", "point-value", "removed-record", "span-count"],
)
def test_the_digest_changes_with_any_content(changes: dict[str, Any]) -> None:
    assert bundle_sha256(parse(bundle(**changes))) != bundle_sha256(parse(bundle()))


def test_the_digest_is_a_sha256_hex_string() -> None:
    digest = bundle_sha256(parse(bundle()))

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# Series


@pytest.mark.parametrize(
    ("points", "message"),
    [
        (
            [{"t": START + MINUTE, "v": 1}, {"t": START, "v": 1}],
            "points must be in time order without repeats",
        ),
        (
            [{"t": START, "v": 1}, {"t": START, "v": 2}],
            "points must be in time order without repeats",
        ),
        ([{"t": START - MINUTE, "v": 1}], "points must fall inside the series window"),
        ([{"t": END, "v": 1}], "points must fall inside the series window"),
        ([{"t": START + timedelta(seconds=30), "v": 1}], "points must sit on the resolution grid"),
    ],
    ids=["unordered", "repeated", "before-window", "at-window-end", "off-grid"],
)
def test_series_points_are_ordered_inside_their_window_and_aligned(
    points: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError, match=exactly(message)):
        parse(bundle(metrics=[scalar(points=points)]))


def test_the_last_point_on_the_grid_is_accepted() -> None:
    parse(bundle(metrics=[scalar(points=[{"t": END - MINUTE, "v": 1}])]))


def test_missing_points_are_allowed_as_gaps() -> None:
    parse(
        bundle(metrics=[scalar(points=[{"t": START, "v": 1}, {"t": START + 10 * MINUTE, "v": 1}])])
    )


def test_histogram_points_are_checked_like_scalar_points() -> None:
    late = histogram(points=[histogram_point(START + MINUTE), histogram_point(START)])
    with pytest.raises(
        ValidationError, match=exactly("points must be in time order without repeats")
    ):
        parse(bundle(metrics=[late]))


def test_histogram_bounds_must_increase() -> None:
    flat = histogram(points=[histogram_point(START, (50, 50, 250))])
    with pytest.raises(ValidationError, match="bucket bounds must increase"):
        parse(bundle(metrics=[flat]))


def test_every_histogram_point_shares_its_bounds() -> None:
    mixed = histogram(points=[histogram_point(START), histogram_point(START + MINUTE, (50, 100))])
    with pytest.raises(ValidationError, match="the same bucket bounds"):
        parse(bundle(metrics=[mixed]))


@pytest.mark.parametrize(
    "series",
    [
        histogram(points=[{"t": START, "v": 1}]),
        scalar(points=[histogram_point(START)]),
        scalar(kind="summary"),
        histogram(points=[histogram_point(START, (0, 50))]),
        histogram(points=[histogram_point(START) | {"overflow": -1}]),
        scalar(resolution_seconds=0),
        scalar(unit="percent"),
        scalar(points=[{"t": "2026-11-04T17:00:00", "v": 1}]),
    ],
    ids=[
        "histogram-with-scalars",
        "scalar-with-buckets",
        "unknown-kind",
        "zero-bound",
        "negative-overflow",
        "zero-resolution",
        "unknown-unit",
        "naive-time",
    ],
)
def test_malformed_series_are_rejected(series: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(bundle(metrics=[series]))


# Records against the manifest


@pytest.mark.parametrize(
    "changes",
    [
        {"metrics": [scalar(service="search")]},
        {"logs": [log(service="search")]},
        {"deployments": [deployment(service="search")]},
        {"spans": [span(service="search")]},
    ],
    ids=["series", "log", "deployment", "span"],
)
def test_every_service_must_be_in_the_graph(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="every record's service must be in the graph"):
        parse(bundle(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"metrics": [scalar(service="payments", dependency="checkout-api")]},
        {"metrics": [scalar(service="inventory", dependency="payments")]},
        {
            "spans": [
                span(
                    segments=[
                        {"downstream": "payments", "p50_ms": 1, "p95_ms": 2},
                        {"downstream": "search", "p50_ms": 1, "p95_ms": 2},
                    ]
                )
            ]
        },
    ],
    ids=["reversed-edge", "missing-edge", "span-segment"],
)
def test_every_dependency_must_be_an_edge(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="every dependency in a record must be an edge"):
        parse(bundle(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"metrics": [scalar(window=window(START - MINUTE, END), points=[])]},
        {"spans": [span(window=window(START, END + MINUTE))]},
    ],
    ids=["series", "span"],
)
def test_every_window_lies_inside_the_observation_window(changes: dict[str, Any]) -> None:
    with pytest.raises(
        ValidationError, match="every window must lie inside the observation window"
    ):
        parse(bundle(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"logs": [log(timestamp=START - MINUTE)]},
        {"logs": [log(timestamp=END)]},
        {"deployments": [deployment(deployed_at=END)]},
    ],
    ids=["log-before", "log-at-end", "deployment-at-end"],
)
def test_every_timestamp_lies_inside_the_observation_window(changes: dict[str, Any]) -> None:
    with pytest.raises(
        ValidationError, match="every timestamp must lie inside the observation window"
    ):
        parse(bundle(**changes))


def test_records_at_the_window_start_are_inside() -> None:
    parse(bundle(logs=[log(timestamp=START)], deployments=[deployment(deployed_at=START)]))


@pytest.mark.parametrize(
    "changes",
    [
        {"logs": [log(), log()]},
        {"deployments": [deployment(record_id="log-0001")]},
        {"spans": [span(record_id="dep-0001")]},
    ],
    ids=["two-logs", "log-and-deployment", "deployment-and-span"],
)
def test_record_ids_are_unique_across_the_bundle(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="record IDs must be unique within a bundle"):
        parse(bundle(**changes))


def test_span_p95_cannot_be_below_p50() -> None:
    segment = {"downstream": "inventory", "p50_ms": 300, "p95_ms": 299}
    with pytest.raises(ValidationError, match="p95 can't be below p50"):
        parse(bundle(spans=[span(segments=[segment])]))


def test_span_p95_may_equal_p50() -> None:
    parse(bundle(spans=[span(segments=[{"downstream": None, "p50_ms": 5, "p95_ms": 5}])]))


@pytest.mark.parametrize(
    "changes",
    [
        {"logs": [log(severity="critical")]},
        {"logs": [log(message="")]},
        {"deployments": [deployment(revision="r 42")]},
        {"spans": [span(segments=[])]},
        {"spans": [span(operation="")]},
        {"oracle": {"cause": "inventory call"}},
    ],
    ids=["severity", "empty-message", "revision", "no-segments", "no-operation", "oracle-field"],
)
def test_malformed_records_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(bundle(**changes))
