"""Two owners, every tenant table: neither can read, change, add or cite the other's
rows (erratum E19, invariant 9).

Both owners run a full investigation, so every tenant table holds real rows for each.
The table checks run as the migrator, which owns the tables and holds every privilege,
so only row-level security stands between the owners. The runtime's narrower grants
are checked in test_catalog.py and the tests of each table.
"""

import re
from typing import Any

import pytest
from psycopg import errors, sql

from incidentpilot.domain.enums import ReportAuthor, TerminationReason
from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.evidence import visible_evidence
from incidentpilot.persistence.fence import fenced_transaction
from incidentpilot.workflows.publication import (
    ReportRejected,
    publish_report,
    submit_candidate,
)
from incidentpilot.workflows.scripted import (
    DEMO_SCRIPT,
    SCRIPT_PROMPT_SHA256,
    ScriptedModel,
    abstain,
)
from incidentpilot.workflows.worker import Worker
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import OFFLINE, claimed, queue

# Every table but the exempt ones. A new tenant table must be added here, and its rows
# must come out of a full investigation.
TENANT_TABLES = [
    "attempts",
    "diagnosis_reports",
    "evidence",
    "idempotency_requests",
    "incidents",
    "investigation_evidence",
    "investigations",
    "model_calls",
    "report_candidates",
    "run_events",
    "snapshots",
]
# Capacity slots hold the bound attempt's owner, but they are installation-wide, and the
# runtime has no grant on them.
EXEMPT = {"alembic_version", "owners", "capacity_slots"}
POLICY_VIOLATION = r"^new row violates row-level security policy for table "


@pytest.fixture
async def both(database: ScratchDatabase) -> ScratchDatabase:
    """Each owner has run one investigation through to its published report."""
    async with database.runtime() as connection, database.runtime() as keeping:
        for owner, tag in [(OWNER_A, "full-a"), (OWNER_B, "full-b")]:
            await queue(connection, owner, tag=tag)
            worker = Worker("w", OFFLINE, ScriptedModel(DEMO_SCRIPT))
            assert await worker.run_once(connection, keeping)
    return database


async def owners_seen(connection: Connection, table: str) -> list[tuple[Any, ...]]:
    cursor = await connection.execute(
        sql.SQL("SELECT owner_id, count(*) FROM {} GROUP BY owner_id").format(sql.Identifier(table))
    )
    return list(await cursor.fetchall())


async def counts(database: ScratchDatabase, table: str) -> dict[OwnerId, int]:
    async with database.migrator() as connection:
        seen: dict[OwnerId, int] = {}
        for owner in (OWNER_A, OWNER_B):
            async with owner_transaction(connection, owner):
                seen |= dict(await owners_seen(connection, table))
        return seen


async def test_every_tenant_table_is_listed(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT relname FROM pg_catalog.pg_class"
            " WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p')"
            " ORDER BY relname"
        )
        tables = [name for (name,) in await cursor.fetchall()]

    assert [table for table in tables if table not in EXEMPT] == TENANT_TABLES


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_each_owner_reads_only_its_own_rows(both: ScratchDatabase, table: str) -> None:
    async with both.migrator() as connection:
        for owner in (OWNER_A, OWNER_B):
            async with owner_transaction(connection, owner):
                (seen,) = await owners_seen(connection, table)
            assert seen[0] == owner
            assert seen[1] > 0


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_no_owner_reads_nothing(both: ScratchDatabase, table: str) -> None:
    async with both.migrator() as connection, connection.transaction():
        assert await owners_seen(connection, table) == []


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_another_owners_rows_cannot_be_changed_or_deleted(
    both: ScratchDatabase, table: str
) -> None:
    before = await counts(both, table)
    async with both.migrator() as connection, owner_transaction(connection, OWNER_A):
        changed = await connection.execute(
            sql.SQL("UPDATE {} SET owner_id = owner_id WHERE owner_id = %s").format(
                sql.Identifier(table)
            ),
            (OWNER_B,),
        )
        deleted = await connection.execute(
            sql.SQL("DELETE FROM {} WHERE owner_id = %s").format(sql.Identifier(table)),
            (OWNER_B,),
        )
        assert (changed.rowcount, deleted.rowcount) == (0, 0)
    assert await counts(both, table) == before


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_no_owner_adds_a_row_for_another(both: ScratchDatabase, table: str) -> None:
    # A copy of one of A's own rows, relabelled as B's. The policy refuses it before
    # any key or foreign key is checked.
    async with both.migrator() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=POLICY_VIOLATION):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(
                    sql.SQL(
                        "INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table},"
                        " to_jsonb(mine) || jsonb_build_object('owner_id', %s::text))).*"
                        " FROM {table} AS mine LIMIT 1"
                    ).format(table=sql.Identifier(table)),
                    (OWNER_B,),
                )
    assert set(await counts(both, table)) == {OWNER_A, OWNER_B}


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_no_owner_gives_a_row_away(both: ScratchDatabase, table: str) -> None:
    before = await counts(both, table)
    async with both.migrator() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=POLICY_VIOLATION):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute(
                    sql.SQL(
                        "UPDATE {table} SET owner_id = %s"
                        " WHERE ctid = (SELECT ctid FROM {table} LIMIT 1)"
                    ).format(table=sql.Identifier(table)),
                    (OWNER_B,),
                )
    assert await counts(both, table) == before


async def evidence_of(connection: Connection, owner: OwnerId) -> list[Any]:
    async with owner_transaction(connection, owner):
        cursor = await connection.execute(
            "SELECT id FROM investigations WHERE status = 'COMPLETED'"
        )
        (investigation_id,) = await cursor.fetchone() or (None,)
        return list(await visible_evidence(connection, investigation_id))


async def test_a_visibility_grant_cannot_join_another_owners_evidence(
    both: ScratchDatabase,
) -> None:
    async with both.runtime() as connection:
        theirs = await evidence_of(connection, OWNER_B)
        lease = await claimed(connection, OWNER_A, tag="grant")

        message = re.escape(
            'insert or update on table "investigation_evidence" violates foreign key'
            ' constraint "investigation_evidence_evidence_fkey"'
        )
        with pytest.raises(errors.ForeignKeyViolation, match=rf"^{message}"):
            async with fenced_transaction(connection, lease):
                await connection.execute(
                    "INSERT INTO investigation_evidence"
                    " (owner_id, investigation_id, evidence_id, first_attempt_id, reason)"
                    " VALUES (current_owner_id(), %s, %s, %s, 'tool_result')",
                    (lease.investigation_id, theirs[0].evidence_id, lease.attempt_id),
                )


async def test_a_report_cannot_cite_another_owners_evidence(both: ScratchDatabase) -> None:
    # B's evidence IDs are real, but A's investigation can't see them, so a report
    # citing them fails its checks and publishes nothing.
    async with both.runtime() as connection:
        theirs = await evidence_of(connection, OWNER_B)
        lease = await claimed(connection, OWNER_A, tag="cite")
        async with fenced_transaction(connection, lease):
            candidate_id = await submit_candidate(
                connection,
                lease,
                abstain(theirs),
                author=ReportAuthor.MODEL,
                reason=TerminationReason.INVESTIGATOR_FINISHED,
                prompt_sha256=SCRIPT_PROMPT_SHA256,
            )

        with pytest.raises(ReportRejected) as rejected:
            async with fenced_transaction(connection, lease):
                await publish_report(connection, lease, candidate_id)

        assert [issue.path for issue in rejected.value.issues] == [
            f"candidate.observations[{n}].references[0]" for n in range(len(theirs))
        ]
        async with owner_transaction(connection, OWNER_A):
            cursor = await connection.execute(
                "SELECT status FROM investigations WHERE id = %s", (lease.investigation_id,)
            )
            assert await cursor.fetchone() == ("VALIDATING",)
