"""What the runtime role may do outside row-level security (ADR-014)."""

import re

import pytest
from psycopg import errors

from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase


def denied(target: str) -> str:
    # PostgreSQL may append the offending statement on the following lines.
    return rf"^{re.escape(f'permission denied for {target}')}(\n|$)"


async def test_the_runtime_role_reads_owners(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        cursor = await connection.execute("SELECT id, name FROM owners ORDER BY id")
        assert await cursor.fetchall() == [(OWNER_A, "local"), (OWNER_B, "second")]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO owners (id, name) VALUES (gen_random_uuid(), 'third')",
        "UPDATE owners SET name = 'renamed'",
        "DELETE FROM owners",
    ],
)
async def test_the_runtime_role_cannot_change_owners(
    database: ScratchDatabase, statement: str
) -> None:
    async with database.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=denied("table owners")):
            await connection.execute(statement)


async def test_the_runtime_role_cannot_read_the_migration_history(
    database: ScratchDatabase,
) -> None:
    async with database.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=denied("table alembic_version")):
            await connection.execute("SELECT * FROM alembic_version")


async def test_the_runtime_role_cannot_create_tables(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=denied("schema public")):
            await connection.execute("CREATE TABLE stray (id int)")


async def test_the_runtime_role_cannot_create_temporary_tables(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=r"^permission denied to create"):
            await connection.execute("CREATE TEMPORARY TABLE stray (id int)")


async def test_a_new_function_needs_an_explicit_grant(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        await connection.execute("CREATE FUNCTION scheduler_probe() RETURNS int RETURN 1")

    async with database.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=denied("function scheduler_probe")):
            await connection.execute("SELECT scheduler_probe()")


async def test_other_roles_cannot_see_the_schema(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT pg_catalog.has_schema_privilege('public', 'public', 'USAGE')"
        )
        assert await cursor.fetchone() == (False,)
