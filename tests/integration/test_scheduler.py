"""The scheduler role and its cross-owner functions (errata E11 and E19)."""

from incidentpilot.application.investigations import start_investigation
from incidentpilot.domain.enums import RunMode
from incidentpilot.persistence.database import owner_transaction
from tests.integration.harness import OWNER_A, OWNER_B, ScratchDatabase
from tests.integration.seeding import seed, start_request

# Adding a function here needs an erratum (E19).
SCHEDULER_FUNCTIONS = {"queued_investigation_count"}


async def test_the_queued_count_sees_every_owner(database: ScratchDatabase) -> None:
    async with database.runtime() as connection:
        for owner, key in [(OWNER_A, "a1"), (OWNER_A, "a2"), (OWNER_B, "b1")]:
            seeded = await seed(connection, owner, key=f"seed-{key}")
            await start_investigation(
                connection, owner, RunMode.OFFLINE_DEMO, seeded.incident_id, key, start_request()
            )

        async with owner_transaction(connection, OWNER_B):
            cursor = await connection.execute(
                "SELECT queued_investigation_count(), (SELECT count(*) FROM investigations)"
            )
            assert await cursor.fetchone() == (3, 1)
        cursor = await connection.execute("SELECT queued_investigation_count()")
        assert await cursor.fetchone() == (3,)


async def test_scheduler_functions_are_definers_with_a_pinned_path(
    database: ScratchDatabase,
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT proname, prosecdef, proconfig, pg_get_userbyid(proowner),
                   pg_catalog.pg_get_function_result(oid),
                   pg_catalog.has_function_privilege('incidentpilot_runtime', oid, 'EXECUTE'),
                   EXISTS (SELECT FROM pg_catalog.aclexplode(proacl) WHERE grantee = 0)
            FROM pg_catalog.pg_proc
            WHERE pronamespace = 'public'::regnamespace
              AND proowner = 'incidentpilot_scheduler'::regrole
            """
        )
        rows = await cursor.fetchall()

    assert {name for name, *_ in rows} == SCHEDULER_FUNCTIONS
    for _, definer, config, owner, result, runtime_may, public_may in rows:
        assert (definer, config, owner) == (
            True,
            ["search_path=pg_catalog, pg_temp"],
            "incidentpilot_scheduler",
        )
        # IDs and counts only, never tenant content.
        assert result in {"bigint", "uuid", "SETOF uuid"}
        assert (runtime_may, public_may) == (True, False)


async def test_the_scheduler_role_cannot_log_in_and_is_not_inherited(
    database: ScratchDatabase,
) -> None:
    async with database.migrator() as connection:
        cursor = await connection.execute(
            """
            SELECT role.rolcanlogin, role.rolbypassrls,
                   pg_catalog.pg_has_role('incidentpilot_runtime', role.oid, 'MEMBER'),
                   pg_catalog.pg_has_role('incidentpilot_migrator', role.oid, 'USAGE')
            FROM pg_catalog.pg_roles AS role WHERE rolname = 'incidentpilot_scheduler'
            """
        )
        # The migrator may SET ROLE to it, but never holds its rights by inheritance.
        assert await cursor.fetchone() == (False, True, False, False)
