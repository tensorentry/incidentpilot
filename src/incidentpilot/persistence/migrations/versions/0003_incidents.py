"""Incidents and idempotent request records (spec §7.2, §12.1; erratum E19).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE incidents (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 2000),
            service text NOT NULL CHECK (service ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
            severity text NOT NULL CHECK (severity IN ('sev1', 'sev2', 'sev3', 'sev4')),
            window_start timestamptz NOT NULL,
            window_end timestamptz NOT NULL,
            sanitizer_version text NOT NULL CHECK (sanitizer_version ~ '^[0-9]+$'),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            PRIMARY KEY (owner_id, id),
            CONSTRAINT incidents_window_ordered CHECK (window_end > window_start),
            CONSTRAINT incidents_window_bounded
                CHECK (window_end - window_start <= interval '24 hours')
        )
        """
    )
    op.execute("CREATE INDEX incidents_owner_created ON incidents (owner_id, created_at)")
    for statement in owner_isolation("incidents"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON incidents TO incidentpilot_runtime")

    # The key is scoped to its owner and operation. The fingerprint covers the normalized
    # request, so a retry returns the original resource and a changed body conflicts.
    op.execute(
        """
        CREATE TABLE idempotency_requests (
            owner_id uuid NOT NULL REFERENCES owners (id),
            operation text NOT NULL
                CHECK (operation IN ('create_incident', 'start_investigation')),
            key text NOT NULL CHECK (key ~ '^[!-~]{1,128}$'),
            request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            resource_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            PRIMARY KEY (owner_id, operation, key)
        )
        """
    )
    for statement in owner_isolation("idempotency_requests"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON idempotency_requests TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
