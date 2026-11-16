"""The lease fence (spec §6.1, §6.5; invariant 2).

Every durable write a worker makes runs inside `fenced_transaction`. It locks the
investigation row, then checks that the worker's attempt is still current, its epoch
unchanged, the investigation still running and the lease unexpired by database time.
If any check fails, nothing is written.

The row lock is held until commit, so a reclaim, which needs the same lock, can't slip
in between the check and the commit. Keep fenced transactions short: a lease can expire
during one, and the worker learns of it only at its next fenced transaction.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from incidentpilot.domain.ids import AttemptId, InvestigationId, LeaseEpoch, OwnerId
from incidentpilot.persistence.database import Connection, owner_transaction

# NULL comparisons make the whole expression NULL, which counts as stale.
_CHECK = """
SELECT current_attempt_id = %(attempt)s
   AND lease_epoch = %(epoch)s
   AND status IN ('RUNNING', 'VALIDATING')
   AND lease_expires_at > pg_catalog.clock_timestamp()
FROM investigations WHERE id = %(investigation)s
FOR UPDATE
"""


class StaleLease(Exception):
    """The worker no longer holds the lease, so it must stop writing."""


@dataclass(frozen=True)
class Lease:
    """What a worker holds after claiming an attempt."""

    owner_id: OwnerId
    investigation_id: InvestigationId
    attempt_id: AttemptId
    epoch: LeaseEpoch


@asynccontextmanager
async def fenced_transaction(connection: Connection, lease: Lease) -> AsyncIterator[Connection]:
    async with owner_transaction(connection, lease.owner_id):
        cursor = await connection.execute(
            _CHECK,
            {
                "attempt": lease.attempt_id,
                "epoch": lease.epoch,
                "investigation": lease.investigation_id,
            },
        )
        if await cursor.fetchone() != (True,):
            raise StaleLease(
                f"attempt {lease.attempt_id} no longer holds investigation"
                f" {lease.investigation_id} at epoch {lease.epoch}"
            )
        yield connection
