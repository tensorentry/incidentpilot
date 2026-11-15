"""Reading observation files for import, before anything touches the database."""

import re
from pathlib import Path

import pytest

from incidentpilot.application import snapshots
from incidentpilot.application.snapshots import read_bundle
from incidentpilot.persistence.snapshots import SnapshotError

OBSERVATIONS = Path(__file__).resolve().parents[3] / "scenarios" / "observations"
FIRST = sorted(OBSERVATIONS.glob("*.json"))[0]


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


def test_a_committed_file_reads_as_its_snapshot() -> None:
    assert read_bundle(FIRST).manifest.snapshot_id == FIRST.stem


def test_a_file_must_be_named_for_its_snapshot(tmp_path: Path) -> None:
    renamed = tmp_path / "snap-000000000000.json"
    renamed.write_bytes(FIRST.read_bytes())

    message = f"snap-000000000000.json holds snapshot {FIRST.stem}"
    with pytest.raises(SnapshotError, match=exactly(message)):
        read_bundle(renamed)


def test_an_invalid_file_is_refused_without_quoting_it(tmp_path: Path) -> None:
    broken = tmp_path / "snap-000000000000.json"
    broken.write_text('{"manifest": "canary-secret-value"}')

    with pytest.raises(
        SnapshotError, match=exactly("snap-000000000000.json isn't a valid observation bundle")
    ) as caught:
        read_bundle(broken)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_importing_without_a_database_exits_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INCIDENTPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    message = "Invalid configuration: INCIDENTPILOT_DATABASE_URL is required"
    with pytest.raises(SystemExit, match=exactly(message)):
        snapshots.main([])


def test_a_malformed_owner_is_refused_before_anything_connects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        snapshots.main(["--owner", "everyone"])

    assert caught.value.code == 2
    assert "argument --owner: invalid UUID value: 'everyone'" in capsys.readouterr().err


def test_help_describes_the_import(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        snapshots.main(["--help"])

    assert "Import snapshots from observation files for one owner" in capsys.readouterr().out
