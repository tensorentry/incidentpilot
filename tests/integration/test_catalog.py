"""Every tenant table is under owner isolation (erratum E19).

Only the owners table and Alembic's own table are exempt. T3.14 widens this into the
full catalog check: keys, foreign keys, roles and definer functions.
"""

from tests.integration.harness import ScratchDatabase

EXEMPT = {"alembic_version", "owners"}


async def test_every_other_table_is_isolated_by_owner(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT
                table_.relname,
                table_.relrowsecurity AND table_.relforcerowsecurity,
                (SELECT array_agg(polname) FROM pg_catalog.pg_policy WHERE polrelid = table_.oid),
                (SELECT attnotnull FROM pg_catalog.pg_attribute
                 WHERE attrelid = table_.oid AND attname = 'owner_id' AND NOT attisdropped)
            FROM pg_catalog.pg_class AS table_
            WHERE table_.relnamespace = 'public'::regnamespace
              AND table_.relkind IN ('r', 'p')
            """
        )
        tables = {name: rest for name, *rest in await cursor.fetchall()}

    assert set(tables) >= EXEMPT
    for name in set(tables) - EXEMPT:
        assert tables[name] == [True, ["owner_isolation"], True], name
