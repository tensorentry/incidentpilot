"""Real PostgreSQL for every test (spec §17.1), with two owners (erratum E19).

The migrations run once into a template database, and each test gets its own copy.
Tests can't see each other's rows, and parallel test processes, as mutation testing
runs, can't collide. The template is rebuilt whenever the persistence code changes.
"""

from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from incidentpilot.persistence import migrate
from incidentpilot.settings import load_settings
from tests.integration.harness import (
    OWNER_B,
    TEMPLATE,
    TEMPLATE_LOCK,
    ScratchDatabase,
    Servers,
    admin,
    fingerprint,
    scratch,
    sweep,
)


@pytest.fixture(scope="session")
def servers() -> Servers:
    settings = load_settings()
    if settings.database_url is None or settings.migration_database_url is None:
        pytest.fail(
            "integration tests need INCIDENTPILOT_DATABASE_URL and "
            "INCIDENTPILOT_MIGRATION_DATABASE_URL; see README",
            pytrace=False,
        )
    return Servers(
        runtime=settings.database_url.get_secret_value(),
        migrator=settings.migration_database_url.get_secret_value(),
    )


@pytest.fixture(scope="session")
def run_pid(servers: Servers) -> Iterator[int]:
    """Held open for the whole run, so other runs know this run's copies are in use."""
    with admin(servers) as connection:
        yield connection.info.backend_pid


@pytest.fixture(scope="session")
def template(servers: Servers, run_pid: int) -> str:
    """Build the template if it's missing or stale. A lock keeps processes from racing."""
    expected = fingerprint()
    name = sql.Identifier(TEMPLATE)
    with admin(servers) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (TEMPLATE_LOCK,))
        sweep(connection)
        row = connection.execute(
            "SELECT pg_catalog.shobj_description(oid, 'pg_database') "
            "FROM pg_catalog.pg_database WHERE datname = %s",
            (TEMPLATE,),
        ).fetchone()
        if row is None or row[0] != expected:
            if row is not None:
                connection.execute(sql.SQL("ALTER DATABASE {} IS_TEMPLATE false").format(name))
                connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))
            connection.execute(sql.SQL("CREATE DATABASE {}").format(name))
            migrate.upgrade(make_conninfo(servers.migrator, dbname=TEMPLATE))
            with psycopg.connect(make_conninfo(servers.migrator, dbname=TEMPLATE)) as seeding:
                seeding.execute("INSERT INTO owners (id, name) VALUES (%s, 'second')", (OWNER_B,))
            connection.execute(
                sql.SQL("ALTER DATABASE {} WITH IS_TEMPLATE true ALLOW_CONNECTIONS false").format(
                    name
                )
            )
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(name, sql.Literal(expected))
            )
        connection.execute("SELECT pg_advisory_unlock(%s)", (TEMPLATE_LOCK,))
    return TEMPLATE


@pytest.fixture
def database(servers: Servers, run_pid: int, template: str) -> Iterator[ScratchDatabase]:
    """A migrated database holding two owners, private to this test."""
    yield from scratch(servers, run_pid, template)


@pytest.fixture
def empty_database(servers: Servers, run_pid: int) -> Iterator[ScratchDatabase]:
    """A database no migration has touched."""
    yield from scratch(servers, run_pid, None)
