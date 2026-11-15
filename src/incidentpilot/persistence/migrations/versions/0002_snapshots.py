"""The snapshot registry, one per owner (errata E7 and E19).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The manifest is stored whole, and the columns that keys and constraints need are
    # copied out of it. The CHECKs keep the two from disagreeing.
    op.execute(
        """
        CREATE TABLE snapshots (
            owner_id uuid NOT NULL CONSTRAINT snapshots_owner_fkey REFERENCES owners (id),
            id text NOT NULL CHECK (id ~ '^[a-z0-9][a-z0-9-]{0,127}$'),
            parent_snapshot_id text,
            adapter_version text NOT NULL CHECK (adapter_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
            content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
            manifest jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            PRIMARY KEY (owner_id, id),
            -- Composite, so a snapshot can only descend from its own owner's snapshots.
            CONSTRAINT snapshots_parent_fkey FOREIGN KEY (owner_id, parent_snapshot_id)
                REFERENCES snapshots (owner_id, id),
            CONSTRAINT snapshots_not_own_parent CHECK (parent_snapshot_id <> id),
            CONSTRAINT snapshots_manifest_matches CHECK (
                manifest ->> 'schema_version' = '1'
                AND manifest ->> 'snapshot_id' = id
                AND manifest ->> 'adapter_version' = adapter_version
                AND (manifest ->> 'parent_snapshot_id') IS NOT DISTINCT FROM parent_snapshot_id
            )
        )
        """
    )
    for statement in owner_isolation("snapshots"):
        op.execute(statement)
    # No UPDATE or DELETE: a snapshot never changes once imported.
    op.execute("GRANT SELECT, INSERT ON snapshots TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
