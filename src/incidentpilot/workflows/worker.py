"""The worker loop (spec §6.1, D08).

A worker repeatedly runs maintenance, such as the sweeper (T3.10), then claims one
investigation in its own mode and runs a job on it while a LeaseKeeper renews the
lease. A failing job doesn't stop the worker: its lease lapses, and recovery takes it
from there.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta

from incidentpilot.domain.enums import RunMode
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.fence import Lease
from incidentpilot.workflows.claim import LEASE, claim_next
from incidentpilot.workflows.lease import RENEW_EVERY, LeaseKeeper

type Job = Callable[[Connection, Lease, LeaseKeeper], Awaitable[None]]
type Maintenance = Callable[[Connection], Awaitable[None]]

log = logging.getLogger(__name__)


async def no_maintenance(connection: Connection) -> None:
    """The default between claims: nothing."""


@dataclass
class Worker:
    worker_id: str
    mode: RunMode
    job: Job
    maintenance: Maintenance = no_maintenance
    lease_duration: timedelta = LEASE
    renew_every: timedelta = RENEW_EVERY
    poll_interval: timedelta = timedelta(seconds=1)

    async def run_once(self, connection: Connection, keeper_connection: Connection) -> bool:
        """Claim and run one investigation. False if there was nothing to claim."""
        lease = await claim_next(connection, self.worker_id, self.mode, self.lease_duration)
        if lease is None:
            return False
        keeper = LeaseKeeper(keeper_connection, lease, self.lease_duration, self.renew_every)
        renewing = asyncio.create_task(keeper.run())
        try:
            await self.job(connection, lease, keeper)
        except Exception:
            log.exception("job failed on investigation %s", lease.investigation_id)
        finally:
            keeper.stop()
            await renewing
        return True

    async def run(
        self, connection: Connection, keeper_connection: Connection, stop: asyncio.Event
    ) -> None:
        """Work until `stop` is set, waiting `poll_interval` whenever the queue is empty."""
        while not stop.is_set():
            await self.maintenance(connection)
            if not await self.run_once(connection, keeper_connection):
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll_interval.total_seconds())
