"""`incidentpilot worker` and the kill/restart demo (spec Phase 1 exit, §6.3)."""

import asyncio
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from incidentpilot import cli
from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.settings import load_settings
from incidentpilot.workflows.claim import claim_next
from tests.integration.harness import OWNER_A, ScratchDatabase
from tests.integration.seeding import OFFLINE, expire, queue

DEMO = Path(__file__).parents[2] / "scripts" / "kill_restart_demo.py"


@pytest.fixture
def connected(database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch) -> ScratchDatabase:
    monkeypatch.setenv("INCIDENTPILOT_DATABASE_URL", database.conninfo("runtime"))
    monkeypatch.setenv("INCIDENTPILOT_MODE", "offline_demo")
    return database


async def work(*argv: str) -> Any:
    """Run the worker command in this event loop, so a worker that never stops times out."""
    arguments = cli.build_parser().parse_args(["worker", *argv])
    return await asyncio.wait_for(cli.run(load_settings(), arguments), timeout=20)


async def outcome(connection: Connection, investigation_id: InvestigationId) -> list[object]:
    """The investigation's status, its attempts in order, and how many reports it has."""
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute(
            "SELECT i.status,"
            " (SELECT array_agg(worker_id || ':' || status ORDER BY attempt_no)"
            "  FROM attempts WHERE investigation_id = i.id),"
            " (SELECT count(*) FROM diagnosis_reports WHERE investigation_id = i.id)"
            " FROM investigations i WHERE i.id = %s",
            (investigation_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return list(row)


async def lease_left(connection: Connection, investigation_id: InvestigationId) -> float | None:
    async with owner_transaction(connection, OWNER_A):
        cursor = await connection.execute(
            "SELECT extract(epoch FROM lease_expires_at - pg_catalog.clock_timestamp())"
            " FROM investigations WHERE id = %s AND status = 'RUNNING'",
            (investigation_id,),
        )
        row = await cursor.fetchone()
    return None if row is None else float(row[0])


async def test_a_worker_publishes_its_jobs_then_stops(
    connected: ScratchDatabase,
) -> None:
    async with connected.runtime() as connection:
        first, second = await queue(connection, n=2)

        output = await work("--worker-id", "w-1", "--jobs", "2")

        assert output == {"worker_id": "w-1", "jobs": 2}
        for investigation_id in (first, second):
            assert await outcome(connection, investigation_id) == [
                "COMPLETED",
                ["w-1:SUCCEEDED"],
                1,
            ]


async def test_a_worker_sweeps_up_a_dead_workers_investigation(
    connected: ScratchDatabase,
) -> None:
    async with connected.runtime() as connection:
        (investigation_id,) = await queue(connection)
        dead = await claim_next(connection, "dead", OFFLINE)
        assert dead is not None
        await expire(connection, dead)

        await work("--worker-id", "w-1", "--jobs", "1")

        assert await outcome(connection, investigation_id) == [
            "COMPLETED",
            ["dead:LOST", "w-1:SUCCEEDED"],
            1,
        ]


async def test_a_worker_keeps_a_short_lease_alive_through_a_slow_provider(
    connected: ScratchDatabase,
) -> None:
    # Three calls of 0.3 s each outlast a 0.6 s lease, so the job only finishes if the
    # lease is renewed, and each renewal grants no more than 0.6 s.
    async with connected.runtime() as connection:
        (investigation_id,) = await queue(connection)
        started = time.monotonic()
        running = asyncio.create_task(
            work("--jobs", "1", "--lease-seconds", "0.6", "--provider-delay", "0.3")
        )
        seen = []
        while not running.done():
            left = await lease_left(connection, investigation_id)
            if left is not None:
                seen.append(left)
            await asyncio.sleep(0.05)
        await running

        assert time.monotonic() - started >= 0.9
        assert seen
        assert max(seen) <= 0.6
        assert (await outcome(connection, investigation_id))[0] == "COMPLETED"


async def test_the_kill_restart_demo_recovers_both_investigations(
    database: ScratchDatabase,
) -> None:
    environment = os.environ | {
        "INCIDENTPILOT_DATABASE_URL": database.conninfo("runtime"),
        "INCIDENTPILOT_MODE": "offline_demo",
    }
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(DEMO)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=timedelta(minutes=2).total_seconds(),
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "worker demo-a killed with SIGKILL while its first call was in flight" in done.stdout
    assert "its late commit was refused: attempt " in done.stdout
    assert done.stdout.count("outcome: COMPLETED at epoch 2, 1 report") == 2
    assert done.stdout.endswith("Both investigations recovered, each with one report.\n")
