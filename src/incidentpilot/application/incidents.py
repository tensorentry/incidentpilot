"""Create an incident, idempotently (spec §12.1, invariant 11).

A retry with the same owner, key and request returns the original incident. The same
key with a different request is refused. The key record and the incident commit in one
transaction, so neither exists without the other.
"""

from dataclasses import dataclass
from uuid import uuid4

from incidentpilot.application.errors import RequestError
from incidentpilot.application.idempotent import check_key, claim_or_replay
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.ids import IncidentId, OwnerId
from incidentpilot.domain.incident import MAX_DESCRIPTION, CreateIncident, IncidentRecord
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.incidents import get_incident, insert_incident
from incidentpilot.policies.redaction import SANITIZER_VERSION, sanitize

OPERATION = "create_incident"


@dataclass(frozen=True)
class CreatedIncident:
    incident: IncidentRecord
    # False when this was a retry of an earlier request.
    created: bool


def fingerprint(owner_id: OwnerId, request: CreateIncident) -> str:
    return sha256_digest(
        {"operation": OPERATION, "owner_id": owner_id, "request": request.model_dump()}
    )


async def create_incident(
    connection: Connection, owner_id: OwnerId, key: str, request: CreateIncident
) -> CreatedIncident:
    check_key(key)
    description = sanitize(request.description, limit=MAX_DESCRIPTION).text
    if not description.strip():
        raise RequestError("the description has no visible text")
    request_sha256 = fingerprint(owner_id, request)
    incident_id = IncidentId(uuid4())
    async with owner_transaction(connection, owner_id):
        earlier = await claim_or_replay(connection, OPERATION, key, request_sha256, incident_id)
        if earlier is None:
            incident = await insert_incident(
                connection, incident_id, request, description, SANITIZER_VERSION
            )
            return CreatedIncident(incident, created=True)
        original = await get_incident(connection, IncidentId(earlier))
        # Neither table allows DELETE, and both rows commit together.
        assert original is not None  # noqa: S101
        return CreatedIncident(original, created=False)
