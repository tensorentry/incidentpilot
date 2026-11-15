"""Import snapshots from observation files for one owner (errata E7 and E19).

`python -m incidentpilot.application.snapshots [--owner UUID] [--root DIR]`

Each owner gets its own copy, so the same file imported for two owners becomes two
snapshots. Importing a file again changes nothing.
"""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from incidentpilot.adapters.fixture_bundle import ObservationBundle, bundle_sha256
from incidentpilot.domain.ids import OwnerId
from incidentpilot.persistence.database import (
    Connection,
    connect_runtime,
    owner_transaction,
    required,
)
from incidentpilot.persistence.isolation import LOCAL_OWNER_ID
from incidentpilot.persistence.snapshots import SnapshotError, insert_snapshot
from incidentpilot.settings import ConfigurationError, load_settings

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


async def import_directory(connection: Connection, owner_id: OwnerId, root: Path) -> list[str]:
    lines = []
    for path in sorted(root.glob("*.json")):
        created = await import_file(connection, owner_id, path)
        lines.append(f"{'imported' if created else 'already imported'} {path.stem}")
    return lines


async def run(owner_id: OwnerId, root: Path) -> list[str]:
    settings = load_settings()
    connection = await connect_runtime(
        required(settings.database_url, "INCIDENTPILOT_DATABASE_URL")
    )
    async with connection:
        return await import_directory(connection, owner_id, root)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", type=UUID, default=LOCAL_OWNER_ID)
    parser.add_argument("--root", type=Path, default=OBSERVATIONS)
    arguments = parser.parse_args(argv)
    try:
        lines = asyncio.run(run(OwnerId(arguments.owner), arguments.root))
    except (ConfigurationError, SnapshotError) as error:
        raise SystemExit(str(error)) from None
    print("\n".join(lines))


if __name__ == "__main__":
    main()
