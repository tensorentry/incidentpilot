"""The CLI end to end against PostgreSQL: import, report, start, show (erratum E10)."""

import asyncio
import json
import re
from datetime import datetime
from typing import Any

import pytest

from incidentpilot import cli
from tests.integration.harness import OWNER_B, ScratchDatabase
from tests.integration.seeding import FIXTURE, MANIFEST, OBSERVATIONS

INCIDENT_WINDOW = ["--start", "2026-11-04T18:15:00Z", "--end", "2026-11-04T18:45:00Z"]


@pytest.fixture
def connected(database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch) -> ScratchDatabase:
    monkeypatch.setenv("INCIDENTPILOT_DATABASE_URL", database.conninfo("runtime"))
    monkeypatch.setenv("INCIDENTPILOT_MODE", "offline_demo")
    return database


async def run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    await asyncio.to_thread(cli.main, list(argv))
    text = capsys.readouterr().out
    parsed = json.loads(text)
    # Sorted, indented JSON, so output diffs cleanly and scripts can rely on it.
    assert text == json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    return parsed


def create(*extra: str) -> list[str]:
    return [
        *extra,
        "create-incident",
        "--service",
        "checkout-api",
        "--severity",
        "sev2",
        *INCIDENT_WINDOW,
        "--description",
        "Checkout p95 latency is up",
        "--key",
        "incident-1",
    ]


def start(incident_id: str, *extra: str) -> list[str]:
    return [
        *extra,
        "start-investigation",
        incident_id,
        "--snapshot",
        MANIFEST.snapshot_id,
        *INCIDENT_WINDOW,
        "--key",
        "start-1",
    ]


async def test_import_report_start_and_show(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    imported = await run(capsys, "import-snapshots", "--root", str(OBSERVATIONS))
    incident = await run(capsys, *create())
    incident_id = incident["incident"]["id"]
    started = await run(capsys, *start(incident_id))
    investigation_id = started["investigation"]["id"]
    shown = await run(capsys, "show-investigation", investigation_id)

    assert {"snapshot_id": FIXTURE.stem, "created": True} in imported["snapshots"]
    assert (incident["created"], incident["idempotency_key"]) == (True, "incident-1")
    assert (started["created"], started["idempotency_key"]) == (True, "start-1")
    assert shown["investigation"]["status"] == "QUEUED"
    assert shown["investigation"]["config"]["mode"] == "offline_demo"
    (event,) = shown["events"]
    assert (event["seq"], event["type"]) == (1, "INVESTIGATION_QUEUED")
    assert event["payload"]["incident_id"] == incident_id
    assert datetime.fromisoformat(event["at"]).tzinfo is not None


async def test_a_retried_command_returns_the_original(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    first = await run(capsys, *create())
    again = await run(capsys, *create())

    assert again["created"] is False
    assert again["incident"] == first["incident"]


async def test_a_missing_key_gets_a_fresh_one(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = create()[:-2]
    first = await run(capsys, *argv)
    second = await run(capsys, *argv)

    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["incident"] != second["incident"]


async def test_another_owner_cannot_see_the_investigation(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    await run(capsys, "import-snapshots", "--root", str(OBSERVATIONS))
    incident = await run(capsys, *create())
    started = await run(capsys, *start(incident["incident"]["id"]))
    investigation_id = started["investigation"]["id"]

    message = f"incidentpilot: investigation {investigation_id} doesn't exist"
    with pytest.raises(SystemExit, match=rf"^{re.escape(message)}$"):
        await run(capsys, "--owner", str(OWNER_B), "show-investigation", investigation_id)


async def test_a_mode_other_than_the_process_is_refused(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    await run(capsys, "import-snapshots", "--root", str(OBSERVATIONS))
    incident = await run(capsys, *create())

    message = "incidentpilot: this process runs in offline_demo mode, not live"
    with pytest.raises(SystemExit, match=rf"^{re.escape(message)}$"):
        await run(capsys, *start(incident["incident"]["id"]), "--mode", "live")


async def test_an_import_error_exits_with_its_reason(
    connected: ScratchDatabase, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    message = "incidentpilot: the owner doesn't exist"
    with pytest.raises(SystemExit, match=rf"^{re.escape(message)}$"):
        await run(
            capsys,
            "--owner",
            "00000000-0000-4000-8000-0000000000ff",
            "import-snapshots",
            "--root",
            str(OBSERVATIONS),
        )
