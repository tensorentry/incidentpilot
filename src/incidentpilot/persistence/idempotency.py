"""Idempotent request records (spec §12.1, invariant 11). Run inside owner_transaction."""

import re
from dataclasses import dataclass
from uuid import UUID

from incidentpilot.persistence.database import Connection

# Matches the table's CHECK, so a bad key is refused before it reaches the database.
KEY = re.compile(r"[!-~]{1,128}")
_FIND = (
    "SELECT request_sha256, resource_id FROM idempotency_requests WHERE operation = %s AND key = %s"
)


@dataclass(frozen=True)
class RecordedRequest:
    request_sha256: str
    resource_id: UUID


async def claim(
    connection: Connection, operation: str, key: str, request_sha256: str, resource_id: UUID
) -> bool:
    """Record this request under its key. False if the key already has a request.

    A concurrent claim of the same key waits here until the first transaction ends,
    so exactly one of them creates the resource.
    """
    cursor = await connection.execute(
        """
        INSERT INTO idempotency_requests (owner_id, operation, key, request_sha256, resource_id)
        VALUES (current_owner_id(), %s, %s, %s, %s)
        ON CONFLICT (owner_id, operation, key) DO NOTHING
        """,
        (operation, key, request_sha256, resource_id),
    )
    return cursor.rowcount == 1


async def recorded(connection: Connection, operation: str, key: str) -> RecordedRequest | None:
    row = await (await connection.execute(_FIND, (operation, key))).fetchone()
    if row is None:
        return None
    request_sha256, resource_id = row
    return RecordedRequest(str(request_sha256), UUID(str(resource_id)))
