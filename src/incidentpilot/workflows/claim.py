"""Claim the next investigation (spec §6.1, §6.5; errata E8, E11 and E19).

One short transaction, committed before any external call:

1. lock the oldest eligible investigation in this worker's mode, whoever owns it;
2. adopt its owner for the rest of the transaction;
3. create the attempt and bind a free capacity slot, investigation first, then slot;
4. advance the epoch, set the lease by database time, mark it RUNNING and append
   ATTEMPT_STARTED.

If no slot is free, the whole transaction rolls back and nothing changes.
"""

import re
from datetime import timedelta
from uuid import uuid4

from incidentpilot.domain.enums import EventType, InvestigationStatus, RunMode
from incidentpilot.domain.ids import AttemptId, InvestigationId, LeaseEpoch, OwnerId
from incidentpilot.domain.transitions import require_investigation_transition
from incidentpilot.persistence.database import Connection, adopt_owner, scheduler_transaction
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease

# §6.1: a 30-second lease, renewed every 10 seconds.
LEASE = timedelta(seconds=30)
WORKER_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_PICK = "SELECT owner_id, investigation_id FROM claimable_investigation(%s)"
_CURRENT = "SELECT status, lease_epoch, attempts_started FROM investigations WHERE id = %s"
_BIND = "SELECT bind_capacity_slot(%s, %s)"


class _NoCapacity(Exception):
    """Every slot is taken; raised to roll the claim back."""


async def claim_next(
    connection: Connection, worker_id: str, mode: RunMode, lease: timedelta = LEASE
) -> Lease | None:
    """Claim one investigation for this worker, or return None if there's nothing to do."""
    if not WORKER_ID.fullmatch(worker_id):
        raise ValueError("a worker ID is 1 to 128 letters, digits or . _ : -")
    try:
        async with scheduler_transaction(connection):
            picked = await (await connection.execute(_PICK, (mode,))).fetchone()
            if picked is None:
                return None
            owner_id, investigation_id = OwnerId(picked[0]), InvestigationId(picked[1])
            await adopt_owner(connection, owner_id)
            return await _start_attempt(connection, owner_id, investigation_id, worker_id, lease)
    except _NoCapacity:
        return None


async def _start_attempt(
    connection: Connection,
    owner_id: OwnerId,
    investigation_id: InvestigationId,
    worker_id: str,
    lease: timedelta,
) -> Lease:
    row = await (await connection.execute(_CURRENT, (investigation_id,))).fetchone()
    assert row is not None  # noqa: S101 (claimable_investigation just locked it)
    status, epoch, started = InvestigationStatus(row[0]), LeaseEpoch(row[1] + 1), row[2] + 1
    require_investigation_transition(status, InvestigationStatus.RUNNING)
    attempt_id = AttemptId(uuid4())
    await connection.execute(
        """
        INSERT INTO attempts (owner_id, id, investigation_id, attempt_no, worker_id, lease_epoch)
        VALUES (current_owner_id(), %s, %s, %s, %s, %s)
        """,
        (attempt_id, investigation_id, started, worker_id, epoch),
    )
    slot = (await (await connection.execute(_BIND, (attempt_id, epoch))).fetchone() or (None,))[0]
    if slot is None:
        raise _NoCapacity
    # The deadline is set once, at the first claim, and recovery never moves it (§3.2).
    await connection.execute(
        """
        UPDATE investigations
        SET status = 'RUNNING', current_attempt_id = %s, lease_epoch = %s,
            lease_expires_at = pg_catalog.clock_timestamp() + %s, attempts_started = %s,
            deadline_at = coalesce(
                deadline_at, pg_catalog.clock_timestamp() + make_interval(secs => deadline_seconds)
            ),
            row_version = row_version + 1
        WHERE id = %s
        """,
        (attempt_id, epoch, lease, started, investigation_id),
    )
    await append_event(
        connection,
        investigation_id,
        EventType.ATTEMPT_STARTED,
        {
            "attempt_id": str(attempt_id),
            "attempt_no": started,
            "worker_id": worker_id,
            "lease_epoch": epoch,
            "slot": slot,
        },
    )
    return Lease(owner_id, investigation_id, attempt_id, epoch)
