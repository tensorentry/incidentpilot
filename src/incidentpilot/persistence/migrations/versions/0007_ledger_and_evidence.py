"""The model-call ledger and committed evidence (spec §7.2, §7.3, §11.1; errata E9, E14,
E15, E18 and E19).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

MODEL_CALL_STATES = "'RESERVED', 'DISPATCHED', 'SETTLED', 'UNKNOWN', 'FAILED'"
EVIDENCE_KINDS = "'metric_series_window', 'log_record', 'deployment_record', 'span_summary'"
WINDOWED_KINDS = "'metric_series_window', 'span_summary'"
VISIBILITY_REASONS = "'tool_result'"


def upgrade() -> None:
    # §11.1: the budget lives on the investigation. Each counter can be recomputed from
    # the model_calls rows (E18):
    # - requests reserved: every call except one released before it was sent;
    # - requests dispatched, settled and unknown: calls that reached those states;
    # - cost reserved: estimates held by calls still in flight;
    # - cost known: actual cost of settled calls;
    # - cost unknown: estimates kept by calls that were sent but never settled.
    op.execute(
        """
        ALTER TABLE investigations
            ADD COLUMN model_requests_reserved integer NOT NULL DEFAULT 0,
            ADD COLUMN model_requests_dispatched integer NOT NULL DEFAULT 0,
            ADD COLUMN model_requests_settled integer NOT NULL DEFAULT 0,
            ADD COLUMN model_requests_unknown integer NOT NULL DEFAULT 0,
            ADD COLUMN cost_reserved_micro_usd bigint NOT NULL DEFAULT 0,
            ADD COLUMN cost_known_micro_usd bigint NOT NULL DEFAULT 0,
            ADD COLUMN cost_unknown_micro_usd bigint NOT NULL DEFAULT 0,
            ADD COLUMN input_tokens_actual bigint NOT NULL DEFAULT 0,
            ADD COLUMN output_tokens_actual bigint NOT NULL DEFAULT 0,
            ADD CONSTRAINT investigations_model_requests_counted CHECK (
                0 <= model_requests_settled
                AND 0 <= model_requests_unknown
                AND model_requests_settled + model_requests_unknown <= model_requests_dispatched
                AND model_requests_dispatched <= model_requests_reserved
                AND model_requests_reserved <= max_model_requests
            ),
            ADD CONSTRAINT investigations_costs_nonnegative CHECK (
                cost_reserved_micro_usd >= 0
                AND cost_known_micro_usd >= 0
                AND cost_unknown_micro_usd >= 0
                AND input_tokens_actual >= 0
                AND output_tokens_actual >= 0
            )
        """
    )

    # E18: DISPATCHED commits before the request is sent, so dispatched_at tells a call
    # that was sent from one that wasn't, whatever state it ends in.
    op.execute(
        f"""
        CREATE TABLE model_calls (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            investigation_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            state text NOT NULL DEFAULT 'RESERVED' CHECK (state IN ({MODEL_CALL_STATES})),
            reserved_cost_micro_usd bigint NOT NULL CHECK (reserved_cost_micro_usd >= 0),
            provider_request_id text CHECK (length(provider_request_id) BETWEEN 1 AND 256),
            input_tokens bigint CHECK (input_tokens >= 0),
            output_tokens bigint CHECK (output_tokens >= 0),
            actual_cost_micro_usd bigint CHECK (actual_cost_micro_usd >= 0),
            failure_category text,
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            dispatched_at timestamptz,
            ended_at timestamptz,
            PRIMARY KEY (owner_id, id),
            CONSTRAINT model_calls_attempt_fkey FOREIGN KEY (owner_id, attempt_id)
                REFERENCES attempts (owner_id, id),
            CONSTRAINT model_calls_investigation_fkey FOREIGN KEY (owner_id, investigation_id)
                REFERENCES investigations (owner_id, id),
            CONSTRAINT model_calls_dispatched_when_sent CHECK (
                CASE state
                    WHEN 'RESERVED' THEN dispatched_at IS NULL
                    WHEN 'FAILED' THEN true
                    ELSE dispatched_at IS NOT NULL
                END
            ),
            CONSTRAINT model_calls_usage_when_settled CHECK (
                (state = 'SETTLED') = (input_tokens IS NOT NULL)
                AND (state = 'SETTLED') = (output_tokens IS NOT NULL)
                AND (state = 'SETTLED') = (actual_cost_micro_usd IS NOT NULL)
            ),
            CONSTRAINT model_calls_failure_when_failed
                CHECK (failure_category IS NULL OR state = 'FAILED'),
            CONSTRAINT model_calls_ended_when_final
                CHECK ((state IN ('RESERVED', 'DISPATCHED')) = (ended_at IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX model_calls_attempt_created ON model_calls (attempt_id, created_at)")
    for statement in owner_isolation("model_calls"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT, UPDATE ON model_calls TO incidentpilot_runtime")

    # E14 and E15: one row per citable source record. The dedup identity includes the
    # owner (E19), so two owners never share a row.
    op.execute(
        f"""
        CREATE TABLE evidence (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            snapshot_id text NOT NULL,
            kind text NOT NULL CHECK (kind IN ({EVIDENCE_KINDS})),
            source_ref text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 512),
            adapter_version text NOT NULL CHECK (length(adapter_version) BETWEEN 1 AND 64),
            redaction_version text NOT NULL CHECK (length(redaction_version) BETWEEN 1 AND 64),
            window_start timestamptz,
            window_end timestamptz,
            content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
            content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$'),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (owner_id, id),
            CONSTRAINT evidence_identity UNIQUE (
                owner_id, snapshot_id, kind, source_ref, adapter_version, redaction_version,
                content_sha256
            ),
            CONSTRAINT evidence_snapshot_fkey FOREIGN KEY (owner_id, snapshot_id)
                REFERENCES snapshots (owner_id, id),
            CONSTRAINT evidence_window_by_kind CHECK (
                CASE WHEN kind IN ({WINDOWED_KINDS})
                    THEN window_start IS NOT NULL AND window_end > window_start
                    ELSE window_start IS NULL AND window_end IS NULL
                END
            )
        )
        """
    )
    for statement in owner_isolation("evidence"):
        op.execute(statement)
    # §7.3: evidence is immutable, so the runtime role can't change or delete it.
    op.execute("GRANT SELECT, INSERT ON evidence TO incidentpilot_runtime")

    # §7.3: an investigation sees only evidence granted to it here.
    op.execute(
        f"""
        CREATE TABLE investigation_evidence (
            owner_id uuid NOT NULL REFERENCES owners (id),
            investigation_id uuid NOT NULL,
            evidence_id uuid NOT NULL,
            first_attempt_id uuid NOT NULL,
            reason text NOT NULL CHECK (reason IN ({VISIBILITY_REASONS})),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (owner_id, investigation_id, evidence_id),
            CONSTRAINT investigation_evidence_investigation_fkey
                FOREIGN KEY (owner_id, investigation_id) REFERENCES investigations (owner_id, id),
            CONSTRAINT investigation_evidence_evidence_fkey
                FOREIGN KEY (owner_id, evidence_id) REFERENCES evidence (owner_id, id),
            CONSTRAINT investigation_evidence_attempt_fkey
                FOREIGN KEY (owner_id, first_attempt_id) REFERENCES attempts (owner_id, id)
        )
        """
    )
    for statement in owner_isolation("investigation_evidence"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON investigation_evidence TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
