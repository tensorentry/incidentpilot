"""Import snapshots from observation files for one owner (errata E7 and E19).

The CLI's `import-snapshots` command calls this. Each owner gets its own copy, so the
same file imported for two owners becomes two snapshots. Importing a file again
changes nothing.
"""

from pathlib import Path

from pydantic import ValidationError

from incidentpilot.adapters.fixture_bundle import ObservationBundle, bundle_sha256
from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence.database import Connection, owner_transaction
from incidentpilot.persistence.snapshots import SnapshotError, insert_snapshot

OBSERVATIONS = Path("scenarios/observations")


def read_bundle(path: Path) -> ObservationBundle:
    try:
        bundle = ObservationBundle.model_validate_json(path.read_bytes())
    except ValidationError:
        # The errors would quote the file's content.
        raise SnapshotError(f"{path.name} isn't a valid observation bundle") from None
    if path.name != f"{bundle.manifest.snapshot_id}.json":
        raise SnapshotError(f"{path.name} holds snapshot {bundle.manifest.snapshot_id}")
    return bundle


async def import_file(connection: Connection, owner_id: OwnerId, path: Path) -> bool:
    """Import one file. False if this owner already has the snapshot."""
    bundle = read_bundle(path)
    async with owner_transaction(connection, owner_id):
        return await insert_snapshot(connection, bundle.manifest, bundle_sha256(bundle))


async def import_directory(
    connection: Connection, owner_id: OwnerId, root: Path
) -> list[tuple[str, bool]]:
    """Import every bundle under `root`, returning each snapshot and whether it was new."""
    return [
        (path.stem, await import_file(connection, owner_id, path))
        for path in sorted(root.glob("*.json"))
    ]
