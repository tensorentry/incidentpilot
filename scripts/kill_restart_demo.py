"""The kill/restart demo behind `make demo` (spec §16.2 Phase 1 exit; §6.3).

It drives real `incidentpilot` processes against the configured database, in two scenes:

1. Kill and restart. Worker A is killed with SIGKILL while a model call is in flight.
   Worker B comes up, sweeps A's lapsed lease, and finishes the investigation at the
   next epoch.
2. A stale worker wakes up. Worker A is frozen with SIGSTOP mid-call, B recovers and
   publishes, then A is resumed. A's late commit, under the old epoch, is refused.

After each scene it checks the database: one published report, A's attempt LOST and
B's SUCCEEDED, and A's call left UNKNOWN rather than settled. It exits non-zero if any
check fails.
"""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from incidentpilot.domain.ids import InvestigationId
from incidentpilot.persistence.database import (
    Connection,
    connect_runtime,
    owner_transaction,
    required,
)
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID
from incidentpilot.settings import load_settings

COMMAND = str(Path(sys.executable).with_name("incidentpilot"))
SNAPSHOT = "snap-9cbd373fc274"
WINDOW = ["--start", "2026-11-04T18:15:00Z", "--end", "2026-11-04T18:45:00Z"]
# Short leases, so a dead worker's investigation is recovered within seconds.
LEASE_SECONDS = "2"
# Long enough that worker A is always caught with its first call in flight, and short
# enough that a resumed worker A soon wakes to commit. The wait runs on while A is frozen.
SLOW_PROVIDER_SECONDS = "10"
TIMEOUT = 60.0

_CALLS = """
SELECT a.attempt_no, c.state FROM model_calls c
JOIN attempts a ON a.owner_id = c.owner_id AND a.id = c.attempt_id
WHERE c.investigation_id = %s ORDER BY a.attempt_no, c.created_at
"""
_ATTEMPTS = """
SELECT attempt_no, worker_id, lease_epoch, status, failure_category FROM attempts
WHERE investigation_id = %s ORDER BY attempt_no
"""
_OUTCOME = """
SELECT i.status, i.lease_epoch, count(r.id) FROM investigations i
LEFT JOIN diagnosis_reports r ON r.owner_id = i.owner_id AND r.investigation_id = i.id
WHERE i.id = %s GROUP BY i.status, i.lease_epoch
"""
# Our role's other sessions on this database that are doing anything at all.
_BUSY = """
SELECT count(*) FROM pg_stat_activity
WHERE datname = current_database() AND usename = current_user
  AND pid <> pg_backend_pid() AND state <> 'idle'
"""


class DemoFailed(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def incidentpilot(*argv: str) -> Any:
    done = subprocess.run([COMMAND, *argv], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise DemoFailed(f"incidentpilot {argv[0]} failed: {done.stderr.strip()}")
    return json.loads(done.stdout)


# Every worker started, so none is left running, or frozen, if the demo fails.
_workers: list[subprocess.Popen[str]] = []


def worker(name: str, *extra: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [COMMAND, "worker", "--worker-id", name, "--lease-seconds", LEASE_SECONDS, *extra],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _workers.append(process)
    return process


def finished(process: subprocess.Popen[str], name: str) -> str:
    """Wait for a worker started with `--jobs 1`, and return what it logged."""
    try:
        out, err = process.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DemoFailed(f"worker {name} didn't finish in {TIMEOUT:.0f} seconds") from None
    if process.returncode != 0:
        raise DemoFailed(f"worker {name} exited {process.returncode}: {err.strip()}")
    say(f"  worker {name} exited after {json.loads(out)['jobs']} job")
    return err


async def rows(connection: Connection, query: str, *parameters: object) -> list[tuple[Any, ...]]:
    async with owner_transaction(connection, LOCAL_OWNER_ID):
        cursor = await connection.execute(query, parameters)
        return list(await cursor.fetchall())


async def in_flight(connection: Connection, investigation_id: InvestigationId) -> None:
    """Wait until worker A has a model call DISPATCHED: sent, with no answer yet."""
    for _ in range(int(TIMEOUT * 10)):
        if (1, "DISPATCHED") in await rows(connection, _CALLS, investigation_id):
            return
        await asyncio.sleep(0.1)
    raise DemoFailed("worker A never dispatched a call")


async def freeze(connection: Connection, process: subprocess.Popen[str]) -> None:
    """SIGSTOP the worker outside any transaction, so it holds no row lock while frozen."""
    for _ in range(100):
        process.send_signal(signal.SIGSTOP)
        await asyncio.sleep(0.05)
        if (await rows(connection, _BUSY)) == [(0,)]:
            return
        process.send_signal(signal.SIGCONT)
        await asyncio.sleep(0.05)
    raise DemoFailed("worker A was always inside a transaction")


def start() -> InvestigationId:
    incident = incidentpilot(
        "create-incident",
        "--service",
        "checkout-api",
        "--severity",
        "sev2",
        *WINDOW,
        "--description",
        "Checkout p95 latency is up",
    )["incident"]
    started = incidentpilot("start-investigation", incident["id"], "--snapshot", SNAPSHOT, *WINDOW)[
        "investigation"
    ]
    say(f"  queued investigation {started['id']}")
    return InvestigationId(started["id"])


async def check(connection: Connection, investigation_id: InvestigationId) -> None:
    outcome = await rows(connection, _OUTCOME, investigation_id)
    attempts = await rows(connection, _ATTEMPTS, investigation_id)
    calls = await rows(connection, _CALLS, investigation_id)
    say(f"  outcome: {outcome[0][0]} at epoch {outcome[0][1]}, {outcome[0][2]} report")
    for number, name, epoch, status, failure in attempts:
        say(f"  attempt {number} by {name} at epoch {epoch}: {status} {failure or ''}".rstrip())
    say(f"  model calls: {', '.join(f'{number}:{state}' for number, state in calls)}")
    expected = [
        (1, "demo-a", 1, "LOST", "LEASE_LOST"),
        (2, "demo-b", 2, "SUCCEEDED", None),
    ]
    if outcome != [("COMPLETED", 2, 1)] or attempts != expected or calls[0] != (1, "UNKNOWN"):
        raise DemoFailed("the investigation didn't end as recovery promises")


async def kill_and_restart(connection: Connection) -> None:
    say("Scene 1: kill a worker mid-call, then start another")
    investigation_id = start()
    a = worker("demo-a", "--jobs", "1", "--provider-delay", SLOW_PROVIDER_SECONDS)
    await in_flight(connection, investigation_id)
    a.kill()
    a.wait()
    say("  worker demo-a killed with SIGKILL while its first call was in flight")
    finished(worker("demo-b", "--jobs", "1"), "demo-b")
    await check(connection, investigation_id)


async def stale_worker(connection: Connection) -> None:
    say("Scene 2: freeze a worker mid-call, recover, then wake it")
    investigation_id = start()
    a = worker("demo-a", "--jobs", "1", "--provider-delay", SLOW_PROVIDER_SECONDS)
    await in_flight(connection, investigation_id)
    await freeze(connection, a)
    say("  worker demo-a frozen with SIGSTOP while its first call was in flight")
    finished(worker("demo-b", "--jobs", "1"), "demo-b")
    a.send_signal(signal.SIGCONT)
    say("  worker demo-a resumed")
    logged = finished(a, "demo-a")
    refusal = next((line for line in logged.splitlines() if "StaleLease:" in line), None)
    if refusal is None:
        raise DemoFailed(f"worker demo-a's stale commit wasn't refused: {logged.strip()}")
    say(f"  its late commit was refused: {refusal.split('StaleLease: ', 1)[1]}")
    await check(connection, investigation_id)


async def main() -> None:
    conninfo = required(load_settings().database_url, "INCIDENTPILOT_DATABASE_URL")
    incidentpilot("import-snapshots")
    try:
        async with await connect_runtime(conninfo) as connection:
            await kill_and_restart(connection)
            await stale_worker(connection)
    finally:
        for process in _workers:
            process.kill()
            process.wait()
    say("Both investigations recovered, each with one report.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except DemoFailed as failure:
        raise SystemExit(f"demo failed: {failure}") from None
