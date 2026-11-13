"""The roles the application connects as (ADR-014)."""

import re

import pytest

from incidentpilot.persistence.database import connect_runtime
from incidentpilot.settings import ConfigurationError
from tests.integration.harness import ScratchDatabase


async def test_the_runtime_role_has_no_special_attributes(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        cursor = await connection.execute(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication, rolinherit"
            " FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        assert await cursor.fetchone() == (False, False, False, False, False, False)


async def test_the_runtime_role_owns_nothing(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT
                (SELECT count(*) FROM pg_catalog.pg_class WHERE relowner = role.oid)
              + (SELECT count(*) FROM pg_catalog.pg_proc WHERE proowner = role.oid)
              + (SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspowner = role.oid)
              + (SELECT count(*) FROM pg_catalog.pg_database WHERE datdba = role.oid)
            FROM pg_catalog.pg_roles AS role WHERE rolname = 'incidentpilot_runtime'
            """
        )
        assert await cursor.fetchone() == (0,)


async def test_the_migrator_is_not_a_superuser(database: ScratchDatabase) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            "SELECT current_user, rolsuper, rolbypassrls"
            " FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        assert await cursor.fetchone() == ("incidentpilot_migrator", False, False)


async def test_a_runtime_connection_refuses_a_role_that_owns_the_schema(
    database: ScratchDatabase,
) -> None:
    message = "Invalid configuration: the runtime database role can bypass row security"
    with pytest.raises(ConfigurationError, match=rf"^{re.escape(message)}$"):
        await connect_runtime(database.conninfo("migrator"))
