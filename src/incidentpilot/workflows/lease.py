"""Keeping a lease (spec §6.1).

A claim gives a 30-second lease, renewed every 10 seconds. Renewal is a fenced write,
so it fails once another attempt holds the investigation. A worker that can't renew
stops dispatching new work: `LeaseKeeper.require` raises before each dispatch.
"""

import asyncio
import logging
from datetime import timedelta

import psycopg

from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.fence import Lease, StaleLease, fenced_transaction
from incidentpilot.workflows.claim import LEASE

RENEW_EVERY = timedelta(seconds=10)
_EXTEND = (
    "UPDATE investigations SET lease_expires_at = pg_catalog.clock_timestamp() + %s WHERE id = %s"
)

log = logging.getLogger(__name__)


async def renew_lease(connection: Connection, lease: Lease, duration: timedelta = LEASE) -> None:
    """Push the lease's expiry out to `duration` from now, if the lease is still current."""
    async with fenced_transaction(connection, lease):
        await connection.execute(_EXTEND, (duration, lease.investigation_id))


class LeaseKeeper:
    """Renews one lease on its own connection until stopped or the lease is lost.

    It needs its own connection: the worker's connection may be inside a transaction
    when a renewal falls due, and one connection can't run two transactions at once.
    """

    def __init__(
        self,
        connection: Connection,
        lease: Lease,
        duration: timedelta = LEASE,
        every: timedelta = RENEW_EVERY,
    ) -> None:
        if every >= duration:
            raise ValueError("a lease must be renewed before it expires")
        self.lease = lease
        self.lost = asyncio.Event()
        self._connection = connection
        self._duration = duration
        self._every = every
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not await self._stopped_within(self._every):
            try:
                await renew_lease(self._connection, self.lease, self._duration)
            except (StaleLease, psycopg.Error):
                log.warning("lost the lease on investigation %s", self.lease.investigation_id)
                self.lost.set()
                return

    def stop(self) -> None:
        """Stop renewing at the next wake-up, never in the middle of a renewal."""
        self._stop.set()

    def require(self) -> None:
        """Call before each dispatch: a lost lease means no new work may start."""
        if self.lost.is_set():
            raise StaleLease(
                f"attempt {self.lease.attempt_id} couldn't renew its lease on investigation"
                f" {self.lease.investigation_id}"
            )

    async def _stopped_within(self, interval: timedelta) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), interval.total_seconds())
        except TimeoutError:
            return False
        return True
