"""Lifecycle vocabulary. Values are stored and published, so treat them as contracts."""

from enum import StrEnum


class RunMode(StrEnum):
    OFFLINE_DEMO = "offline_demo"
    LIVE = "live"
    REPLAY = "replay"


class InvestigationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AttemptStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


class ModelCallState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    SETTLED = "SETTLED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class ToolCallState(StrEnum):
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class DiagnosticOutcome(StrEnum):
    DIAGNOSED = "diagnosed"
    PARTIAL = "partially_diagnosed"
    INSUFFICIENT = "insufficient_evidence"


class ValidationStageStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    NOT_ASSESSED = "not_assessed"


class TerminationReason(StrEnum):
    INVESTIGATOR_FINISHED = "investigator_finished"
    MODEL_REFUSED = "model_refused"
    PROVIDER_FAILURE = "provider_failure"
    TURN_LIMIT_REACHED = "turn_limit_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_REACHED = "deadline_reached"
    FINALIZATION_RESERVE_REACHED = "finalization_reserve_reached"
    RETRIES_EXHAUSTED = "retries_exhausted"
    QUEUE_AGE_EXCEEDED = "queue_age_exceeded"
    INVALID_SETUP = "invalid_setup"
    NO_ADMISSIBLE_REPORT = "no_admissible_report"
    CANCELLED = "cancelled"


class FailureCategory(StrEnum):
    LEASE_LOST = "LEASE_LOST"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TELEMETRY_ERROR = "TELEMETRY_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    INVALID_INPUT = "INVALID_INPUT"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PROGRAMMING_ERROR = "PROGRAMMING_ERROR"

    @property
    def retryable(self) -> bool:
        return self not in _NEVER_RETRIED


_NEVER_RETRIED = frozenset(
    {
        FailureCategory.INVALID_INPUT,
        FailureCategory.FORBIDDEN_SCOPE,
        FailureCategory.CONFIGURATION_ERROR,
        FailureCategory.PROGRAMMING_ERROR,
    }
)


class EventType(StrEnum):
    INVESTIGATION_QUEUED = "INVESTIGATION_QUEUED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ATTEMPT_LOST = "ATTEMPT_LOST"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    MODEL_CALL_DISPATCHED = "MODEL_CALL_DISPATCHED"
    MODEL_CALL_SETTLED = "MODEL_CALL_SETTLED"
    MODEL_CALL_UNKNOWN = "MODEL_CALL_UNKNOWN"
    MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
    TOOL_CALL_COMPLETED = "TOOL_CALL_COMPLETED"
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    EVIDENCE_COMMITTED = "EVIDENCE_COMMITTED"
    VALIDATION_STARTED = "VALIDATION_STARTED"
    REPORT_REPAIR_REQUESTED = "REPORT_REPAIR_REQUESTED"
    REPORT_PUBLISHED = "REPORT_PUBLISHED"
    INVESTIGATION_CANCELLED = "INVESTIGATION_CANCELLED"
    INVESTIGATION_FAILED = "INVESTIGATION_FAILED"
