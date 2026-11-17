"""Attempts, capacity slots and the claim's scheduler functions (spec §6.1, §6.5;
errata E8 and E19).

Revision ID: 0005
Revises: 0004
"""

from alembic import op

from incidentpilot.persistence.isolation import owner_isolation

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ATTEMPT_STATUSES = "'RUNNING', 'SUCCEEDED', 'FAILED', 'LOST', 'CANCELLED'"
# §2.4: at most two investigations run at once, across the whole installation.
CAPACITY = 2


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE attempts (
            owner_id uuid NOT NULL REFERENCES owners (id),
            id uuid NOT NULL,
            investigation_id uuid NOT NULL,
            attempt_no integer NOT NULL CHECK (attempt_no >= 1),
            status text NOT NULL DEFAULT 'RUNNING' CHECK (status IN ({ATTEMPT_STATUSES})),
            worker_id text NOT NULL CHECK (worker_id ~ '^[A-Za-z0-9._:-]{{1,128}}$'),
            lease_epoch bigint NOT NULL CHECK (lease_epoch >= 1),
            failure_category text,
            started_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            ended_at timestamptz,
            PRIMARY KEY (owner_id, id),
            CONSTRAINT attempts_number_unique UNIQUE (owner_id, investigation_id, attempt_no),
            CONSTRAINT attempts_investigation_fkey FOREIGN KEY (owner_id, investigation_id)
                REFERENCES investigations (owner_id, id),
            CONSTRAINT attempts_ended_when_final
                CHECK ((status = 'RUNNING') = (ended_at IS NULL))
        )
        """
    )
    for statement in owner_isolation("attempts"):
        op.execute(statement)
    op.execute("GRANT SELECT, INSERT, UPDATE ON attempts TO incidentpilot_runtime")

    # Installation-wide, so no owner column (E19). The runtime role has no grants here:
    # it binds and releases slots only through the scheduler functions below.
    op.execute(
        """
        CREATE TABLE capacity_slots (
            id smallint PRIMARY KEY CHECK (id >= 1),
            owner_id uuid,
            investigation_id uuid,
            attempt_id uuid UNIQUE,
            lease_epoch bigint,
            bound_at timestamptz,
            CONSTRAINT capacity_slots_bound_together CHECK (
                (attempt_id IS NULL) = (owner_id IS NULL)
                AND (attempt_id IS NULL) = (investigation_id IS NULL)
                AND (attempt_id IS NULL) = (lease_epoch IS NULL)
                AND (attempt_id IS NULL) = (bound_at IS NULL)
            )
        )
        """
    )
    op.execute(f"INSERT INTO capacity_slots (id) SELECT generate_series(1, {CAPACITY})")

    op.execute(
        "GRANT SELECT (owner_id, id, status, eligible_at, mode, created_at)"
        " ON investigations TO incidentpilot_scheduler"
    )
    # FOR UPDATE needs UPDATE on some column; this one is never written by the scheduler.
    op.execute("GRANT UPDATE (row_version) ON investigations TO incidentpilot_scheduler")
    op.execute(
        "GRANT SELECT (owner_id, id, investigation_id) ON attempts TO incidentpilot_scheduler"
    )
    op.execute("GRANT SELECT, UPDATE ON capacity_slots TO incidentpilot_scheduler")
    # Binding and release check the attempt belongs to the transaction's owner.
    op.execute("GRANT EXECUTE ON FUNCTION current_owner_id() TO incidentpilot_scheduler")
    op.execute("GRANT CREATE ON SCHEMA public TO incidentpilot_scheduler")
    op.execute("SET LOCAL ROLE incidentpilot_scheduler")

    # The claim's first step: lock the oldest eligible investigation in this mode,
    # whoever owns it, and return only its owner and ID (E19). The lock belongs to the
    # caller's transaction, which then works under that owner.
    op.execute(
        """
        CREATE FUNCTION claimable_investigation(claim_mode text)
        RETURNS TABLE (owner_id uuid, investigation_id uuid)
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        BEGIN ATOMIC
            SELECT owner_id, id FROM public.investigations
            WHERE status IN ('QUEUED', 'RETRY_WAIT')
              AND mode = claim_mode
              AND eligible_at <= pg_catalog.clock_timestamp()
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED;
        END
        """
    )
    # Second step, after the investigation row is locked (§6.1 lock order): take a free
    # slot for an attempt the current owner holds. Returns the slot, or NULL if none.
    op.execute(
        """
        CREATE FUNCTION bind_capacity_slot(attempt uuid, epoch bigint) RETURNS smallint
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            owner uuid := public.current_owner_id();
            investigation uuid;
            slot smallint;
        BEGIN
            SELECT a.investigation_id INTO investigation FROM public.attempts AS a
            WHERE a.id = attempt AND a.owner_id = owner;
            IF investigation IS NULL THEN
                RAISE EXCEPTION 'attempt % does not belong to the current owner', attempt
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            SELECT s.id INTO slot FROM public.capacity_slots AS s
            WHERE s.attempt_id IS NULL ORDER BY s.id LIMIT 1 FOR UPDATE SKIP LOCKED;
            IF slot IS NOT NULL THEN
                UPDATE public.capacity_slots
                SET owner_id = owner, investigation_id = investigation, attempt_id = attempt,
                    lease_epoch = epoch, bound_at = pg_catalog.clock_timestamp()
                WHERE id = slot;
            END IF;
            RETURN slot;
        END
        $$
        """
    )
    # Frees the slot an attempt of the current owner holds. True if one was bound.
    op.execute(
        """
        CREATE FUNCTION release_capacity_slot(attempt uuid) RETURNS boolean
        LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        BEGIN ATOMIC
            WITH freed AS (
                UPDATE public.capacity_slots
                SET owner_id = NULL, investigation_id = NULL, attempt_id = NULL,
                    lease_epoch = NULL, bound_at = NULL
                WHERE attempt_id = attempt AND owner_id = public.current_owner_id()
                RETURNING id
            )
            SELECT EXISTS (SELECT FROM freed);
        END
        """
    )
    for function in [
        "claimable_investigation(text)",
        "bind_capacity_slot(uuid, bigint)",
        "release_capacity_slot(uuid)",
    ]:
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO incidentpilot_runtime")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM incidentpilot_scheduler")


def downgrade() -> None:
    raise NotImplementedError("migrations only go forward; restore a backup instead")
