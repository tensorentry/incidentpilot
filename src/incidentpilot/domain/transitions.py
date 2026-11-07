"""Legal state changes: spec D06 as corrected by E1, §6.1 for attempts, and §11.1 with
E9 and E18 for the reservation ledger.

These tables say which changes can ever happen. Whether one is allowed right now,
such as a retry with attempts remaining, depends on data and belongs to callers.
"""

from collections.abc import Mapping
from enum import Enum, StrEnum, auto
from types import MappingProxyType

from incidentpilot.domain.enums import (
    AttemptStatus,
    InvestigationStatus,
    ModelCallState,
    ToolCallState,
)


class IllegalTransitionError(ValueError):
    """The requested state change is never legal."""


class ReservationEffect(Enum):
    """What a ledger change does to the budget a call reserved. Defined in E18."""

    HOLD = auto()
    RELEASE = auto()
    SETTLE = auto()
    RETAIN = auto()


INVESTIGATION_TRANSITIONS: Mapping[InvestigationStatus, frozenset[InvestigationStatus]] = (
    MappingProxyType(
        {
            InvestigationStatus.QUEUED: frozenset(
                {
                    InvestigationStatus.RUNNING,
                    InvestigationStatus.CANCELLED,
                    InvestigationStatus.FAILED,
                }
            ),
            InvestigationStatus.RUNNING: frozenset(
                {
                    InvestigationStatus.VALIDATING,
                    InvestigationStatus.RETRY_WAIT,
                    InvestigationStatus.CANCELLED,
                    InvestigationStatus.FAILED,
                }
            ),
            InvestigationStatus.RETRY_WAIT: frozenset(
                {
                    InvestigationStatus.RUNNING,
                    InvestigationStatus.CANCELLED,
                    InvestigationStatus.FAILED,
                }
            ),
            InvestigationStatus.VALIDATING: frozenset(
                {
                    InvestigationStatus.COMPLETED,
                    InvestigationStatus.RETRY_WAIT,
                    InvestigationStatus.CANCELLED,
                    InvestigationStatus.FAILED,
                }
            ),
            InvestigationStatus.COMPLETED: frozenset(),
            InvestigationStatus.CANCELLED: frozenset(),
            InvestigationStatus.FAILED: frozenset(),
        }
    )
)

ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, frozenset[AttemptStatus]] = MappingProxyType(
    {
        AttemptStatus.RUNNING: frozenset(
            {
                AttemptStatus.SUCCEEDED,
                AttemptStatus.FAILED,
                AttemptStatus.LOST,
                AttemptStatus.CANCELLED,
            }
        ),
        AttemptStatus.SUCCEEDED: frozenset(),
        AttemptStatus.FAILED: frozenset(),
        AttemptStatus.LOST: frozenset(),
        AttemptStatus.CANCELLED: frozenset(),
    }
)

MODEL_CALL_TRANSITIONS: Mapping[ModelCallState, frozenset[ModelCallState]] = MappingProxyType(
    {
        ModelCallState.RESERVED: frozenset({ModelCallState.DISPATCHED, ModelCallState.FAILED}),
        ModelCallState.DISPATCHED: frozenset(
            {ModelCallState.SETTLED, ModelCallState.UNKNOWN, ModelCallState.FAILED}
        ),
        ModelCallState.SETTLED: frozenset(),
        ModelCallState.UNKNOWN: frozenset(),
        ModelCallState.FAILED: frozenset(),
    }
)

TOOL_CALL_TRANSITIONS: Mapping[ToolCallState, frozenset[ToolCallState]] = MappingProxyType(
    {
        ToolCallState.RESERVED: frozenset({ToolCallState.DISPATCHED, ToolCallState.FAILED}),
        ToolCallState.DISPATCHED: frozenset(
            {ToolCallState.COMPLETED, ToolCallState.UNKNOWN, ToolCallState.FAILED}
        ),
        ToolCallState.COMPLETED: frozenset(),
        ToolCallState.UNKNOWN: frozenset(),
        ToolCallState.FAILED: frozenset(),
    }
)


def require_investigation_transition(
    current: InvestigationStatus, target: InvestigationStatus
) -> None:
    _require(InvestigationStatus, INVESTIGATION_TRANSITIONS, current, target)


def require_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    _require(AttemptStatus, ATTEMPT_TRANSITIONS, current, target)


def require_model_call_transition(current: ModelCallState, target: ModelCallState) -> None:
    _require(ModelCallState, MODEL_CALL_TRANSITIONS, current, target)


def require_tool_call_transition(current: ToolCallState, target: ToolCallState) -> None:
    _require(ToolCallState, TOOL_CALL_TRANSITIONS, current, target)


def is_terminal_investigation(status: InvestigationStatus) -> bool:
    _require_type(InvestigationStatus, status)
    return not INVESTIGATION_TRANSITIONS[status]


def is_terminal_attempt(status: AttemptStatus) -> bool:
    _require_type(AttemptStatus, status)
    return not ATTEMPT_TRANSITIONS[status]


def is_terminal_model_call(state: ModelCallState) -> bool:
    _require_type(ModelCallState, state)
    return not MODEL_CALL_TRANSITIONS[state]


def is_terminal_tool_call(state: ToolCallState) -> bool:
    _require_type(ToolCallState, state)
    return not TOOL_CALL_TRANSITIONS[state]


def model_call_effect(current: ModelCallState, target: ModelCallState) -> ReservationEffect:
    require_model_call_transition(current, target)
    if target is ModelCallState.DISPATCHED:
        return ReservationEffect.HOLD
    if current is ModelCallState.RESERVED:
        return ReservationEffect.RELEASE
    if target is ModelCallState.SETTLED:
        return ReservationEffect.SETTLE
    return ReservationEffect.RETAIN


def tool_call_effect(current: ToolCallState, target: ToolCallState) -> ReservationEffect:
    require_tool_call_transition(current, target)
    if target is ToolCallState.DISPATCHED:
        return ReservationEffect.HOLD
    if current is ToolCallState.RESERVED:
        return ReservationEffect.RELEASE
    return ReservationEffect.RETAIN


def model_call_state_on_close(state: ModelCallState) -> ModelCallState:
    """The state E9 leaves a call in when its attempt closes."""
    _require_type(ModelCallState, state)
    if state is ModelCallState.RESERVED:
        return ModelCallState.FAILED
    if state is ModelCallState.DISPATCHED:
        return ModelCallState.UNKNOWN
    return state


def tool_call_state_on_close(state: ToolCallState) -> ToolCallState:
    """The state E9 leaves a call in when its attempt closes."""
    _require_type(ToolCallState, state)
    if state is ToolCallState.RESERVED:
        return ToolCallState.FAILED
    if state is ToolCallState.DISPATCHED:
        return ToolCallState.UNKNOWN
    return state


def _require[S: StrEnum](
    status_type: type[S], table: Mapping[S, frozenset[S]], current: S, target: S
) -> None:
    _require_type(status_type, current, target)
    if target not in table[current]:
        raise IllegalTransitionError(f"{current} cannot change to {target}")


def _require_type[S: StrEnum](status_type: type[S], *statuses: S) -> None:
    # StrEnum members equal plain strings, so the wrong type would pass a table lookup.
    # Database rows are untyped, so mypy can't catch this at that boundary.
    if any(type(status) is not status_type for status in statuses):
        raise TypeError(f"expected {status_type.__name__} values")
