"""Owner isolation in PostgreSQL (erratum E19, ADR-014).

Row-level security limits every tenant table to the owner set for the current
transaction. An unset owner matches no row, so a forgotten context fails closed.
"""

import re
from uuid import UUID

from incidentpilot.domain.ids import OwnerId

RUNTIME_ROLE = "incidentpilot_runtime"
MIGRATOR_ROLE = "incidentpilot_migrator"
OWNER_SETTING = "app.owner_id"
OWNER_POLICY = "owner_isolation"
# Seeded by the first migration (erratum E10) until real authentication arrives.
LOCAL_OWNER_ID = OwnerId(UUID("00000000-0000-4000-8000-000000000001"))

_TABLE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


def owner_isolation(table: str) -> list[str]:
    """The statements a migration runs to put one tenant table under owner isolation.

    FORCE applies the policy to the table's owner too, so only a superuser or a role
    with BYPASSRLS could skip it, and the runtime role is neither.
    """
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError(f"not a plain table name: {table!r}")
    owned = "owner_id = current_owner_id()"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {OWNER_POLICY} ON {table} USING ({owned}) WITH CHECK ({owned})",
    ]
