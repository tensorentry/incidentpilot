"""Real PostgreSQL for integration tests (spec §17.1), with two owners (erratum E19)."""

import hashlib
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence import migrate
from incidentpilot.persistence.database import Connection, connect_runtime
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID, RUNTIME_ROLE

OWNER_A = LOCAL_OWNER_ID
OWNER_B = OwnerId(UUID("00000000-0000-4000-8000-000000000002"))
TEMPLATE = "incidentpilot_test_template"
PERSISTENCE = Path(migrate.__file__).parent
# Any fixed key works; it only has to match across test processes.
TEMPLATE_LOCK = 4_212_031
# A copy is named for the server process of its test run's admin connection, so a copy
# whose process is gone was left behind by a run that was killed.
COPY_NAME = re.compile(r"incidentpilot_test_(\d+)_[0-9a-f]{32}")


@dataclass(frozen=True)
class Servers:
    # Connection strings hold passwords, which a failing test would otherwise print.
    runtime: str = field(repr=False)
    migrator: str = field(repr=False)


@dataclass(frozen=True)
class ScratchDatabase:
    name: str
    servers: Servers

    def conninfo(self, role: Literal["runtime", "migrator"]) -> str:
        base = self.servers.runtime if role == "runtime" else self.servers.migrator
        return make_conninfo(base, dbname=self.name)

    @asynccontextmanager
    async def runtime(self) -> AsyncIterator[Connection]:
        connection = await connect_runtime(self.conninfo("runtime"))
        async with connection:
            yield connection

    @asynccontextmanager
    async def migrator(self) -> AsyncIterator[Connection]:
        connection = await psycopg.AsyncConnection.connect(
            self.conninfo("migrator"), autocommit=True
        )
        async with connection:
            yield connection


def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(PERSISTENCE.rglob("*.py")):
        digest.update(path.relative_to(PERSISTENCE).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    # The harness seeds the template too, so its own code is part of what was built.
    for harness in (Path(__file__), Path(__file__).with_name("conftest.py")):
        digest.update(harness.read_bytes() + b"\0")
    return digest.hexdigest()


def admin(servers: Servers) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(make_conninfo(servers.migrator, dbname="postgres"), autocommit=True)


def sweep(connection: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    """Drop the copies left by killed test runs. A live run's copies are kept."""
    live = {pid for (pid,) in connection.execute("SELECT pid FROM pg_catalog.pg_stat_activity")}
    names = connection.execute(
        "SELECT datname FROM pg_catalog.pg_database"
        " WHERE datname LIKE 'incidentpilot\\_test\\_%%' AND datname <> %s ORDER BY datname",
        (TEMPLATE,),
    )
    dropped = []
    for (name,) in names.fetchall():
        assert isinstance(name, str)
        copy = COPY_NAME.fullmatch(name)
        if copy is None or int(copy[1]) not in live:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
            dropped.append(name)
    return dropped


def scratch(servers: Servers, run_pid: int, template: str | None) -> Iterator[ScratchDatabase]:
    name = f"incidentpilot_test_{run_pid}_{uuid4().hex}"
    identifier = sql.Identifier(name)
    create = sql.SQL("CREATE DATABASE {}").format(identifier)
    if template is not None:
        create += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
    with admin(servers) as connection:
        connection.execute(create)
        # A copy doesn't inherit database privileges, so match the real database's.
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(identifier))
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                identifier, sql.Identifier(RUNTIME_ROLE)
            )
        )
    try:
        yield ScratchDatabase(name, servers)
    finally:
        with admin(servers) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier))
