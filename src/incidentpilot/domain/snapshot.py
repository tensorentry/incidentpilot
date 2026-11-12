"""Snapshot manifests: erratum E7. A snapshot is immutable once imported.

The manifest is what the API and the scope policy need: the incident as reported,
the window telemetry covers, and the service graph that bounds which services
and dependencies an investigation may query (§8.3).
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.ids import SnapshotId
from incidentpilot.domain.names import ServiceName
from incidentpilot.domain.window import TimeWindow

MAX_SERVICES = 50
MAX_TEXT = 500

Snapshot = Annotated[SnapshotId, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")]


class Dependency(Contract):
    caller: ServiceName
    callee: ServiceName


class ServiceGraph(Contract):
    services: Annotated[list[ServiceName], Field(min_length=1, max_length=MAX_SERVICES)]
    dependencies: Annotated[list[Dependency], Field(max_length=MAX_SERVICES * 4)]

    @model_validator(mode="after")
    def _edges_join_known_services(self) -> Self:
        if len(set(self.services)) != len(self.services):
            raise ValueError("services must not repeat")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must not repeat")
        known = set(self.services)
        for edge in self.dependencies:
            if edge.caller not in known or edge.callee not in known:
                raise ValueError("a dependency must join two services in the graph")
            if edge.caller == edge.callee:
                raise ValueError("a service can't depend on itself")
        return self

    def allows(self, caller: str, callee: str) -> bool:
        return Dependency(caller=caller, callee=callee) in self.dependencies


class Incident(Contract):
    """The incident as reported. Nothing here may hint at the cause (§14.1)."""

    service: ServiceName
    window: TimeWindow
    summary: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


class SnapshotManifest(Contract):
    schema_version: Literal["1"]
    snapshot_id: Snapshot
    adapter_version: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
    parent_snapshot_id: Snapshot | None
    observation_window: TimeWindow
    incident: Incident
    service_graph: ServiceGraph

    @model_validator(mode="after")
    def _incident_is_observed(self) -> Self:
        if self.incident.service not in self.service_graph.services:
            raise ValueError("the incident's service must be in the graph")
        if not self.observation_window.contains(self.incident.window):
            raise ValueError("telemetry must cover the incident window")
        if self.parent_snapshot_id == self.snapshot_id:
            raise ValueError("a snapshot can't be its own parent")
        return self
