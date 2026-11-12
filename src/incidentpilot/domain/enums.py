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


class EvidenceKind(StrEnum):
    METRIC_SERIES_WINDOW = "metric_series_window"
    LOG_RECORD = "log_record"
    DEPLOYMENT_RECORD = "deployment_record"
    SPAN_SUMMARY = "span_summary"


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class LogSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"


class DiagnosticOutcome(StrEnum):
    DIAGNOSED = "diagnosed"
    PARTIAL = "partially_diagnosed"
    INSUFFICIENT = "insufficient_evidence"


class ClaimKind(StrEnum):
    OBSERVED = "observed"
    COMPUTED = "computed"


class Aggregation(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    RATE = "rate"
    P50 = "p50"
    P90 = "p90"
    P95 = "p95"
    P99 = "p99"
    RATIO = "ratio"


class MeasurementUnit(StrEnum):
    COUNT = "count"
    MILLISECONDS = "ms"
    SECONDS = "s"
    BYTES = "bytes"
    PER_SECOND = "per_second"
    RATIO = "ratio"


class CausalRole(StrEnum):
    TRIGGER = "trigger"
    PROXIMATE = "proximate"
    CONTRIBUTING = "contributing"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CauseCategory(StrEnum):
    """What kind of cause a hypothesis names. Grading compares it exactly (§14.3).

    The list is wider than the cases need, so naming a category is a real choice.
    """

    DEPLOYMENT_REGRESSION = "deployment_regression"
    DEPENDENCY_DEGRADATION = "dependency_degradation"
    RESOURCE_SATURATION = "resource_saturation"
    TRAFFIC_SURGE = "traffic_surge"
    CONFIGURATION_CHANGE = "configuration_change"


class AlternativeStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"


class RecommendationKind(StrEnum):
    DIAGNOSTIC = "diagnostic"
    SIMULATION_CANDIDATE = "simulation_candidate"


class ReportAuthor(StrEnum):
    MODEL = "model"
    APPLICATION = "application"


class StopReason(StrEnum):
    """Why the runner returned no candidate (E5). Values match `TerminationReason`."""

    MODEL_REFUSED = "model_refused"
    PROVIDER_FAILURE = "provider_failure"
    TURN_LIMIT_REACHED = "turn_limit_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"


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


class ToolName(StrEnum):
    """The P0 tools of spec §8.1 and E16. Nothing else is ever registered."""

    GET_SERVICE_METRICS = "get_service_metrics"
    SEARCH_LOGS = "search_logs"
    GET_DEPLOYMENTS = "get_deployments"
    GET_DEPENDENCY_HEALTH = "get_dependency_health"
    GET_TRACE_SUMMARY = "get_trace_summary"


class ToolStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    ERROR = "error"


class ToolErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    STALE_LEASE = "STALE_LEASE"
    CANCELLED = "CANCELLED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    CURSOR_INVALID = "CURSOR_INVALID"
    RESULT_TRUNCATED = "RESULT_TRUNCATED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    @property
    def retryable(self) -> bool:
        """Whether the same call may succeed later. Every other code needs a change first."""
        return self in _TRANSIENT_TOOL_ERRORS


_TRANSIENT_TOOL_ERRORS = frozenset(
    {ToolErrorCode.TIMEOUT, ToolErrorCode.SOURCE_UNAVAILABLE, ToolErrorCode.RATE_LIMITED}
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
