"""Report candidates and published reports (spec §6.5, §7.2; errata E1, E5, E6 and E19).

Revision ID: 0008
Revises: 0007
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

AUTHORS = "'model', 'application'"
OUTCOMES = "'diagnosed', 'partially_diagnosed', 'insufficient_evidence'"
TERMINATION_REASONS = (
    "'investigator_finished', 'model_refused', 'provider_failure', 'turn_limit_reached',"
    " 'budget_exhausted', 'deadline_reached', 'finalization_reserve_reached',"
    " 'retries_exhausted', 'queue_age_exceeded', 'invalid_setup', 'no_admissible_report',"
    " 'cancelled'"
)
TERMINAL_STATUSES = "'COMPLETED', 'CANCELLED', 'FAILED'"
SHA256 = "~ '^[0-9a-f]{64}$'"


def upgrade() -> None:
    # Every investigation that ended says why, and nothing still running claims a reason.
    op.execute(
        f"""
        ALTER TABLE investigations
            ADD CONSTRAINT investigations_termination_reason_known
                CHECK (termination_reason IN ({TERMINATION_REASONS})),
            ADD CONSTRAINT investigations_reason_when_ended
                CHECK ((status IN ({TERMINAL_STATUSES})) = (termination_reason IS NOT NULL))
        """
    )

    # E1: a candidate commits with RUNNING -> VALIDATING, so a recovery attempt can
    # validate it again without new model or tool calls. A repair (T4.9) adds another.
    op.execute(
        f"""
        CREATE TABLE report_candidates (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            investigation_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            author text NOT NULL CHECK (author IN ({AUTHORS})),
            termination_reason text NOT NULL CHECK (termination_reason IN ({TERMINATION_REASONS})),
            prompt_sha256 text NOT NULL CHECK (prompt_sha256 {SHA256}),
            content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
            content_sha256 text NOT NULL CHECK (content_sha256 {SHA256}),
            created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (owner_id, id),
            -- A cancelled investigation publishes nothing, so it hands in no candidate.
            CONSTRAINT report_candidates_not_cancelled CHECK (termination_reason <> 'cancelled'),
            -- Only a finished investigator's candidate is the model's (§9.4).
            CONSTRAINT report_candidates_author_matches_reason CHECK (
                (termination_reason = 'investigator_finished') = (author = 'model')
            ),
            CONSTRAINT report_candidates_investigation_fkey
                FOREIGN KEY (owner_id, investigation_id) REFERENCES investigations (owner_id, id),
            CONSTRAINT report_candidates_attempt_fkey
                FOREIGN KEY (owner_id, attempt_id) REFERENCES attempts (owner_id, id)
        )
        """
    )
    op.execute(
        "CREATE INDEX report_candidates_investigation_created"
        " ON report_candidates (owner_id, investigation_id, created_at)"
    )
    for statement in owner_isolation("report_candidates"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON report_candidates TO incidentpilot_runtime")

    # Invariant 1: one published report per investigation, and reports are immutable.
    op.execute(
        f"""
        CREATE TABLE diagnosis_reports (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            investigation_id uuid NOT NULL,
            attempt_id uuid NOT NULL,
            candidate_id uuid NOT NULL,
            diagnostic_outcome text NOT NULL CHECK (diagnostic_outcome IN ({OUTCOMES})),
            report jsonb NOT NULL CHECK (jsonb_typeof(report) = 'object'),
            report_sha256 text NOT NULL CHECK (report_sha256 {SHA256}),
            validator_version text NOT NULL CHECK (length(validator_version) BETWEEN 1 AND 64),
            published_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            PRIMARY KEY (owner_id, id),
            CONSTRAINT diagnosis_reports_one_per_investigation UNIQUE (owner_id, investigation_id),
            CONSTRAINT diagnosis_reports_investigation_fkey
                FOREIGN KEY (owner_id, investigation_id) REFERENCES investigations (owner_id, id),
            CONSTRAINT diagnosis_reports_attempt_fkey
                FOREIGN KEY (owner_id, attempt_id) REFERENCES attempts (owner_id, id),
            CONSTRAINT diagnosis_reports_candidate_fkey
                FOREIGN KEY (owner_id, candidate_id) REFERENCES report_candidates (owner_id, id)
        )
        """
    )
    for statement in owner_isolation("diagnosis_reports"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT ON diagnosis_reports TO incidentpilot_runtime")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
