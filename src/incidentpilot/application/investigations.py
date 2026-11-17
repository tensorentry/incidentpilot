"""Start an investigation (spec §12.1, §12.2; errata E8 and E11).

Starting freezes the snapshot, scope and configuration, then queues the work. It's
idempotent like creating an incident, and a request that fails validation leaves
nothing behind: no key, no investigation and no event.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from incidentpilot.application.errors import Conflict, NotFound, Rejected, RequestError
from incidentpilot.application.idempotent import check_key, claim_or_replay
from incidentpilot.domain.canonical import sha256_digest
from incidentpilot.domain.enums import (
    AttemptStatus,
    EventType,
    InvestigationStatus,
    RunMode,
    TerminationReason,
)
from incidentpilot.domain.ids import IncidentId, InvestigationId, OwnerId
from incidentpilot.domain.investigation import (
    QUEUE_LIMIT,
    FrozenConfig,
    InvestigationLimits,
    InvestigationRecord,
    StartInvestigation,
)
from incidentpilot.domain.transitions import is_terminal_investigation
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.incidents import get_incident
from incidentpilot.persistence.investigations import get_investigation, insert_investigation
from incidentpilot.persistence.snapshots import get_snapshot
from incidentpilot.workflows.recovery import close_attempt, end_investigation

OPERATION = "start_investigation"
# Any fixed key works, as long as only the start path takes it (E11).
ADMISSION_LOCK = 3_141_592
# SQL reads keywords and names in any case, so short queries live at module level,
# out of reach of mutations that only flip their case.
_LOCK_ADMISSION = "SELECT pg_advisory_xact_lock(%s)"
_COUNT_QUEUED = "SELECT queued_investigation_count()"
_LOCK_FOR_CANCEL = "SELECT status, current_attempt_id FROM investigations WHERE id = %s FOR UPDATE"


@dataclass(frozen=True)
class StartedInvestigation:
    investigation: InvestigationRecord
    # False when this was a retry of an earlier request.
    created: bool


def fingerprint(owner_id: OwnerId, incident_id: IncidentId, request: StartInvestigation) -> str:
    return sha256_digest(
        {
            "operation": OPERATION,
            "owner_id": owner_id,
            "incident_id": incident_id,
            "request": request.model_dump(),
        }
    )


def admitted_mode(
    requested: RunMode, process_mode: RunMode
) -> Literal[RunMode.OFFLINE_DEMO, RunMode.LIVE]:
    """E8: a request runs in its process's mode, and replay never starts anything."""
    if requested is RunMode.REPLAY:
        raise Rejected("replay shows stored runs and never starts an investigation")
    if requested is not process_mode:
        raise Rejected(f"this process runs in {process_mode} mode, not {requested}")
    return RunMode.LIVE if requested is RunMode.LIVE else RunMode.OFFLINE_DEMO


async def start_investigation(
    connection: Connection,
    owner_id: OwnerId,
    process_mode: RunMode,
    incident_id: IncidentId,
    key: str,
    request: StartInvestigation,
) -> StartedInvestigation:
    check_key(key)
    mode = admitted_mode(request.mode, process_mode)
    request_sha256 = fingerprint(owner_id, incident_id, request)
    investigation_id = InvestigationId(uuid4())
    async with owner_transaction(connection, owner_id):
        earlier = await claim_or_replay(
            connection, OPERATION, key, request_sha256, investigation_id
        )
        if earlier is None:
            config = await _freeze(connection, incident_id, request, mode)
            await _admit(connection)
            await insert_investigation(connection, investigation_id, incident_id, config)
            await append_event(
                connection,
                investigation_id,
                EventType.INVESTIGATION_QUEUED,
                {
                    "incident_id": str(incident_id),
                    "snapshot_id": config.snapshot_id,
                    "mode": config.mode.value,
                    "config_sha256": config.digest,
                },
            )
        stored = await get_investigation(connection, InvestigationId(earlier or investigation_id))
        # Neither the key nor the investigation can be deleted, and both commit together.
        assert stored is not None  # noqa: S101
        return StartedInvestigation(stored, created=earlier is None)


async def _freeze(
    connection: Connection,
    incident_id: IncidentId,
    request: StartInvestigation,
    mode: Literal[RunMode.OFFLINE_DEMO, RunMode.LIVE],
) -> FrozenConfig:
    """§12.2: check the incident, snapshot, scope and window, then fix the configuration."""
    incident = await get_incident(connection, incident_id)
    if incident is None:
        raise NotFound(f"incident {incident_id} doesn't exist")
    snapshot = await get_snapshot(connection, request.snapshot_id)
    if snapshot is None:
        raise NotFound(f"snapshot {request.snapshot_id} doesn't exist")
    manifest = snapshot.manifest
    if incident.service not in manifest.service_graph.services:
        raise RequestError(f"snapshot {request.snapshot_id} has no service {incident.service}")
    if not manifest.observation_window.contains(request.time_window):
        raise RequestError(f"snapshot {request.snapshot_id} doesn't cover that window")
    return FrozenConfig(
        mode=mode,
        model_profile=request.model_profile,
        snapshot_id=request.snapshot_id,
        window=request.time_window,
        allowed_services=sorted(manifest.service_graph.services),
        limits=InvestigationLimits(),
    )


async def _admit(connection: Connection) -> None:
    """E11: serialize admission, then count queued work across every owner."""
    await connection.execute(_LOCK_ADMISSION, (ADMISSION_LOCK,))
    row = await (await connection.execute(_COUNT_QUEUED)).fetchone()
    assert row is not None  # noqa: S101 (count always returns one row)
    if row[0] >= QUEUE_LIMIT:
        raise Rejected(f"the queue already holds {QUEUE_LIMIT} investigations")


@dataclass(frozen=True)
class CancelledInvestigation:
    investigation: InvestigationRecord
    # False when it was already cancelled.
    changed: bool


async def cancel_investigation(
    connection: Connection, owner_id: OwnerId, investigation_id: InvestigationId
) -> CancelledInvestigation:
    """Cancel queued or running work (spec D09; erratum E9).

    The investigation row lock decides any race with publication: whichever commits
    first wins, and the other sees the final state. Cancelling twice is not an error;
    cancelling work that already completed or failed is a conflict.
    """
    async with owner_transaction(connection, owner_id):
        row = await (await connection.execute(_LOCK_FOR_CANCEL, (investigation_id,))).fetchone()
        if row is None:
            raise NotFound(f"investigation {investigation_id} doesn't exist")
        status, attempt_id = InvestigationStatus(row[0]), row[1]
        if status is not InvestigationStatus.CANCELLED:
            if is_terminal_investigation(status):
                raise Conflict(f"investigation {investigation_id} has already ended: {status}")
            if attempt_id is not None:
                await close_attempt(connection, attempt_id, AttemptStatus.CANCELLED)
            await end_investigation(
                connection,
                investigation_id,
                status,
                InvestigationStatus.CANCELLED,
                TerminationReason.CANCELLED,
                EventType.INVESTIGATION_CANCELLED,
            )
        stored = await get_investigation(connection, investigation_id)
        assert stored is not None  # noqa: S101 (locked above, and never deleted)
        return CancelledInvestigation(stored, changed=status is not InvestigationStatus.CANCELLED)
