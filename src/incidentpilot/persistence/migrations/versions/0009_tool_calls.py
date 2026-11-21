"""The tool-call ledger and per-tool quotas (spec §7.2, §8.4, §11.1; errata E9, E13, E18
and E19).

Revision ID: 0009
Revises: 0008
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TOOLS = (
    "'get_service_metrics', 'search_logs', 'get_deployments', 'get_dependency_health',"
    " 'get_trace_summary'"
)
TOOL_CALL_STATES = "'RESERVED', 'DISPATCHED', 'COMPLETED', 'UNKNOWN', 'FAILED'"
ANSWERED = "'ok', 'empty', 'partial'"
ERROR_CODES = (
    "'INVALID_ARGUMENT', 'OUT_OF_SCOPE', 'STALE_LEASE', 'CANCELLED', 'QUOTA_EXCEEDED',"
    " 'TIMEOUT', 'SOURCE_UNAVAILABLE', 'RATE_LIMITED', 'CURSOR_INVALID', 'RESULT_TRUNCATED',"
    " 'INTERNAL_ERROR'"
)
EVENT_TYPES = (
    "'INVESTIGATION_QUEUED', 'ATTEMPT_STARTED', 'ATTEMPT_LOST', 'ATTEMPT_FAILED',"
    " 'RETRY_SCHEDULED', 'MODEL_CALL_DISPATCHED', 'MODEL_CALL_SETTLED', 'MODEL_CALL_UNKNOWN',"
    " 'MODEL_CALL_FAILED', 'TOOL_CALL_DISPATCHED', 'TOOL_CALL_COMPLETED', 'TOOL_CALL_UNKNOWN',"
    " 'TOOL_CALL_FAILED', 'EVIDENCE_COMMITTED', 'VALIDATION_STARTED',"
    " 'REPORT_REPAIR_REQUESTED', 'REPORT_PUBLISHED', 'INVESTIGATION_CANCELLED',"
    " 'INVESTIGATION_FAILED'"
)


def upgrade() -> None:
    # Tool calls get the same dispatch and close events as model calls.
    op.execute("ALTER TABLE run_events DROP CONSTRAINT run_events_event_type_check")
    op.execute(
        f"""
        ALTER TABLE run_events ADD CONSTRAINT run_events_event_type_check
            CHECK (event_type IN ({EVENT_TYPES}))
        """
    )

    # §11.1 and E13: the global tool quota lives on the investigation. Both counters can
    # be recomputed from the tool_calls rows (E18): reserved counts every call except one
    # released before it was sent, and dispatched counts calls that were sent.
    op.execute(
        """
        ALTER TABLE investigations
            ADD COLUMN tool_calls_reserved integer NOT NULL DEFAULT 0,
            ADD COLUMN tool_calls_dispatched integer NOT NULL DEFAULT 0,
            ADD CONSTRAINT investigations_tool_calls_counted CHECK (
                0 <= tool_calls_dispatched
                AND tool_calls_dispatched <= tool_calls_reserved
                AND tool_calls_reserved <= max_tool_calls
            )
        """
    )

    # E13: one row per investigation and tool, created when the investigation starts.
    # The cap is copied from the frozen configuration so the CHECK can hold it.
    # Investigations started before this migration have no rows, so their tool calls
    # are refused rather than unbounded.
    op.execute(
        f"""
        CREATE TABLE investigation_tool_quota (
            owner_id uuid NOT NULL REFERENCES owners (id),
            investigation_id uuid NOT NULL,
            tool_name text NOT NULL CHECK (tool_name IN ({TOOLS})),
            cap integer NOT NULL CHECK (cap > 0),
            reserved integer NOT NULL DEFAULT 0,
            dispatched integer NOT NULL DEFAULT 0,
            PRIMARY KEY (owner_id, investigation_id, tool_name),
            CONSTRAINT investigation_tool_quota_investigation_fkey
                FOREIGN KEY (owner_id, investigation_id) REFERENCES investigations (owner_id, id),
            CONSTRAINT investigation_tool_quota_counted
                CHECK (0 <= dispatched AND dispatched <= reserved AND reserved <= cap)
        )
        """
    )
    for statement in owner_isolation("investigation_tool_quota"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT, UPDATE ON investigation_tool_quota TO incidentpilot_runtime")

    # E18: the state is separate from the tool's answer. DISPATCHED commits before the
    # adapter is called, so dispatched_at tells a call that was sent from one that
    # wasn't, whatever state it ends in.
    op.execute(
        f"""
        CREATE TABLE tool_calls (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            investigation_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            invocation_id uuid NOT NULL,
            -- The model's own ID for the call, kept to trace it. It isn't trusted.
            provider_call_id text NOT NULL CHECK (length(provider_call_id) BETWEEN 1 AND 128),
            tool_name text NOT NULL,
            arguments_sha256 text NOT NULL CHECK (arguments_sha256 ~ '^[0-9a-f]{{64}}$'),
            state text NOT NULL DEFAULT 'RESERVED' CHECK (state IN ({TOOL_CALL_STATES})),
            result_status text CHECK (result_status IN ({ANSWERED}, 'error')),
            error_code text CHECK (error_code IN ({ERROR_CODES})),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            dispatched_at timestamptz,
            ended_at timestamptz,
            PRIMARY KEY (owner_id, id),
            -- An invocation is one logical call within an attempt (§7.2).
            CONSTRAINT tool_calls_invocation UNIQUE (owner_id, attempt_id, invocation_id),
            CONSTRAINT tool_calls_attempt_fkey FOREIGN KEY (owner_id, attempt_id)
                REFERENCES attempts (owner_id, id),
            CONSTRAINT tool_calls_investigation_fkey FOREIGN KEY (owner_id, investigation_id)
                REFERENCES investigations (owner_id, id),
            CONSTRAINT tool_calls_quota_fkey FOREIGN KEY (owner_id, investigation_id, tool_name)
                REFERENCES investigation_tool_quota (owner_id, investigation_id, tool_name),
            CONSTRAINT tool_calls_dispatched_when_sent CHECK (
                CASE state
                    WHEN 'RESERVED' THEN dispatched_at IS NULL
                    WHEN 'FAILED' THEN true
                    ELSE dispatched_at IS NOT NULL
                END
            ),
            CONSTRAINT tool_calls_result_by_state CHECK (
                CASE state
                    WHEN 'COMPLETED' THEN result_status IN ({ANSWERED}) AND error_code IS NULL
                    WHEN 'FAILED' THEN result_status = 'error' AND error_code IS NOT NULL
                    ELSE result_status IS NULL AND error_code IS NULL
                END
            ),
            CONSTRAINT tool_calls_ended_when_final
                CHECK ((state IN ('RESERVED', 'DISPATCHED')) = (ended_at IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX tool_calls_attempt_created ON tool_calls (attempt_id, created_at)")
    for statement in owner_isolation("tool_calls"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT, UPDATE ON tool_calls TO incidentpilot_runtime")

    # §7.2: which evidence each call returned. Both are immutable once written.
    op.execute(
        """
        CREATE TABLE tool_evidence (
            owner_id uuid NOT NULL REFERENCES owners (id),
            tool_call_id uuid NOT NULL,
            evidence_id uuid NOT NULL,
            PRIMARY KEY (owner_id, tool_call_id, evidence_id),
            CONSTRAINT tool_evidence_tool_call_fkey FOREIGN KEY (owner_id, tool_call_id)
                REFERENCES tool_calls (owner_id, id),
            CONSTRAINT tool_evidence_evidence_fkey FOREIGN KEY (owner_id, evidence_id)
                REFERENCES evidence (owner_id, id)
        )
        """
    )
    for statement in owner_isolation("tool_evidence"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON tool_evidence TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
