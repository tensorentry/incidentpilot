"""Every tenant table is under owner isolation (erratum E19).

Only the owners table and Alembic's own table are exempt. T3.14 widens this into the
full catalog check: keys, foreign keys, roles and definer functions.
"""

import re

import pytest

from incidentpilot.domain.enums import EventType, IncidentSeverity, InvestigationStatus, RunMode
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


async def test_every_unique_key_on_a_tenant_table_includes_the_owner(
    database: ScratchDatabase,
) -> None:
    # Constraint checks ignore row-level security, so a key without the owner would
    # reveal that another owner's row exists.
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT table_.relname, index_.indexrelid::regclass::text,
                   EXISTS (
                       SELECT FROM pg_catalog.pg_attribute
                       WHERE attrelid = table_.oid AND attname = 'owner_id'
                         AND attnum = ANY (index_.indkey)
                   )
            FROM pg_catalog.pg_index AS index_
            JOIN pg_catalog.pg_class AS table_ ON table_.oid = index_.indrelid
            WHERE table_.relnamespace = 'public'::regnamespace AND index_.indisunique
            """
        )
        keys = [(table, index, owned) for table, index, owned in await cursor.fetchall()]

    tenant_keys = [(index, owned) for table, index, owned in keys if table not in EXEMPT]
    assert tenant_keys
    assert all(owned for _, owned in tenant_keys), tenant_keys


@pytest.mark.parametrize(
    ("table", "column", "values"),
    [
        ("incidents", "severity", {s.value for s in IncidentSeverity}),
        ("investigations", "status", {s.value for s in InvestigationStatus}),
        ("investigations", "mode", {m.value for m in RunMode} - {RunMode.REPLAY.value}),
        ("run_events", "event_type", {e.value for e in EventType}),
    ],
)
async def test_each_stored_vocabulary_matches_its_enum(
    database: ScratchDatabase, table: str, column: str, values: set[str]
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT pg_catalog.pg_get_constraintdef(oid) FROM pg_catalog.pg_constraint"
            " WHERE conrelid = %s::regclass AND contype = 'c'"
            " AND pg_catalog.pg_get_constraintdef(oid) LIKE %s",
            (table, f"%({column} = ANY%"),
        )
        (definition,) = await cursor.fetchone() or ("",)

    assert set(re.findall(r"'([^']*)'::text", str(definition))) == values
