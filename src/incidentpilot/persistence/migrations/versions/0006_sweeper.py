"""The sweeper's discovery function (spec §6.1, §2.4; errata E9 and E19).

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT (lease_expires_at, deadline_at) ON investigations TO incidentpilot_scheduler"
    )
    op.execute("GRANT CREATE ON SCHEMA public TO incidentpilot_scheduler")
    op.execute("SET LOCAL ROLE incidentpilot_scheduler")
    # E19's expired-lease discovery, widened to the other two things the sweeper ends:
    # queued work older than the queue age, and retry waits past their deadline. It
    # locks one such investigation, whoever owns it, and returns only its owner and ID.
    op.execute(
        """
        CREATE FUNCTION sweepable_investigation(max_queue_age interval)
        RETURNS TABLE (owner_id uuid, investigation_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        BEGIN ATOMIC
            SELECT owner_id, id FROM public.investigations
            WHERE (status IN ('RUNNING', 'VALIDATING')
                   AND lease_expires_at <= pg_catalog.clock_timestamp())
               OR (status = 'QUEUED'
                   AND created_at <= pg_catalog.clock_timestamp() - max_queue_age)
               OR (status = 'RETRY_WAIT' AND deadline_at <= pg_catalog.clock_timestamp())
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED;
        END
        """
    )
    op.execute("REVOKE ALL ON FUNCTION sweepable_investigation(interval) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION sweepable_investigation(interval) TO incidentpilot_runtime"
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM incidentpilot_scheduler")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
