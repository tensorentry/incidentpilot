"""Connections and the owner-scoped transaction (ADR-014).

Every query on tenant data runs inside owner_transaction. It sets the owner for that
transaction only, so a pooled connection can't carry one owner into the next
transaction.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from pydantic import SecretStr

from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence.isolation import OWNER_SETTING
from incidentpilot.settings import ConfigurationError

type Connection = AsyncConnection[TupleRow]

# Any of these lets a role read every owner's rows or turn the policies off.
_CAN_ESCAPE_ISOLATION = """
SELECT role.rolsuper
    OR role.rolbypassrls
    OR EXISTS (
        SELECT FROM pg_catalog.pg_database
        WHERE datname = pg_catalog.current_database() AND datdba = role.oid
    )
    OR EXISTS (
        SELECT FROM pg_catalog.pg_class
        WHERE relnamespace = 'public'::regnamespace AND relowner = role.oid
    )
FROM pg_catalog.pg_roles AS role
WHERE role.rolname = current_user
"""


class OwnerContextError(RuntimeError):
    """An owner transaction was opened where its owner could leak or be overridden."""


def required(url: SecretStr | None, setting: str) -> str:
    if url is None:
        raise ConfigurationError(f"Invalid configuration: {setting} is required")
    return url.get_secret_value()


async def connect_runtime(conninfo: str) -> Connection:
    """Connect as the runtime role, refusing one that row-level security can't hold."""
    connection = await AsyncConnection.connect(conninfo, autocommit=True)
    row = await (await connection.execute(_CAN_ESCAPE_ISOLATION)).fetchone()
    if row != (False,):
        await connection.close()
        raise ConfigurationError(
            "Invalid configuration: the runtime database role can bypass row security"
        )
    return connection


@asynccontextmanager
async def owner_transaction(connection: Connection, owner_id: OwnerId) -> AsyncIterator[Connection]:
    """One transaction in which every query sees and writes only this owner's rows."""
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        # Inside another transaction, the owner would outlive this block.
        raise OwnerContextError("an owner transaction can't start inside another transaction")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)", (OWNER_SETTING, str(owner_id))
        )
        yield connection
