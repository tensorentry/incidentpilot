"""The snapshot registry and its import step (errata E7 and E19)."""

import asyncio
import re
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from incidentpilot.adapters.fixture_bundle import ObservationBundle, bundle_sha256
from incidentpilot.application import snapshots
from incidentpilot.application.snapshots import import_directory, import_file, read_bundle
from incidentpilot.domain.ids import OwnerId, SnapshotId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.snapshots import SnapshotError, get_snapshot
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase

OBSERVATIONS = Path(__file__).resolve().parents[2] / "scenarios" / "observations"
FILES = sorted(OBSERVATIONS.glob("*.json"))
FIRST = FILES[0]


def exactly(message: str) -> str:
    return rf"^{re.escape(message)}$"


def write(directory: Path, bundle: ObservationBundle) -> Path:
    path = directory / f"{bundle.manifest.snapshot_id}.json"
    path.write_text(bundle.model_dump_json())
    return path


def variant(snapshot_id: str, parent: str | None = None) -> ObservationBundle:
    bundle = read_bundle(FIRST)
    manifest = bundle.manifest.model_copy(
        update={"snapshot_id": snapshot_id, "parent_snapshot_id": parent}
    )
    return bundle.model_copy(update={"manifest": manifest})


async def stored_ids(connection: Connection, owner: OwnerId) -> list[str]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute("SELECT id FROM snapshots ORDER BY id")
        return [str(id_) for (id_,) in await cursor.fetchall()]


async def test_the_committed_files_import_and_read_back(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        lines = await import_directory(connection, OWNER_A, OBSERVATIONS)

        assert lines == [f"imported {path.stem}" for path in FILES]
        for path in FILES:
            bundle = read_bundle(path)
            async with owner_transaction(connection, OWNER_A):
                stored = await get_snapshot(connection, bundle.manifest.snapshot_id)
            assert stored is not None
            assert stored.manifest == bundle.manifest
            assert stored.content_sha256 == bundle_sha256(bundle)


async def test_importing_again_changes_nothing(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await import_directory(connection, OWNER_A, OBSERVATIONS)
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute("SELECT id, created_at FROM snapshots ORDER BY id")
            before = await cursor.fetchall()

        lines = await import_directory(connection, OWNER_A, OBSERVATIONS)

        assert lines == [f"already imported {path.stem}" for path in FILES]
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute("SELECT id, created_at FROM snapshots ORDER BY id")
            assert await cursor.fetchall() == before


async def test_each_owner_gets_its_own_copy(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await import_directory(connection, OWNER_A, OBSERVATIONS)
        await import_directory(connection, OWNER_B, OBSERVATIONS)

        expected = [path.stem for path in FILES]
        assert await stored_ids(connection, OWNER_A) == expected
        assert await stored_ids(connection, OWNER_B) == expected


async def test_an_owner_cannot_see_another_owners_snapshot(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        await import_file(connection, OWNER_A, FIRST)

        async with owner_transaction(connection, OWNER_B):
            assert await get_snapshot(connection, SnapshotId(FIRST.stem)) is None
        assert await stored_ids(connection, OWNER_B) == []


async def test_different_content_under_the_same_id_is_refused(
    database: ScratchDatabase, tmp_path: Path
) -> None:
    original = read_bundle(FIRST)
    log = original.logs[0].model_copy(update={"message": "Edited after import"})
    edited = write(tmp_path, original.model_copy(update={"logs": [log, *original.logs[1:]]}))

    async with database.runtime() as connection:
        await import_file(connection, OWNER_A, FIRST)
        message = f"snapshot {FIRST.stem} is already imported with different content"
        with pytest.raises(SnapshotError, match=exactly(message)):
            await import_file(connection, OWNER_A, edited)

        async with owner_transaction(connection, OWNER_A):
            stored = await get_snapshot(connection, original.manifest.snapshot_id)
        assert stored is not None
        assert stored.content_sha256 == bundle_sha256(original)


async def test_a_snapshot_descends_from_its_owners_snapshot(
    database: ScratchDatabase, tmp_path: Path
) -> None:
    child = write(tmp_path, variant("snap-child", parent=FIRST.stem))

    async with database.runtime() as connection:
        await import_file(connection, OWNER_A, FIRST)

        assert await import_file(connection, OWNER_A, child) is True
        async with owner_transaction(connection, OWNER_A):
            stored = await get_snapshot(connection, SnapshotId("snap-child"))
        assert stored is not None
        assert stored.manifest.parent_snapshot_id == FIRST.stem


@pytest.mark.parametrize("parent_owner", [None, OWNER_A], ids=["nobody", "other owner"])
async def test_a_parent_must_be_the_owners_own(
    database: ScratchDatabase, tmp_path: Path, parent_owner: OwnerId | None
) -> None:
    child = write(tmp_path, variant("snap-child", parent=FIRST.stem))

    async with database.runtime() as connection:
        if parent_owner is not None:
            await import_file(connection, parent_owner, FIRST)

        message = f"snapshot snap-child needs its parent {FIRST.stem} imported first"
        with pytest.raises(SnapshotError, match=exactly(message)):
            await import_file(connection, OWNER_B, child)
        assert await stored_ids(connection, OWNER_B) == []


async def test_an_unknown_owner_is_refused(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        with pytest.raises(SnapshotError, match=exactly("the owner doesn't exist")):
            await import_file(connection, OwnerId(uuid4()), FIRST)


@pytest.mark.parametrize(
    "statement", ["UPDATE snapshots SET adapter_version = 'edited'", "DELETE FROM snapshots"]
)
async def test_snapshots_never_change(database: ScratchDatabase, statement: str) -> None:
    async with database.runtime() as connection:
        await import_file(connection, OWNER_A, FIRST)
        with pytest.raises(
            errors.InsufficientPrivilege, match=r"^permission denied for table snapshots(\n|$)"
        ):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(statement)


async def test_concurrent_imports_store_one_snapshot(database: ScratchDatabase) -> None:
    async with database.runtime() as first, database.runtime() as second:
        results = await asyncio.gather(
            import_file(first, OWNER_A, FIRST), import_file(second, OWNER_A, FIRST)
        )

        assert sorted(results) == [False, True]
        assert await stored_ids(first, OWNER_A) == [FIRST.stem]


def raw_row(**changes: object) -> dict[str, object]:
    manifest = read_bundle(FIRST).manifest.model_dump(mode="json")
    row: dict[str, object] = {
        "id": manifest["snapshot_id"],
        "parent": None,
        "adapter_version": manifest["adapter_version"],
        "manifest": manifest,
    }
    return row | changes


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"id": "snap-other"}, "snapshots_manifest_matches"),
        ({"adapter_version": "other"}, "snapshots_manifest_matches"),
        ({"parent": "snap-other"}, "snapshots_manifest_matches"),
        (
            {"manifest": raw_row()["manifest"] | {"schema_version": "2"}},  # type: ignore[operator]
            "snapshots_manifest_matches",
        ),
        (
            {
                "parent": FIRST.stem,
                "manifest": raw_row()["manifest"] | {"parent_snapshot_id": FIRST.stem},  # type: ignore[operator]
            },
            "snapshots_not_own_parent",
        ),
    ],
    ids=["id", "adapter version", "parent", "schema version", "own parent"],
)
async def test_stored_columns_must_agree_with_the_manifest(
    database: ScratchDatabase, changes: dict[str, object], constraint: str
) -> None:
    row = raw_row(**changes)
    async with database.migrator() as connection:
        with pytest.raises(errors.CheckViolation, match=rf'constraint "{constraint}"'):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(
                    "INSERT INTO snapshots (owner_id, id, parent_snapshot_id, adapter_version,"
                    " content_sha256, manifest) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        OWNER_A,
                        row["id"],
                        row["parent"],
                        row["adapter_version"],
                        "0" * 64,
                        Jsonb(row["manifest"]),
                    ),
                )


async def test_the_entry_point_imports_for_the_named_owner(
    database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_DATABASE_URL", database.conninfo("runtime"))

    await asyncio.to_thread(snapshots.main, ["--owner", str(OWNER_B), "--root", str(OBSERVATIONS)])

    assert capsys.readouterr().out == "".join(f"imported {path.stem}\n" for path in FILES)
    async with database.runtime() as connection:
        assert await stored_ids(connection, OWNER_B) == [path.stem for path in FILES]
        assert await stored_ids(connection, OWNER_A) == []


async def test_the_entry_point_defaults_to_the_local_owner(
    database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_DATABASE_URL", database.conninfo("runtime"))
    monkeypatch.chdir(OBSERVATIONS.parents[1])

    await asyncio.to_thread(snapshots.main, [])

    assert capsys.readouterr().out.count("imported") == len(FILES)
    async with database.runtime() as connection:
        assert await stored_ids(connection, OWNER_A) == [path.stem for path in FILES]


async def test_the_entry_point_exits_with_an_import_error(
    database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_DATABASE_URL", database.conninfo("runtime"))

    with pytest.raises(SystemExit, match=exactly("the owner doesn't exist")):
        await asyncio.to_thread(
            snapshots.main, ["--owner", str(uuid4()), "--root", str(OBSERVATIONS)]
        )
