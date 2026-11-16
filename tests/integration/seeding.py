"""Rows most investigation tests start from: a snapshot and an incident it covers."""

import json
from dataclasses import dataclass
from pathlib import Path

from incidentpilot.application.incidents import create_incident
from incidentpilot.application.snapshots import import_file, read_bundle
from incidentpilot.domain.ids import IncidentId, OwnerId
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.domain.investigation import StartInvestigation
from incidentpilot.domain.snapshot import SnapshotManifest
from incidentpilot.persistence.database import Connection

OBSERVATIONS = Path(__file__).resolve().parents[2] / "scenarios" / "observations"
FIXTURE = sorted(OBSERVATIONS.glob("*.json"))[0]
MANIFEST: SnapshotManifest = read_bundle(FIXTURE).manifest


@dataclass(frozen=True)
class Seeded:
    incident_id: IncidentId
    manifest: SnapshotManifest


def incident_request(service: str | None = None) -> CreateIncident:
    reported = MANIFEST.incident
    fields = {
        "description": reported.summary,
        "service": service or reported.service,
        "severity": "sev2",
        "window": reported.window.model_dump(mode="json"),
    }
    return CreateIncident.model_validate_json(json.dumps(fields))


def start_request(**changes: object) -> StartInvestigation:
    fields: dict[str, object] = {
        "mode": "offline_demo",
        "snapshot_id": MANIFEST.snapshot_id,
        "time_window": MANIFEST.incident.window.model_dump(mode="json"),
        "model_profile": "single-investigator-default",
    }
    return StartInvestigation.model_validate_json(json.dumps(fields | changes))


async def seed(
    connection: Connection, owner: OwnerId, key: str = "seed", service: str | None = None
) -> Seeded:
    await import_file(connection, owner, FIXTURE)
    created = await create_incident(connection, owner, key, incident_request(service))
    return Seeded(created.incident.id, MANIFEST)
