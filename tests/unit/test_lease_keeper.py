"""LeaseKeeper's checks that need no database."""

import re
from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest

from incidentpilot.domain.ids import AttemptId, InvestigationId, LeaseEpoch, OwnerId
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.fence import Lease, StaleLease
from incidentpilot.workflows.lease import LeaseKeeper

LEASE = Lease(
    OwnerId(UUID(int=1)), InvestigationId(UUID(int=2)), AttemptId(UUID(int=3)), LeaseEpoch(1)
)
NO_DATABASE = cast(Connection, None)


@pytest.mark.parametrize("every", [timedelta(seconds=30), timedelta(seconds=31)])
def test_renewal_must_come_before_expiry(every: timedelta) -> None:
    with pytest.raises(ValueError, match=r"^a lease must be renewed before it expires$"):
        LeaseKeeper(NO_DATABASE, LEASE, timedelta(seconds=30), every)


async def test_a_kept_lease_allows_dispatch() -> None:
    LeaseKeeper(NO_DATABASE, LEASE).require()


async def test_a_lost_lease_stops_dispatch() -> None:
    keeper = LeaseKeeper(NO_DATABASE, LEASE)
    keeper.lost.set()

    message = (
        f"attempt {LEASE.attempt_id} couldn't renew its lease on investigation"
        f" {LEASE.investigation_id}"
    )
    with pytest.raises(StaleLease, match=rf"^{re.escape(message)}$"):
        keeper.require()


async def test_a_stopped_keeper_returns_without_renewing() -> None:
    keeper = LeaseKeeper(NO_DATABASE, LEASE, timedelta(seconds=30), timedelta(seconds=10))
    keeper.stop()

    await keeper.run()
    assert not keeper.lost.is_set()
