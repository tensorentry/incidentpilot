"""The tool-call ledger (spec §8.4, §11.1; errata E9, E13 and E18).

A tool call reserves against the investigation's global quota and its tool's own
quota, commits DISPATCHED before the adapter is called, then completes, fails or is
left UNKNOWN. Every change moves both quotas in the same transaction, by the effect
E18 assigns it, so the counters always match the rows.

Workers call these inside `fenced_transaction`; `close_tool_calls` runs inside
`close_attempt`. Both already hold the investigation row lock, so the counters can't
race.
"""

from collections.abc import Sequence
from uuid import uuid4

from incidentpilot.domain.enums import EventType, ToolCallState, ToolErrorCode, ToolName, ToolStatus
from incidentpilot.domain.ids import (
    AttemptId,
    EvidenceId,
    InvestigationId,
    InvocationId,
    ToolCallId,
)
from incidentpilot.domain.transitions import (
    ReservationEffect,
    tool_call_effect,
    tool_call_state_on_close,
)
from incidentpilot.persistence.database import Connection
from incidentpilot.persistence.events import append_event
from incidentpilot.persistence.fence import Lease

_EVENTS = {
    ToolCallState.DISPATCHED: EventType.TOOL_CALL_DISPATCHED,
    ToolCallState.COMPLETED: EventType.TOOL_CALL_COMPLETED,
    ToolCallState.UNKNOWN: EventType.TOOL_CALL_UNKNOWN,
    ToolCallState.FAILED: EventType.TOOL_CALL_FAILED,
}
_ANSWERED = frozenset({ToolStatus.OK, ToolStatus.EMPTY, ToolStatus.PARTIAL})
_RESERVE_GLOBAL = """
UPDATE investigations SET tool_calls_reserved = tool_calls_reserved + 1
WHERE id = %s AND tool_calls_reserved < max_tool_calls
"""
_RESERVE_TOOL = """
UPDATE investigation_tool_quota SET reserved = reserved + 1
WHERE investigation_id = %s AND tool_name = %s AND reserved < cap
"""
_INSERT = """
INSERT INTO tool_calls (
    owner_id, id, investigation_id, attempt_id, invocation_id, provider_call_id, tool_name,
    arguments_sha256
)
VALUES (current_owner_id(), %s, %s, %s, %s, %s, %s, %s)
"""
_CALL = (
    "SELECT state, investigation_id, tool_name FROM tool_calls"
    " WHERE id = %s AND attempt_id = %s FOR UPDATE"
)
# Every caller holds the investigation row lock, so these states can't change before
# each call is moved.
_OPEN_CALLS = (
    "SELECT id, state FROM tool_calls WHERE attempt_id = %s"
    " AND state IN ('RESERVED', 'DISPATCHED') ORDER BY created_at, id"
)
_MOVE = """
UPDATE tool_calls
SET state = %(state)s,
    dispatched_at = CASE WHEN %(dispatching)s THEN pg_catalog.clock_timestamp()
                         ELSE dispatched_at END,
    ended_at = CASE WHEN %(final)s THEN pg_catalog.clock_timestamp() END,
    result_status = %(result)s,
    error_code = %(error)s
WHERE id = %(id)s
"""
_HOLD = (
    "UPDATE investigations SET tool_calls_dispatched = tool_calls_dispatched + 1 WHERE id = %s",
    "UPDATE investigation_tool_quota SET dispatched = dispatched + 1"
    " WHERE investigation_id = %s AND tool_name = %s",
)
_RELEASE = (
    "UPDATE investigations SET tool_calls_reserved = tool_calls_reserved - 1 WHERE id = %s",
    "UPDATE investigation_tool_quota SET reserved = reserved - 1"
    " WHERE investigation_id = %s AND tool_name = %s",
)
_LINK = """
INSERT INTO tool_evidence (owner_id, tool_call_id, evidence_id)
VALUES (current_owner_id(), %s, %s)
ON CONFLICT DO NOTHING
"""


class QuotaDenied(Exception):
    """No tool call may be reserved: the investigation's tool quota is spent, or `tool`'s
    own quota if one is named."""

    def __init__(self, investigation_id: InvestigationId, tool: ToolName | None) -> None:
        spent = "its tool quota" if tool is None else f"its {tool} quota"
        super().__init__(f"investigation {investigation_id} has spent {spent}")


async def reserve_tool_call(
    connection: Connection,
    lease: Lease,
    tool: ToolName,
    invocation_id: InvocationId,
    provider_call_id: str,
    arguments_sha256: str,
) -> ToolCallId:
    """Reserve one call against both quotas, or raise QuotaDenied.

    A denial raised inside the fenced transaction rolls back whichever quota was
    already taken.
    """
    taken = await connection.execute(_RESERVE_GLOBAL, (lease.investigation_id,))
    if taken.rowcount != 1:
        raise QuotaDenied(lease.investigation_id, None)
    taken = await connection.execute(_RESERVE_TOOL, (lease.investigation_id, tool))
    if taken.rowcount != 1:
        raise QuotaDenied(lease.investigation_id, tool)
    call_id = ToolCallId(uuid4())
    await connection.execute(
        _INSERT,
        (
            call_id,
            lease.investigation_id,
            lease.attempt_id,
            invocation_id,
            provider_call_id,
            tool,
            arguments_sha256,
        ),
    )
    return call_id


async def dispatch_tool_call(connection: Connection, lease: Lease, call_id: ToolCallId) -> None:
    """Record that the adapter is about to be called. Commit this before calling it."""
    await _move(connection, lease.attempt_id, call_id, ToolCallState.DISPATCHED)


async def complete_tool_call(
    connection: Connection,
    lease: Lease,
    call_id: ToolCallId,
    status: ToolStatus,
    evidence: Sequence[EvidenceId],
) -> None:
    """End a call whose answer was persisted, linking the evidence it returned."""
    if status not in _ANSWERED:
        raise ValueError(f"a completed call answered ok, empty or partial, not {status}")
    for evidence_id in evidence:
        await connection.execute(_LINK, (call_id, evidence_id))
    await _move(connection, lease.attempt_id, call_id, ToolCallState.COMPLETED, result=status)


async def fail_tool_call(
    connection: Connection, lease: Lease, call_id: ToolCallId, code: ToolErrorCode
) -> None:
    """End a call that ended in an error. It keeps its quota if it was sent (E18)."""
    await _move(connection, lease.attempt_id, call_id, ToolCallState.FAILED, error=code)


async def close_tool_calls(
    connection: Connection, attempt_id: AttemptId, code: ToolErrorCode
) -> None:
    """E9's ledger steps for a closing attempt: a call that was sent becomes UNKNOWN,
    and one that wasn't fails with `code` and releases its quota."""
    cursor = await connection.execute(_OPEN_CALLS, (attempt_id,))
    for call_id, state in await cursor.fetchall():
        target = tool_call_state_on_close(ToolCallState(state))
        await _move(connection, attempt_id, call_id, target, error=code)


async def _move(
    connection: Connection,
    attempt_id: AttemptId,
    call_id: ToolCallId,
    target: ToolCallState,
    *,
    result: ToolStatus | None = None,
    error: ToolErrorCode | None = None,
) -> None:
    row = await (await connection.execute(_CALL, (call_id, attempt_id))).fetchone()
    if row is None:
        raise LookupError(f"tool call {call_id} doesn't belong to attempt {attempt_id}")
    current, investigation_id, tool = ToolCallState(row[0]), row[1], row[2]
    effect = tool_call_effect(current, target)
    failed = target is ToolCallState.FAILED
    await connection.execute(
        _MOVE,
        {
            "id": call_id,
            "state": target,
            "dispatching": target is ToolCallState.DISPATCHED,
            "final": target is not ToolCallState.DISPATCHED,
            "result": ToolStatus.ERROR if failed else result,
            "error": error if failed else None,
        },
    )
    counters = {ReservationEffect.HOLD: _HOLD, ReservationEffect.RELEASE: _RELEASE}.get(effect)
    if counters is not None:
        on_investigation, on_tool = counters
        await connection.execute(on_investigation, (investigation_id,))
        await connection.execute(on_tool, (investigation_id, tool))
    payload: dict[str, object] = {"tool_call_id": str(call_id), "tool_name": tool}
    if result is not None:
        payload["status"] = result.value
    if failed:
        payload["error_code"] = error
        payload["sent"] = current is ToolCallState.DISPATCHED
    await append_event(connection, InvestigationId(investigation_id), _EVENTS[target], payload)
