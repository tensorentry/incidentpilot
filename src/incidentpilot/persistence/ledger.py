"""The model-call ledger (spec §11.1, D14; errata E9 and E18).

A model request is reserved against the investigation's budget, marked DISPATCHED in
its own committed transaction before it is sent, then settled, failed or left UNKNOWN.
Every change updates the investigation's counters in the same transaction, by the
effect E18 assigns it, so the counters always match the rows.

Workers call these inside `fenced_transaction`; `close_calls` runs inside
`close_attempt`. Both already hold the investigation row lock, so the counters can't
race.
"""

from dataclasses import dataclass
from uuid import uuid4

from psycopg import sql

from incidentpilot.domain.enums import EventType, FailureCategory, ModelCallState
from incidentpilot.domain.ids import AttemptId, InvestigationId, ModelCallId
from incidentpilot.domain.transitions import (
    ReservationEffect,
    model_call_effect,
    model_call_state_on_close,
)
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease

_EVENTS = {
    ModelCallState.DISPATCHED: EventType.MODEL_CALL_DISPATCHED,
    ModelCallState.SETTLED: EventType.MODEL_CALL_SETTLED,
    ModelCallState.UNKNOWN: EventType.MODEL_CALL_UNKNOWN,
    ModelCallState.FAILED: EventType.MODEL_CALL_FAILED,
}
_RESERVE = """
UPDATE investigations
SET model_requests_reserved = model_requests_reserved + 1,
    cost_reserved_micro_usd = cost_reserved_micro_usd + %(estimate)s
WHERE id = %(investigation)s
  AND model_requests_reserved < max_model_requests
  AND cost_reserved_micro_usd + cost_known_micro_usd + cost_unknown_micro_usd + %(estimate)s
      <= spend_cap_micro_usd
  AND deadline_at > pg_catalog.clock_timestamp()
"""
_INSERT = """
INSERT INTO model_calls (owner_id, id, investigation_id, attempt_id, reserved_cost_micro_usd)
VALUES (current_owner_id(), %s, %s, %s, %s)
"""
_CALL = (
    "SELECT state, investigation_id, reserved_cost_micro_usd FROM model_calls"
    " WHERE id = %s AND attempt_id = %s FOR UPDATE"
)
# Every caller holds the investigation row lock, so these states can't change before
# each call is moved.
_OPEN_CALLS = (
    "SELECT id, state FROM model_calls WHERE attempt_id = %s"
    " AND state IN ('RESERVED', 'DISPATCHED') ORDER BY created_at, id"
)
_DENIAL = """
SELECT deadline_at > pg_catalog.clock_timestamp() FROM investigations WHERE id = %s
"""
_MOVE = """
UPDATE model_calls
SET state = %(state)s,
    dispatched_at = CASE WHEN %(dispatching)s THEN pg_catalog.clock_timestamp()
                         ELSE dispatched_at END,
    ended_at = CASE WHEN %(final)s THEN pg_catalog.clock_timestamp() END,
    input_tokens = %(input_tokens)s,
    output_tokens = %(output_tokens)s,
    actual_cost_micro_usd = %(cost)s,
    provider_request_id = %(request_id)s,
    failure_category = %(failure)s
WHERE id = %(id)s
"""
# Filled with one "column = column + change" per counter a move changes.
_COUNT = sql.SQL("UPDATE investigations SET {} WHERE id = %s")


class BudgetDenied(Exception):
    """No model request may be reserved: a limit is spent or the deadline has passed."""

    def __init__(self, investigation_id: InvestigationId, *, past_deadline: bool) -> None:
        self.past_deadline = past_deadline
        limit = "its deadline has passed" if past_deadline else "its budget is spent"
        super().__init__(f"investigation {investigation_id} can't reserve a model call: {limit}")


@dataclass(frozen=True)
class Usage:
    """What the provider reported for one request."""

    input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    provider_request_id: str | None = None

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cost_micro_usd) < 0:
            raise ValueError("usage can't be negative")


def counter_changes(
    current: ModelCallState,
    target: ModelCallState,
    reserved_cost: int,
    usage: Usage | None = None,
) -> dict[str, int]:
    """How a change moves the investigation's counters, by its E18 effect."""
    effect = model_call_effect(current, target)
    if effect is ReservationEffect.HOLD:
        return {"model_requests_dispatched": 1}
    if effect is ReservationEffect.RELEASE:
        return {"model_requests_reserved": -1, "cost_reserved_micro_usd": -reserved_cost}
    if effect is ReservationEffect.SETTLE:
        if usage is None:
            raise ValueError("settling a call needs its usage")
        return {
            "model_requests_settled": 1,
            "cost_reserved_micro_usd": -reserved_cost,
            "cost_known_micro_usd": usage.cost_micro_usd,
            "input_tokens_actual": usage.input_tokens,
            "output_tokens_actual": usage.output_tokens,
        }
    changes = {"cost_reserved_micro_usd": -reserved_cost, "cost_unknown_micro_usd": reserved_cost}
    if target is ModelCallState.UNKNOWN:
        changes["model_requests_unknown"] = 1
    return changes


async def reserve_call(
    connection: Connection, lease: Lease, estimate_micro_usd: int
) -> ModelCallId:
    """Reserve one request and its estimated cost, or raise BudgetDenied."""
    if estimate_micro_usd < 0:
        raise ValueError("a cost estimate can't be negative")
    cursor = await connection.execute(
        _RESERVE, {"estimate": estimate_micro_usd, "investigation": lease.investigation_id}
    )
    if cursor.rowcount != 1:
        row = await (await connection.execute(_DENIAL, (lease.investigation_id,))).fetchone()
        raise BudgetDenied(lease.investigation_id, past_deadline=row != (True,))
    call_id = ModelCallId(uuid4())
    await connection.execute(
        _INSERT, (call_id, lease.investigation_id, lease.attempt_id, estimate_micro_usd)
    )
    return call_id


async def dispatch_call(connection: Connection, lease: Lease, call_id: ModelCallId) -> None:
    """Record that the request is about to be sent. Commit this before sending it."""
    await _move(connection, lease.attempt_id, call_id, ModelCallState.DISPATCHED)


async def settle_call(
    connection: Connection, lease: Lease, call_id: ModelCallId, usage: Usage
) -> None:
    await _move(connection, lease.attempt_id, call_id, ModelCallState.SETTLED, usage=usage)


async def fail_call(
    connection: Connection, lease: Lease, call_id: ModelCallId, failure: FailureCategory
) -> None:
    """End a call that failed. It keeps its reservation if it was sent (E18)."""
    await _move(connection, lease.attempt_id, call_id, ModelCallState.FAILED, failure=failure)


async def mark_call_unknown(connection: Connection, lease: Lease, call_id: ModelCallId) -> None:
    """End a sent call whose outcome can't be known. It keeps its reservation."""
    await _move(connection, lease.attempt_id, call_id, ModelCallState.UNKNOWN)


async def close_calls(
    connection: Connection, attempt_id: AttemptId, failure: FailureCategory | None
) -> None:
    """E9's ledger steps for a closing attempt: a call that was sent becomes UNKNOWN,
    and one that wasn't fails and releases its reservation."""
    cursor = await connection.execute(_OPEN_CALLS, (attempt_id,))
    for call_id, state in await cursor.fetchall():
        target = model_call_state_on_close(ModelCallState(state))
        await _move(connection, attempt_id, call_id, target, failure=failure)


async def _move(
    connection: Connection,
    attempt_id: AttemptId,
    call_id: ModelCallId,
    target: ModelCallState,
    *,
    usage: Usage | None = None,
    failure: FailureCategory | None = None,
) -> None:
    row = await (await connection.execute(_CALL, (call_id, attempt_id))).fetchone()
    if row is None:
        raise LookupError(f"model call {call_id} doesn't belong to attempt {attempt_id}")
    current, investigation_id, reserved_cost = ModelCallState(row[0]), row[1], row[2]
    changes = counter_changes(current, target, reserved_cost, usage)
    await connection.execute(
        _MOVE,
        {
            "id": call_id,
            "state": target,
            "dispatching": target is ModelCallState.DISPATCHED,
            "final": target is not ModelCallState.DISPATCHED,
            "input_tokens": usage and usage.input_tokens,
            "output_tokens": usage and usage.output_tokens,
            "cost": usage and usage.cost_micro_usd,
            "request_id": usage and usage.provider_request_id,
            "failure": failure if target is ModelCallState.FAILED else None,
        },
    )
    assignments = sql.SQL(", ").join(
        sql.SQL("{column} = {column} + {change}").format(
            column=sql.Identifier(column), change=sql.Literal(change)
        )
        for column, change in sorted(changes.items())
    )
    await connection.execute(
        _COUNT.format(assignments),
        (investigation_id,),
    )
    await append_event(
        connection,
        InvestigationId(investigation_id),
        _EVENTS[target],
        _payload(call_id, current, target, usage, failure),
    )


def _payload(
    call_id: ModelCallId,
    current: ModelCallState,
    target: ModelCallState,
    usage: Usage | None,
    failure: FailureCategory | None,
) -> dict[str, object]:
    payload: dict[str, object] = {"model_call_id": str(call_id)}
    if usage is not None:
        payload |= {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_micro_usd": usage.cost_micro_usd,
        }
    if target is ModelCallState.FAILED:
        payload["failure_category"] = failure and failure.value
        payload["sent"] = current is ModelCallState.DISPATCHED
    return payload
