"""Rows most investigation tests start from: a snapshot and an incident it covers, and
investigations queued or claimed on top of them."""

import json
from dataclasses import dataclass
from pathlib import Path

from incidentpilot.application.incidents import create_incident
from incidentpilot.application.investigations import start_investigation
from incidentpilot.application.snapshots import import_file, read_bundle
from incidentpilot.domain.enums import RunMode
from incidentpilot.domain.ids import IncidentId, InvestigationId, OwnerId
from incidentpilot.domain.incident import CreateIncident
from incidentpilot.domain.investigation import StartInvestigation
from incidentpilot.domain.snapshot import SnapshotManifest
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.fence import Lease
from incidentpilot.workflows.claim import claim_next
from tests.integration.harness import OWNER_A

OBSERVATIONS = Path(__file__).resolve().parents[2] / "scenarios" / "observations"
FIXTURE = sorted(OBSERVATIONS.glob("*.json"))[0]
MANIFEST: SnapshotManifest = read_bundle(FIXTURE).manifest
OFFLINE = RunMode.OFFLINE_DEMO


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


async def queue(
    connection: Connection, owner: OwnerId = OWNER_A, n: int = 1, tag: str = "q"
) -> list[InvestigationId]:
    seeded = await seed(connection, owner, key=f"seed-{tag}")
    ids = []
    for i in range(n):
        started = await start_investigation(
            connection, owner, OFFLINE, seeded.incident_id, f"{tag}-{i}", start_request()
        )
        ids.append(started.investigation.id)
    return ids


async def claimed(connection: Connection, owner: OwnerId = OWNER_A, tag: str = "q") -> Lease:
    await queue(connection, owner, tag=tag)
    lease = await claim_next(connection, "crashed-worker", OFFLINE)
    assert lease is not None
    return lease


async def update(connection: Connection, owner: OwnerId, sql: str, *params: object) -> None:
    async with owner_transaction(connection, owner):
        await connection.execute(sql, params)


async def expire(connection: Connection, lease: Lease) -> None:
    await update(
        connection,
        lease.owner_id,
        "UPDATE investigations SET lease_expires_at = pg_catalog.clock_timestamp()"
        " - interval '1 second' WHERE id = %s",
        lease.investigation_id,
    )
