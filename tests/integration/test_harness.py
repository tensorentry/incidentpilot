"""The harness gives each test its own migrated database with two owners."""

from uuid import uuid4

from psycopg import sql

from tests.integration.harness import (
    OWNER_A,
    OWNER_B,
    TEMPLATE,
    ScratchDatabase,
    Servers,
    admin,
    sweep,
)


async def test_each_test_has_its_own_database(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        cursor = await connection.execute("SELECT current_database()")
        assert await cursor.fetchone() == (database.name,)
    assert database.name != TEMPLATE


async def test_the_two_owners_are_distinct_and_the_first_is_local(
    database: ScratchDatabase,
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute("SELECT id, name FROM owners ORDER BY id")
        assert await cursor.fetchall() == [(OWNER_A, "local"), (OWNER_B, "second")]


def test_a_sweep_drops_only_copies_whose_run_is_gone(
    database: ScratchDatabase, servers: Servers
) -> None:
    # No server process has PID 0, and a name without a PID predates this scheme. Names
    # are unique, because other test runs may be sweeping at the same time.
    stale = [f"incidentpilot_test_0_{uuid4().hex}", f"incidentpilot_test_unnamed_{uuid4().hex}"]
    with admin(servers) as connection:
        for name in stale:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        dropped = sweep(connection)
        remaining = {
            name
            for (name,) in connection.execute(
                "SELECT datname FROM pg_catalog.pg_database WHERE datname LIKE 'incidentpilot%'"
            )
        }

    # Another run's sweep may have dropped them first, so check what's left.
    assert not set(stale) & remaining
    assert {database.name, TEMPLATE, "incidentpilot"} <= remaining
    assert not set(dropped) & {database.name, TEMPLATE, "incidentpilot"}
