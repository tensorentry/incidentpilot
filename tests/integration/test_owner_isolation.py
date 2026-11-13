"""Row-level security keeps each owner to its own rows (erratum E19, ADR-014).

No tenant table exists yet, so these tests build a scratch one with the same helper
every tenant migration uses.
"""

import re

import psycopg
import pytest
from psycopg import errors

from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence.database import Connection, OwnerContextError, owner_transaction
from incidentpilot.persistence.isolation import owner_isolation
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase

POLICY_VIOLATION = re.escape('new row violates row-level security policy for table "notes"')


@pytest.fixture
async def notes(database: ScratchDatabase) -> ScratchDatabase:
    """A tenant table holding two of owner A's notes and one of owner B's."""
    async with database.migrator() as connection:
        await connection.execute(
            "CREATE TABLE notes (id int PRIMARY KEY,"
            " owner_id uuid NOT NULL REFERENCES owners (id), body text NOT NULL)"
        )
        for statement in owner_isolation("notes"):
            await connection.execute(statement)
        await connection.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON notes TO incidentpilot_runtime"
        )
        for id_, owner, body in [(1, OWNER_A, "a1"), (2, OWNER_A, "a2"), (3, OWNER_B, "b1")]:
            async with owner_transaction(connection, owner):
                await connection.execute(
                    "INSERT INTO notes VALUES (%s, %s, %s)", (id_, owner, body)
                )
    return database


async def bodies(connection: Connection) -> list[str]:
    cursor = await connection.execute("SELECT body FROM notes ORDER BY id")
    return [body for (body,) in await cursor.fetchall()]


async def owned_bodies(connection: Connection, owner: OwnerId) -> list[str]:
    async with owner_transaction(connection, owner):
        return await bodies(connection)


async def test_each_owner_sees_only_its_own_rows(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        assert await owned_bodies(connection, OWNER_A) == ["a1", "a2"]
        assert await owned_bodies(connection, OWNER_B) == ["b1"]


async def test_no_owner_sees_nothing(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        assert await bodies(connection) == []


async def test_the_owner_ends_with_its_transaction(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        await owned_bodies(connection, OWNER_A)

        assert await bodies(connection) == []


async def test_the_owner_ends_when_its_transaction_fails(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        with pytest.raises(errors.DivisionByZero):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute("SELECT 1 / 0")

        assert await bodies(connection) == []


async def test_the_transaction_reports_its_owner(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection, owner_transaction(connection, OWNER_B):
        cursor = await connection.execute("SELECT current_owner_id()")
        assert await cursor.fetchone() == (OWNER_B,)


async def test_the_table_owner_is_held_too(notes: ScratchDatabase) -> None:
    async with notes.migrator() as connection:
        assert await bodies(connection) == []
        assert await owned_bodies(connection, OWNER_B) == ["b1"]


async def test_no_one_writes_a_row_for_another_owner(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=POLICY_VIOLATION):
            async with owner_transaction(connection, OWNER_B):
                await connection.execute("INSERT INTO notes VALUES (4, %s, 'a3')", (OWNER_A,))


async def test_no_owner_writes_nothing(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=POLICY_VIOLATION):
            await connection.execute("INSERT INTO notes VALUES (4, %s, 'a3')", (OWNER_A,))


async def test_an_owner_cannot_give_a_row_away(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        with pytest.raises(errors.InsufficientPrivilege, match=POLICY_VIOLATION):
            async with owner_transaction(connection, OWNER_A):
                await connection.execute("UPDATE notes SET owner_id = %s WHERE id = 1", (OWNER_B,))


async def test_another_owners_rows_cannot_be_changed_or_deleted(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection:
        async with owner_transaction(connection, OWNER_A):
            updated = await connection.execute("UPDATE notes SET body = 'taken' WHERE id = 3")
            deleted = await connection.execute("DELETE FROM notes WHERE id = 3")

        assert (updated.rowcount, deleted.rowcount) == (0, 0)
        assert await owned_bodies(connection, OWNER_B) == ["b1"]


async def test_a_malformed_owner_fails_instead_of_matching(notes: ScratchDatabase) -> None:
    async with notes.runtime() as connection, connection.transaction():
        await connection.execute("SELECT set_config('app.owner_id', 'everyone', true)")
        with pytest.raises(errors.InvalidTextRepresentation):
            await bodies(connection)


async def test_an_owner_transaction_cannot_nest(notes: ScratchDatabase) -> None:
    message = "an owner transaction can't start inside another transaction"
    async with notes.runtime() as connection, connection.transaction():
        with pytest.raises(OwnerContextError, match=rf"^{re.escape(message)}$"):
            async with owner_transaction(connection, OWNER_A):
                pass


async def test_an_owner_transaction_needs_an_autocommit_connection(
    notes: ScratchDatabase,
) -> None:
    message = "an owner transaction can't start inside another transaction"
    async with await psycopg.AsyncConnection.connect(notes.conninfo("runtime")) as connection:
        with pytest.raises(OwnerContextError, match=rf"^{re.escape(message)}$"):
            async with owner_transaction(connection, OWNER_A):
                pass
