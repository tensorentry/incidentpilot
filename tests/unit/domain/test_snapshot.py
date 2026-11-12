import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from incidentpilot.domain.snapshot import ServiceGraph, SnapshotManifest

START = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
OBSERVED = {"start_at": START, "end_at": START + timedelta(hours=2)}
INCIDENT = {"start_at": START + timedelta(hours=1), "end_at": START + timedelta(minutes=90)}


def graph(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "services": ["checkout-api", "inventory", "payments"],
        "dependencies": [
            {"caller": "checkout-api", "callee": "inventory"},
            {"caller": "checkout-api", "callee": "payments"},
        ],
    }
    return fields | changes


def manifest(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": "1",
        "snapshot_id": "snap-3f9a",
        "adapter_version": "fixture-v1",
        "parent_snapshot_id": None,
        "observation_window": OBSERVED,
        "incident": {
            "service": "checkout-api",
            "window": INCIDENT,
            "summary": "Checkout requests are slow",
        },
        "service_graph": graph(),
    }
    return fields | changes


def parse_manifest(fields: dict[str, Any]) -> SnapshotManifest:
    return SnapshotManifest.model_validate_json(json.dumps(to_jsonable_python(fields)))


def parse_graph(fields: dict[str, Any]) -> ServiceGraph:
    return ServiceGraph.model_validate_json(json.dumps(fields))


def test_a_manifest_is_accepted() -> None:
    parsed = parse_manifest(manifest())

    assert parsed.incident.service == "checkout-api"


def test_a_snapshot_may_have_a_different_parent() -> None:
    parse_manifest(manifest(parent_snapshot_id="snap-0001"))


def test_a_snapshot_cannot_be_its_own_parent() -> None:
    with pytest.raises(ValidationError, match="a snapshot can't be its own parent"):
        parse_manifest(manifest(parent_snapshot_id="snap-3f9a"))


def test_the_incident_service_must_be_in_the_graph() -> None:
    incident = manifest()["incident"] | {"service": "search"}
    with pytest.raises(ValidationError, match="the incident's service must be in the graph"):
        parse_manifest(manifest(incident=incident))


@pytest.mark.parametrize(
    "window",
    [
        {"start_at": START - timedelta(minutes=1), "end_at": INCIDENT["end_at"]},
        {"start_at": INCIDENT["start_at"], "end_at": OBSERVED["end_at"] + timedelta(minutes=1)},
    ],
    ids=["starts-before", "ends-after"],
)
def test_telemetry_must_cover_the_incident(window: dict[str, datetime]) -> None:
    incident = manifest()["incident"] | {"window": window}
    with pytest.raises(ValidationError, match="telemetry must cover the incident window"):
        parse_manifest(manifest(incident=incident))


def test_the_incident_may_fill_the_observation_window() -> None:
    incident = manifest()["incident"] | {"window": OBSERVED}
    parse_manifest(manifest(incident=incident))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"services": ["inventory", "inventory"]}, "services must not repeat"),
        (
            {"dependencies": [{"caller": "checkout-api", "callee": "inventory"}] * 2},
            "dependencies must not repeat",
        ),
        (
            {"dependencies": [{"caller": "search", "callee": "inventory"}]},
            "a dependency must join two services in the graph",
        ),
        (
            {"dependencies": [{"caller": "checkout-api", "callee": "search"}]},
            "a dependency must join two services in the graph",
        ),
        (
            {"dependencies": [{"caller": "inventory", "callee": "inventory"}]},
            "a service can't depend on itself",
        ),
    ],
)
def test_the_graph_is_consistent(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_graph(graph(**changes))


def test_a_graph_may_have_no_dependencies() -> None:
    parse_graph(graph(services=["checkout-api"], dependencies=[]))


def test_allows_follows_edge_direction() -> None:
    parsed = parse_graph(graph())

    assert parsed.allows("checkout-api", "payments") is True
    assert parsed.allows("payments", "checkout-api") is False
    assert parsed.allows("inventory", "payments") is False


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2"},
        {"snapshot_id": "Snap-3F9A"},
        {"snapshot_id": "snap/3f9a"},
        {"adapter_version": ""},
        {"service_graph": graph(services=[])},
        {"oracle": {"cause": "deploy"}},
    ],
    ids=["schema", "upper-case-id", "path-in-id", "no-adapter", "no-services", "extra-field"],
)
def test_malformed_manifests_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_manifest(manifest(**changes))


def test_an_empty_summary_is_rejected() -> None:
    incident = manifest()["incident"] | {"summary": ""}
    with pytest.raises(ValidationError):
        parse_manifest(manifest(incident=incident))
