"""The catalog check: the schema keeps every owner's rows apart (erratum E19).

Only the owners table, the installation-wide capacity slots and Alembic's own table
are exempt. Every other table needs:
- a non-null owner;
- forced row-level security with the one owner policy;
- unique keys and foreign keys that include the owner.

Beyond the tables:
- no grant lets the runtime act on rows it can't see;
- nothing is created outside `public` where these checks wouldn't look;
- views run with the caller's rights.

The scheduler's definer functions are checked in test_scheduler.py.
"""

import re

import pytest

from incidentpilot.domain.enums import (
    AttemptStatus,
    DiagnosticOutcome,
    EventType,
    EvidenceKind,
    IncidentSeverity,
    InvestigationStatus,
    ModelCallState,
    ReportAuthor,
    RunMode,
    TerminationReason,
)
from incidentpilot.persistence.database import Connection
from tests.integration.harness import ScratchDatabase

# E19: only these have no owner column. Capacity slots are installation-wide.
EXEMPT = {"alembic_version", "owners", "capacity_slots"}
OWNED = "(owner_id = current_owner_id())"
RUNTIME = "incidentpilot_runtime"


async def tenant_tables(connection: Connection) -> set[str]:
    cursor = await connection.execute(
        "SELECT relname FROM pg_catalog.pg_class"
        " WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')"
    )
    return {name for (name,) in await cursor.fetchall()} - EXEMPT


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


async def test_each_owner_policy_applies_to_every_command_and_role(
    database: ScratchDatabase,
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT polrelid::regclass::text, polname, polcmd, polpermissive, polroles,
                   pg_catalog.pg_get_expr(polqual, polrelid),
                   pg_catalog.pg_get_expr(polwithcheck, polrelid)
            FROM pg_catalog.pg_policy
            """
        )
        policies = await cursor.fetchall()

    assert policies
    for table, *policy in policies:
        # Every command, permissive, for PUBLIC (role 0), reading and writing the owner.
        assert policy == ["owner_isolation", "*", True, [0], OWNED, OWNED], table


async def test_the_current_owner_comes_only_from_the_transaction_setting(
    database: ScratchDatabase,
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT prosecdef, provolatile, proconfig, pg_catalog.pg_get_userbyid(proowner),
                   pg_catalog.pg_get_function_sqlbody(oid)
            FROM pg_catalog.pg_proc WHERE oid = 'current_owner_id'::regproc
            """
        )
        row = await cursor.fetchone()

    assert row == (
        False,
        "s",
        None,
        "incidentpilot_migrator",
        "RETURN (NULLIF(current_setting('app.owner_id'::text, true), ''::text))::uuid",
    )


async def test_every_foreign_key_between_tenant_tables_carries_the_owner(
    database: ScratchDatabase,
) -> None:
    # (owner_id, x) -> (owner_id, id) makes a reference to another owner's row
    # impossible, whatever the IDs.
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT conrelid::regclass::text, conname, confrelid::regclass::text,
                   (SELECT array_agg(attname ORDER BY position)
                    FROM unnest(conkey) WITH ORDINALITY AS key (number, position)
                    JOIN pg_catalog.pg_attribute
                      ON attrelid = conrelid AND attnum = key.number),
                   (SELECT array_agg(attname ORDER BY position)
                    FROM unnest(confkey) WITH ORDINALITY AS key (number, position)
                    JOIN pg_catalog.pg_attribute
                      ON attrelid = confrelid AND attnum = key.number)
            FROM pg_catalog.pg_constraint
            WHERE contype = 'f' AND connamespace = 'public'::regnamespace
            """
        )
        keys = await cursor.fetchall()
        tables = await tenant_tables(connection)

    tenant_keys = [key for key in keys if key[0] not in EXEMPT]
    # Every tenant table names a real owner.
    assert {table for table, _, parent, *_ in tenant_keys if parent == "owners"} == tables
    for _, name, parent, columns, parent_columns in tenant_keys:
        if parent == "owners":
            assert (columns, parent_columns) == (["owner_id"], ["id"]), name
        else:
            assert parent not in EXEMPT, name
            assert (columns[0], parent_columns[0]) == ("owner_id", "owner_id"), name
            assert parent_columns[1:] == ["id"], name


async def test_the_runtime_holds_no_grant_that_skips_row_security(
    database: ScratchDatabase,
) -> None:
    # TRUNCATE ignores row-level security, REFERENCES lets a role probe for rows through
    # its own foreign keys, and TRIGGER runs code on rows it can't see.
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT table_.relname, grant_.privilege_type
            FROM pg_catalog.pg_class AS table_, pg_catalog.aclexplode(table_.relacl) AS grant_
            WHERE table_.relnamespace = 'public'::regnamespace
              AND grant_.grantee = %s::regrole
              AND grant_.privilege_type IN ('TRUNCATE', 'REFERENCES', 'TRIGGER')
            UNION ALL
            SELECT table_.relname || '.' || column_.attname, grant_.privilege_type
            FROM pg_catalog.pg_class AS table_
            JOIN pg_catalog.pg_attribute AS column_ ON column_.attrelid = table_.oid,
                 pg_catalog.aclexplode(column_.attacl) AS grant_
            WHERE table_.relnamespace = 'public'::regnamespace
              AND grant_.grantee = %s::regrole
              AND grant_.privilege_type = 'REFERENCES'
            """,
            (RUNTIME, RUNTIME),
        )
        assert await cursor.fetchall() == []


async def test_nothing_is_granted_to_public(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT table_.relname, grant_.privilege_type
            FROM pg_catalog.pg_class AS table_, pg_catalog.aclexplode(table_.relacl) AS grant_
            WHERE table_.relnamespace = 'public'::regnamespace AND grant_.grantee = 0
            UNION ALL
            SELECT table_.relname || '.' || column_.attname, grant_.privilege_type
            FROM pg_catalog.pg_class AS table_
            JOIN pg_catalog.pg_attribute AS column_ ON column_.attrelid = table_.oid,
                 pg_catalog.aclexplode(column_.attacl) AS grant_
            WHERE table_.relnamespace = 'public'::regnamespace AND grant_.grantee = 0
            """
        )
        assert await cursor.fetchall() == []


async def test_nothing_lives_outside_the_checked_schema(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace"
            " WHERE nspname NOT LIKE 'pg\\_%%' AND nspname <> 'information_schema'"
        )
        assert await cursor.fetchall() == [("public",)]


async def test_views_run_with_the_callers_rights(database: ScratchDatabase) -> None:
    # A plain view runs with its creator's rights and would skip the owner policy, and a
    # materialized view keeps rows outside the policy altogether.
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT relname, relkind, coalesce(reloptions, '{}')
            FROM pg_catalog.pg_class
            WHERE relnamespace = 'public'::regnamespace AND relkind IN ('v', 'm')
            """
        )
        views = await cursor.fetchall()

    for name, kind, options in views:
        assert (kind, "security_invoker=true" in options) == ("v", True), name


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
        ("attempts", "status", {s.value for s in AttemptStatus}),
        ("investigations", "mode", {m.value for m in RunMode} - {RunMode.REPLAY.value}),
        ("run_events", "event_type", {e.value for e in EventType}),
        ("model_calls", "state", {s.value for s in ModelCallState}),
        ("evidence", "kind", {k.value for k in EvidenceKind}),
        ("investigations", "termination_reason", {r.value for r in TerminationReason}),
        ("report_candidates", "author", {a.value for a in ReportAuthor}),
        ("report_candidates", "termination_reason", {r.value for r in TerminationReason}),
        ("diagnosis_reports", "diagnostic_outcome", {o.value for o in DiagnosticOutcome}),
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
            (table, f"CHECK (({column} = ANY%"),
        )
        (definition,) = await cursor.fetchone() or ("",)

    assert set(re.findall(r"'([^']*)'::text", str(definition))) == values
