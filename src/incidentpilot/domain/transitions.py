"""Legal state changes: spec D06 as corrected by E1, and §6.1 for attempts.

These tables say which changes can ever happen. Whether one is allowed right now,
such as a retry with attempts remaining, depends on data and belongs to callers.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from incidentpilot.domain.enums import AttemptStatus, InvestigationStatus


class IllegalTransitionError(ValueError):
    """The requested state change is never legal."""


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


def require_investigation_transition(
    current: InvestigationStatus, target: InvestigationStatus
) -> None:
    _require(InvestigationStatus, INVESTIGATION_TRANSITIONS, current, target)


def require_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    _require(AttemptStatus, ATTEMPT_TRANSITIONS, current, target)


def is_terminal_investigation(status: InvestigationStatus) -> bool:
    return not INVESTIGATION_TRANSITIONS[status]


def is_terminal_attempt(status: AttemptStatus) -> bool:
    return not ATTEMPT_TRANSITIONS[status]


def _require[S: StrEnum](
    status_type: type[S], table: Mapping[S, frozenset[S]], current: S, target: S
) -> None:
    # StrEnum members equal plain strings, so the wrong type would pass the lookup.
    # Database rows are untyped, so mypy can't catch this at that boundary.
    if type(current) is not status_type or type(target) is not status_type:
        raise TypeError(f"expected {status_type.__name__} values")
    if target not in table[current]:
        raise IllegalTransitionError(f"{current} cannot change to {target}")
