"""Owners, the owner-isolation function and locked-down defaults (ADR-014, erratum E10).

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only roles granted access can see the schema, and a function the migrator
    # creates later, such as a SECURITY DEFINER scheduler function, can't be called
    # until it's granted explicitly.
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA public TO incidentpilot_runtime")
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")

    # The setting reads back as '' rather than NULL once a transaction that set it has
    # ended, so NULLIF keeps an unset owner from matching anything.
    op.execute(
        """
        CREATE FUNCTION current_owner_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        RETURN NULLIF(pg_catalog.current_setting('app.owner_id', true), '')::uuid
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION current_owner_id() TO incidentpilot_runtime")

    op.execute(
        """
        CREATE TABLE owners (
            id uuid PRIMARY KEY,
            name text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,63}$'),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
        )
        """
    )
    op.execute(
        "INSERT INTO owners (id, name) VALUES ('00000000-0000-4000-8000-000000000001', 'local')"
    )
    op.execute("GRANT SELECT ON owners TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
