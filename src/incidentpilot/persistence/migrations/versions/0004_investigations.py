"""Investigations, their event log and the admission count (spec §7.2, §7.3; errata E8,
E11 and E19).

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

STATUSES = "'QUEUED', 'RUNNING', 'RETRY_WAIT', 'VALIDATING', 'COMPLETED', 'CANCELLED', 'FAILED'"
EVENT_TYPES = (
    "'INVESTIGATION_QUEUED', 'ATTEMPT_STARTED', 'ATTEMPT_LOST', 'ATTEMPT_FAILED',"
    " 'RETRY_SCHEDULED', 'MODEL_CALL_DISPATCHED', 'MODEL_CALL_SETTLED', 'MODEL_CALL_UNKNOWN',"
    " 'MODEL_CALL_FAILED', 'TOOL_CALL_COMPLETED', 'TOOL_CALL_FAILED', 'EVIDENCE_COMMITTED',"
    " 'VALIDATION_STARTED', 'REPORT_REPAIR_REQUESTED', 'REPORT_PUBLISHED',"
    " 'INVESTIGATION_CANCELLED', 'INVESTIGATION_FAILED'"
)


def upgrade() -> None:
    # The frozen configuration is stored in typed columns (E8), and its digest is kept
    # alongside so a reader can tell the values haven't changed since admission.
    op.execute(
        f"""
        CREATE TABLE investigations (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            incident_id uuid NOT NULL,
            parent_investigation_id uuid,
            snapshot_id text NOT NULL,
            status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ({STATUSES})),
            mode text NOT NULL CHECK (mode IN ('offline_demo', 'live')),
            model_profile text NOT NULL CHECK (model_profile ~ '^[a-z0-9][a-z0-9-]{{0,63}}$'),
            window_start timestamptz NOT NULL,
            window_end timestamptz NOT NULL,
            allowed_services text[] NOT NULL CHECK (cardinality(allowed_services) BETWEEN 1 AND 50),
            max_tool_calls integer NOT NULL CHECK (max_tool_calls > 0),
            max_calls_per_tool integer NOT NULL
                CHECK (max_calls_per_tool > 0 AND max_calls_per_tool <= max_tool_calls),
            max_model_requests integer NOT NULL CHECK (max_model_requests > 0),
            max_recovery_attempts integer NOT NULL CHECK (max_recovery_attempts >= 0),
            deadline_seconds integer NOT NULL CHECK (deadline_seconds > 0),
            finalization_reserve_seconds integer NOT NULL
                CHECK (finalization_reserve_seconds > 0
                       AND finalization_reserve_seconds < deadline_seconds),
            spend_cap_micro_usd bigint NOT NULL CHECK (spend_cap_micro_usd > 0),
            config_sha256 text NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{{64}}$'),
            current_attempt_id uuid,
            lease_epoch bigint NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
            lease_expires_at timestamptz,
            eligible_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            deadline_at timestamptz,
            attempts_started integer NOT NULL DEFAULT 0 CHECK (attempts_started >= 0),
            repair_requests_used integer NOT NULL DEFAULT 0
                CHECK (repair_requests_used BETWEEN 0 AND 1),
            next_event_seq bigint NOT NULL DEFAULT 1 CHECK (next_event_seq >= 1),
            termination_reason text,
            row_version bigint NOT NULL DEFAULT 0 CHECK (row_version >= 0),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            PRIMARY KEY (owner_id, id),
            CONSTRAINT investigations_window_ordered CHECK (window_end > window_start),
            CONSTRAINT investigations_window_bounded
                CHECK (window_end - window_start <= interval '2 hours'),
            -- Composite, so an investigation can only point at its own owner's rows.
            CONSTRAINT investigations_incident_fkey FOREIGN KEY (owner_id, incident_id)
                REFERENCES incidents (owner_id, id),
            CONSTRAINT investigations_snapshot_fkey FOREIGN KEY (owner_id, snapshot_id)
                REFERENCES snapshots (owner_id, id),
            CONSTRAINT investigations_parent_fkey FOREIGN KEY (owner_id, parent_investigation_id)
                REFERENCES investigations (owner_id, id)
        )
        """
    )
    op.execute("CREATE INDEX investigations_claimable ON investigations (status, eligible_at)")
    op.execute("CREATE INDEX investigations_owner_created ON investigations (owner_id, created_at)")
    for statement in owner_isolation("investigations"):
        op.execute(statement)
    # State changes are UPDATEs. Nothing is ever deleted.
    op.execute("GRANT SELECT, INSERT, UPDATE ON investigations TO incidentpilot_runtime")

    op.execute(
        f"""
        CREATE TABLE run_events (
            owner_id uuid NOT NULL REFERENCES owners (id),
            investigation_id uuid NOT NULL,
            seq bigint NOT NULL CHECK (seq >= 1),
            event_type text NOT NULL CHECK (event_type IN ({EVENT_TYPES})),
            payload_version integer NOT NULL CHECK (payload_version = 1),
            payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
            PRIMARY KEY (owner_id, investigation_id, seq),
            CONSTRAINT run_events_investigation_fkey FOREIGN KEY (owner_id, investigation_id)
                REFERENCES investigations (owner_id, id)
        )
        """
    )
    for statement in owner_isolation("run_events"):
        op.execute(statement)
    # §7.3: the runtime role can't change or delete events.
    op.execute("GRANT SELECT, INSERT ON run_events TO incidentpilot_runtime")

    # E11 and E19: the queue limit counts every owner's queued work, which row-level
    # security hides. The scheduler role bypasses it and owns this function, which
    # returns a count and nothing else. It may read only the status column.
    op.execute("GRANT USAGE ON SCHEMA public TO incidentpilot_scheduler")
    op.execute("GRANT SELECT (status) ON investigations TO incidentpilot_scheduler")
    op.execute("GRANT CREATE ON SCHEMA public TO incidentpilot_scheduler")
    op.execute("SET LOCAL ROLE incidentpilot_scheduler")
    op.execute(
        """
        CREATE FUNCTION queued_investigation_count() RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        RETURN (SELECT count(*) FROM public.investigations WHERE status = 'QUEUED')
        """
    )
    # Functions get EXECUTE for PUBLIC by default, and the migrator's default privileges
    # don't cover functions another role creates. Only the owner can take it back: a
    # REVOKE from anyone else is silently ignored. So these run as the scheduler role.
    op.execute("REVOKE ALL ON FUNCTION queued_investigation_count() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION queued_investigation_count() TO incidentpilot_runtime")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM incidentpilot_scheduler")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
