"""The offline demo's worker: the scripted model behind the real worker loop (spec Phase 1).

`incidentpilot worker` runs this. It sweeps between claims, so a worker that comes up
after another one died recovers the dead one's investigation (§6.1). The live
investigator replaces the scripted model in T4.7.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from incidentpilot.domain.enums import RunMode
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.fence import Lease
from incidentpilot.workflows.lease import LeaseKeeper
from incidentpilot.workflows.recovery import sweep
from incidentpilot.workflows.scripted import DEMO_SCRIPT, ScriptedModel
from incidentpilot.workflows.worker import Worker

# Renew three times per lease, as the default 30-second lease renews every 10 seconds.
RENEWALS_PER_LEASE = 3


@dataclass
class Shift:
    """Counts a worker's jobs, and sets `stop` once `limit` of them have ended."""

    limit: int | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    jobs: int = 0

    def ended(self) -> None:
        self.jobs += 1
        if self.jobs == self.limit:
            self.stop.set()


def offline_worker(
    worker_id: str, shift: Shift, lease: timedelta, provider_delay: timedelta
) -> Worker:
    """A worker that plays DEMO_SCRIPT on offline_demo investigations, sweeping between
    claims. A job that fails still counts toward the shift."""
    model = ScriptedModel(DEMO_SCRIPT, provider_delay=provider_delay)

    async def job(connection: Connection, held: Lease, keeper: LeaseKeeper) -> None:
        try:
            await model(connection, held, keeper)
        finally:
            shift.ended()

    async def maintenance(connection: Connection) -> None:
        await sweep(connection)

    return Worker(
        worker_id,
        RunMode.OFFLINE_DEMO,
        job,
        maintenance=maintenance,
        lease_duration=lease,
        renew_every=lease / RENEWALS_PER_LEASE,
    )
