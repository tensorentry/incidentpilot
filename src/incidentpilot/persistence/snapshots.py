"""The snapshot registry (errata E7 and E19). Callers run these inside owner_transaction."""

import json
from dataclasses import dataclass

from psycopg import errors
from psycopg.types.json import Jsonb

from incidentpilot.domain.ids import SnapshotId
from incidentpilot.domain.snapshot import SnapshotManifest
from incidentpilot.persistence.database import Connection

# SQL reads keywords in any case, so queries short enough for mutation to flip their
# case are kept at module level, where it doesn't reach.
_SELECT_SNAPSHOT = "SELECT manifest, content_sha256 FROM snapshots WHERE id = %s"


class SnapshotError(Exception):
    """A snapshot can't be imported as given."""


@dataclass(frozen=True)
class StoredSnapshot:
    manifest: SnapshotManifest
    content_sha256: str


async def insert_snapshot(
    connection: Connection, manifest: SnapshotManifest, content_sha256: str
) -> bool:
    """Store a snapshot for the transaction's owner. False if it was already there.

    The same ID with different content is refused: a snapshot never changes.
    """
    try:
        cursor = await connection.execute(
            """
            INSERT INTO snapshots
                (owner_id, id, parent_snapshot_id, adapter_version, content_sha256, manifest)
            VALUES (current_owner_id(), %s, %s, %s, %s, %s)
            ON CONFLICT (owner_id, id) DO NOTHING
            """,
            (
                manifest.snapshot_id,
                manifest.parent_snapshot_id,
                manifest.adapter_version,
                content_sha256,
                Jsonb(manifest.model_dump(mode="json")),
            ),
        )
    except errors.ForeignKeyViolation as error:
        raise SnapshotError(_missing(error, manifest)) from None
    if cursor.rowcount == 1:
        return True
    stored = await get_snapshot(connection, manifest.snapshot_id)
    if stored is None or stored.content_sha256 != content_sha256:
        raise SnapshotError(
            f"snapshot {manifest.snapshot_id} is already imported with different content"
        )
    return False


async def get_snapshot(connection: Connection, snapshot_id: SnapshotId) -> StoredSnapshot | None:
    cursor = await connection.execute(_SELECT_SNAPSHOT, (snapshot_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    manifest, content_sha256 = row
    # Through JSON, so strict validation reads timestamps and enums as it does from files.
    return StoredSnapshot(
        SnapshotManifest.model_validate_json(json.dumps(manifest)), str(content_sha256)
    )


def _missing(error: errors.ForeignKeyViolation, manifest: SnapshotManifest) -> str:
    if error.diag.constraint_name == "snapshots_parent_fkey":
        return (
            f"snapshot {manifest.snapshot_id} needs its parent "
            f"{manifest.parent_snapshot_id} imported first"
        )
    return "the owner doesn't exist"
