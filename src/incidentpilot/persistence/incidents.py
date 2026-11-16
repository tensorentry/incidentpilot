"""Stored incidents. Run inside owner_transaction."""

from datetime import datetime

from incidentpilot.domain.enums import IncidentSeverity
from incidentpilot.domain.ids import IncidentId
from incidentpilot.domain.incident import CreateIncident, IncidentRecord
from incidentpilot.domain.window import TimeWindow
from incidentpilot.persistence.database import Connection

_SELECT = (
    "SELECT id, description, service, severity, window_start, window_end, sanitizer_version,"
    " created_at FROM incidents WHERE id = %s"
)


async def insert_incident(
    connection: Connection,
    incident_id: IncidentId,
    request: CreateIncident,
    description: str,
    sanitizer_version: str,
) -> IncidentRecord:
    """Store the request with its already-sanitized description."""
    cursor = await connection.execute(
        """
        INSERT INTO incidents (owner_id, id, description, service, severity, window_start,
                               window_end, sanitizer_version)
        VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s)
        RETURNING created_at
        """,
        (
            incident_id,
            description,
            request.service,
            request.severity,
            request.window.start_at,
            request.window.end_at,
            sanitizer_version,
        ),
    )
    row = await cursor.fetchone()
    assert row is not None  # noqa: S101 (INSERT ... RETURNING always returns the row)
    return _record(
        incident_id,
        description,
        request.service,
        request.severity,
        request.window,
        sanitizer_version,
        row[0],
    )


async def get_incident(connection: Connection, incident_id: IncidentId) -> IncidentRecord | None:
    row = await (await connection.execute(_SELECT, (incident_id,))).fetchone()
    if row is None:
        return None
    id_, description, service, severity, start_at, end_at, version, created_at = row
    return _record(
        IncidentId(id_),
        description,
        service,
        IncidentSeverity(severity),
        TimeWindow(start_at=start_at, end_at=end_at),
        version,
        created_at,
    )


def _record(
    incident_id: IncidentId,
    description: object,
    service: object,
    severity: IncidentSeverity,
    window: TimeWindow,
    sanitizer_version: object,
    created_at: object,
) -> IncidentRecord:
    assert isinstance(created_at, datetime)  # noqa: S101 (a timestamptz column)
    return IncidentRecord(
        id=incident_id,
        description=str(description),
        service=str(service),
        severity=severity,
        window=window,
        sanitizer_version=str(sanitizer_version),
        created_at=created_at,
    )
