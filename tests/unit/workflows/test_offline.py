from datetime import timedelta

from incidentpilot.domain.enums import RunMode
from incidentpilot.workflows.offline import Shift, offline_worker
from incidentpilot.workflows.worker import no_maintenance


def test_a_shift_without_a_limit_never_stops() -> None:
    shift = Shift()
    for _ in range(3):
        shift.ended()

    assert (shift.jobs, shift.stop.is_set()) == (3, False)


def test_a_shift_stops_once_its_last_job_ends() -> None:
    shift = Shift(2)
    shift.ended()
    assert (shift.jobs, shift.stop.is_set()) == (1, False)

    shift.ended()
    assert (shift.jobs, shift.stop.is_set()) == (2, True)


def test_the_offline_worker_renews_three_times_per_lease() -> None:
    worker = offline_worker("w-1", Shift(), timedelta(seconds=6), timedelta(seconds=5))

    assert (worker.worker_id, worker.mode) == ("w-1", RunMode.OFFLINE_DEMO)
    assert (worker.lease_duration, worker.renew_every) == (
        timedelta(seconds=6),
        timedelta(seconds=2),
    )
    assert worker.maintenance is not no_maintenance
