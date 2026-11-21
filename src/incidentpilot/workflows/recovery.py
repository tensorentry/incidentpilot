"""Ending attempts and recovering from lost workers (spec §6.1 to §6.4; errata E1, E9).

`close_attempt` is the one way an attempt ends. Publication, cancellation, the
sweeper and failure handling all call it, inside their own transaction and under the
investigation row lock. It covers E9's attempt, slot, model-call and event steps; tool
calls join it with the gateway (T4.3).

The sweeper ends what nobody else will:
- a lease that expired: the attempt is LOST, then the investigation retries, unless its
  recovery attempts or deadline are spent;
- queued work older than the queue age (§2.4);
- a retry wait past the investigation's deadline.
"""

from dataclasses import dataclass
from datetime import timedelta

from incidentpilot.domain.enums import (
    AttemptStatus,
    EventType,
    FailureCategory,
    InvestigationStatus,
    TerminationReason,
    ToolErrorCode,
)
from incidentpilot.domain.ids import AttemptId, InvestigationId, OwnerId
from incidentpilot.domain.transitions import (
    require_attempt_transition,
    require_investigation_transition,
)
from incidentpilot.persistence.database import Connection, adopt_owner, scheduler_transaction
from incidentpilot.persistence.events import Payload, append_event
from incidentpilot.persistence.ledger import close_calls
from incidentpilot.persistence.tool_ledger import close_tool_calls

# §2.4: work waits at most 10 minutes in the queue.
QUEUE_AGE = timedelta(minutes=10)
# A short pause before a recovery attempt may be claimed, so a flapping worker doesn't
# burn every attempt at once.
RETRY_BACKOFF = timedelta(seconds=2)
# How a tool call that was never sent ends when its attempt closes.
_UNSENT_TOOL_CALLS = {
    AttemptStatus.LOST: ToolErrorCode.STALE_LEASE,
    AttemptStatus.CANCELLED: ToolErrorCode.CANCELLED,
}
_ATTEMPT_EVENTS = {
    AttemptStatus.LOST: EventType.ATTEMPT_LOST,
    AttemptStatus.FAILED: EventType.ATTEMPT_FAILED,
}
_PICK = "SELECT owner_id, investigation_id FROM sweepable_investigation(%s)"
_ATTEMPT = "SELECT status, investigation_id FROM attempts WHERE id = %s FOR UPDATE"
_END_ATTEMPT = (
    "UPDATE attempts SET status = %s, failure_category = %s,"
    " ended_at = pg_catalog.clock_timestamp() WHERE id = %s"
)
# Enough to clear a backlog without holding up the worker for long.
SWEEP_LIMIT = 50
_RELEASE = "SELECT release_capacity_slot(%s)"
_STATE = """
SELECT status, current_attempt_id, attempts_started, max_recovery_attempts,
       deadline_at <= pg_catalog.clock_timestamp()
FROM investigations WHERE id = %s FOR UPDATE
"""


@dataclass(frozen=True)
class Swept:
    owner_id: OwnerId
    investigation_id: InvestigationId
    status: InvestigationStatus
    reason: TerminationReason | None


async def close_attempt(
    connection: Connection,
    attempt_id: AttemptId,
    status: AttemptStatus,
    failure: FailureCategory | None = None,
) -> None:
    """End one attempt: final status, free its slot, close its open model and tool calls,
    and record a loss or failure.

    A success or cancellation is recorded by the caller's own terminal event, such as
    REPORT_PUBLISHED or INVESTIGATION_CANCELLED.
    """
    row = await (await connection.execute(_ATTEMPT, (attempt_id,))).fetchone()
    if row is None:
        raise LookupError(f"attempt {attempt_id} doesn't exist")
    require_attempt_transition(AttemptStatus(row[0]), status)
    investigation_id = InvestigationId(row[1])
    await connection.execute(_END_ATTEMPT, (status, failure, attempt_id))
    await connection.execute(_RELEASE, (attempt_id,))
    await close_calls(connection, attempt_id, failure)
    await close_tool_calls(
        connection, attempt_id, _UNSENT_TOOL_CALLS.get(status, ToolErrorCode.INTERNAL_ERROR)
    )
    event = _ATTEMPT_EVENTS.get(status)
    if event is not None:
        await append_event(
            connection,
            investigation_id,
            event,
            {"attempt_id": str(attempt_id), "failure_category": failure and failure.value},
        )


async def end_investigation(
    connection: Connection,
    investigation_id: InvestigationId,
    current: InvestigationStatus,
    status: InvestigationStatus,
    reason: TerminationReason,
    event: EventType,
    details: Payload | None = None,
) -> None:
    """Move an investigation to a terminal status. Its attempt must be closed already.
    `details` joins the reason in the event's payload."""
    require_investigation_transition(current, status)
    await connection.execute(
        """
        UPDATE investigations
        SET status = %s, termination_reason = %s, current_attempt_id = NULL,
            lease_expires_at = NULL, row_version = row_version + 1
        WHERE id = %s
        """,
        (status, reason, investigation_id),
    )
    await append_event(
        connection, investigation_id, event, {"reason": reason.value, **(details or {})}
    )


async def sweep_once(
    connection: Connection, queue_age: timedelta = QUEUE_AGE, backoff: timedelta = RETRY_BACKOFF
) -> Swept | None:
    """End or recover one investigation nobody is running, or return None."""
    async with scheduler_transaction(connection):
        picked = await (await connection.execute(_PICK, (queue_age,))).fetchone()
        if picked is None:
            return None
        owner_id, investigation_id = OwnerId(picked[0]), InvestigationId(picked[1])
        await adopt_owner(connection, owner_id)
        row = await (await connection.execute(_STATE, (investigation_id,))).fetchone()
        assert row is not None  # noqa: S101 (sweepable_investigation just locked it)
        status, attempt_id, started, recoveries, past_deadline = row
        status = InvestigationStatus(status)
        if status is InvestigationStatus.QUEUED:
            reason = TerminationReason.QUEUE_AGE_EXCEEDED
        elif status is InvestigationStatus.RETRY_WAIT:
            reason = TerminationReason.DEADLINE_REACHED
        else:
            await close_attempt(
                connection,
                attempt_id,
                AttemptStatus.LOST,
                FailureCategory.LEASE_LOST,
            )
            if past_deadline:
                reason = TerminationReason.DEADLINE_REACHED
            elif started > recoveries:
                reason = TerminationReason.RETRIES_EXHAUSTED
            else:
                await _schedule_retry(connection, investigation_id, status, started + 1, backoff)
                return Swept(owner_id, investigation_id, InvestigationStatus.RETRY_WAIT, None)
        await end_investigation(
            connection,
            investigation_id,
            status,
            InvestigationStatus.FAILED,
            reason,
            EventType.INVESTIGATION_FAILED,
        )
        return Swept(owner_id, investigation_id, InvestigationStatus.FAILED, reason)


async def _schedule_retry(
    connection: Connection,
    investigation_id: InvestigationId,
    current: InvestigationStatus,
    next_attempt: int,
    backoff: timedelta,
) -> None:
    require_investigation_transition(current, InvestigationStatus.RETRY_WAIT)
    await connection.execute(
        """
        UPDATE investigations
        SET status = 'RETRY_WAIT', current_attempt_id = NULL, lease_expires_at = NULL,
            eligible_at = pg_catalog.clock_timestamp() + %s, row_version = row_version + 1
        WHERE id = %s
        """,
        (backoff, investigation_id),
    )
    await append_event(
        connection,
        investigation_id,
        EventType.RETRY_SCHEDULED,
        {"next_attempt_no": next_attempt, "backoff_seconds": backoff.total_seconds()},
    )


async def sweep(connection: Connection, limit: int = SWEEP_LIMIT) -> list[Swept]:
    """Sweep until nothing is left or `limit` investigations are handled.

    Pass this as a Worker's maintenance, so every worker also sweeps (§6.1).
    """
    handled: list[Swept] = []
    while len(handled) < limit and (swept := await sweep_once(connection)) is not None:
        handled.append(swept)
    return handled
