"""Migrations build the schema from nothing, and only go forward."""

import re
from pathlib import Path

import pytest
from alembic import command

from incidentpilot.persistence import migrate
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID
from tests.integration.harness import ScratchDatabase


async def contents(database: ScratchDatabase) -> tuple[object, object]:
    async with database.migrator() as connection:
        version = await (
            await connection.execute("SELECT version_num FROM alembic_version")
        ).fetchall()
        owners = await (await connection.execute("SELECT id, name FROM owners")).fetchall()
    return version, owners


async def test_migrating_an_empty_database_seeds_only_the_local_owner(
    empty_database: ScratchDatabase,
) -> None:
    migrate.upgrade(empty_database.conninfo("migrator"))

    assert await contents(empty_database) == ([("0001",)], [(LOCAL_OWNER_ID, "local")])


async def test_migrating_again_changes_nothing(empty_database: ScratchDatabase) -> None:
    migrate.upgrade(empty_database.conninfo("migrator"))
    migrate.upgrade(empty_database.conninfo("migrator"))

    assert await contents(empty_database) == ([("0001",)], [(LOCAL_OWNER_ID, "local")])


def test_migrations_refuse_to_go_back(database: ScratchDatabase) -> None:
    config = migrate.alembic_config(database.conninfo("migrator"))

    message = "migrations only go forward; restore a backup instead"
    with pytest.raises(NotImplementedError, match=rf"^{re.escape(message)}$"):
        command.downgrade(config, "base")


async def test_the_entry_point_migrates_the_configured_database(
    empty_database: ScratchDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MIGRATION_DATABASE_URL", empty_database.conninfo("migrator"))
    monkeypatch.chdir(tmp_path)

    migrate.main()

    assert await contents(empty_database) == ([("0001",)], [(LOCAL_OWNER_ID, "local")])
