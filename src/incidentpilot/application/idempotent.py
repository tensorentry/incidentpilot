"""Idempotent creation (spec §12.1, invariant 11), shared by every create operation."""

from uuid import UUID

from incidentpilot.application.errors import IdempotencyConflict, RequestError
from incidentpilot.persistence import idempotency
from incidentpilot.persistence.database import Connection


def check_key(key: str) -> None:
    if not idempotency.KEY.fullmatch(key):
        raise RequestError("an idempotency key is 1 to 128 printable ASCII characters")


async def claim_or_replay(
    connection: Connection, operation: str, key: str, request_sha256: str, new_id: UUID
) -> UUID | None:
    """Claim the key for `new_id`, or return the resource an identical request created.

    None means the caller must create the resource now, in the same transaction.
    """
    if await idempotency.claim(connection, operation, key, request_sha256, new_id):
        return None
    earlier = await idempotency.recorded(connection, operation, key)
    if earlier is None or earlier.request_sha256 != request_sha256:
        raise IdempotencyConflict(f"idempotency key {key!r} was used for a different request")
    return earlier.resource_id
