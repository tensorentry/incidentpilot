import re
from enum import StrEnum
from pathlib import Path

import pytest

from incidentpilot.domain.enums import (
    Aggregation,
    AlternativeStatus,
    AttemptStatus,
    CausalRole,
    ClaimKind,
    Confidence,
    DiagnosticOutcome,
    EventType,
    FailureCategory,
    InvestigationStatus,
    LogSeverity,
    MeasurementUnit,
    MetricKind,
    ModelCallState,
    RecommendationKind,
    ReportAuthor,
    RunMode,
    StopReason,
    TerminationReason,
    ToolCallState,
    ToolErrorCode,
    ToolName,
    ToolStatus,
    ValidationStageStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
D06 = REPO_ROOT / "docs" / "architecture" / "diagrams" / "D06_investigation-state-machine.mmd"
ERRATA = REPO_ROOT / "docs" / "spec-errata.md"


NEVER_RETRIED = [
    FailureCategory.INVALID_INPUT,
    FailureCategory.FORBIDDEN_SCOPE,
    FailureCategory.CONFIGURATION_ERROR,
    FailureCategory.PROGRAMMING_ERROR,
]
RETRIED = [
    FailureCategory.LEASE_LOST,
    FailureCategory.PROVIDER_ERROR,
    FailureCategory.TELEMETRY_ERROR,
    FailureCategory.DATABASE_ERROR,
]


def values(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


def test_investigation_statuses_match_d06() -> None:
    states = set(re.findall(r"\b[A-Z][A-Z_]+\b", D06.read_text()))

    assert values(InvestigationStatus) == states


def test_attempt_statuses_match_spec() -> None:
    match = re.search(r"Attempt statuses: ([^.]+)\.", SPEC.read_text())
    assert match is not None

    assert values(AttemptStatus) == set(re.findall(r"`([A-Z_]+)`", match.group(1)))


def test_model_call_states_match_spec() -> None:
    match = re.search(r"state `([A-Z|]+)`", SPEC.read_text())
    assert match is not None

    assert values(ModelCallState) == set(match.group(1).split("|"))


def test_tool_call_states_match_errata_e18() -> None:
    match = re.search(r"`RESERVED → DISPATCHED → ([A-Z|]+)`", ERRATA.read_text())
    assert match is not None

    assert values(ToolCallState) == {"RESERVED", "DISPATCHED", *match.group(1).split("|")}


def test_run_modes_match_spec() -> None:
    assert values(RunMode) == {"offline_demo", "live", "replay"}


def test_diagnostic_outcomes_match_spec() -> None:
    assert values(DiagnosticOutcome) == {
        "diagnosed",
        "partially_diagnosed",
        "insufficient_evidence",
    }


def test_validation_stage_statuses_cover_errata_e6() -> None:
    assert {"not_run", "not_assessed"} <= values(ValidationStageStatus)


@pytest.mark.parametrize(
    ("field", "enum"),
    [("claim_kind", ClaimKind), ("role", CausalRole), ("confidence", Confidence)],
)
def test_hypothesis_vocabulary_matches_spec_section_7_4(field: str, enum: type[StrEnum]) -> None:
    match = re.search(rf"{field}: str  # ([a-z |]+)", SPEC.read_text())
    assert match is not None

    assert values(enum) == set(match.group(1).split(" | "))


def test_alternative_statuses_match_spec_section_10_1() -> None:
    match = re.search(r"\| `alternatives\[\]` \| ([a-z /]+) \|", SPEC.read_text())
    assert match is not None

    assert values(AlternativeStatus) == set(match.group(1).split(" / "))


def test_recommendation_kinds_cover_spec_section_10_1() -> None:
    assert '"kind": "diagnostic"' in SPEC.read_text()
    assert values(RecommendationKind) == {"diagnostic", "simulation_candidate"}


def test_stop_reasons_are_the_four_errata_e5_names() -> None:
    assert values(StopReason) == {
        "model_refused",
        "provider_failure",
        "turn_limit_reached",
        "budget_exhausted",
    }


def test_measurement_values_are_pinned() -> None:
    assert values(Aggregation) == {
        "sum",
        "mean",
        "min",
        "max",
        "rate",
        "p50",
        "p90",
        "p95",
        "p99",
        "ratio",
    }
    assert values(MeasurementUnit) == {"count", "ms", "s", "bytes", "per_second", "ratio"}


def test_fixture_values_are_pinned() -> None:
    assert values(MetricKind) == {"counter", "gauge", "histogram"}
    assert values(LogSeverity) == {"debug", "info", "warn", "error", "fatal"}


def test_report_authors_are_pinned() -> None:
    assert values(ReportAuthor) == {"model", "application"}


def test_events_named_by_spec_exist() -> None:
    assert {"ATTEMPT_STARTED", "REPORT_PUBLISHED"} <= values(EventType)


def test_spec_sample_termination_reason_exists() -> None:
    assert TerminationReason("investigator_finished") is TerminationReason.INVESTIGATOR_FINISHED


@pytest.mark.parametrize("category", NEVER_RETRIED)
def test_section_6_6_failures_are_not_retried(category: FailureCategory) -> None:
    assert category.retryable is False


@pytest.mark.parametrize("category", RETRIED)
def test_transient_failures_are_retried(category: FailureCategory) -> None:
    assert category.retryable is True


def test_every_failure_category_has_a_tested_retry_decision() -> None:
    assert set(NEVER_RETRIED) | set(RETRIED) == set(FailureCategory)
    assert not set(NEVER_RETRIED) & set(RETRIED)


def test_event_values_are_pinned() -> None:
    assert values(EventType) == {
        "INVESTIGATION_QUEUED",
        "ATTEMPT_STARTED",
        "ATTEMPT_LOST",
        "ATTEMPT_FAILED",
        "RETRY_SCHEDULED",
        "MODEL_CALL_DISPATCHED",
        "MODEL_CALL_SETTLED",
        "MODEL_CALL_UNKNOWN",
        "MODEL_CALL_FAILED",
        "TOOL_CALL_COMPLETED",
        "TOOL_CALL_FAILED",
        "EVIDENCE_COMMITTED",
        "VALIDATION_STARTED",
        "REPORT_REPAIR_REQUESTED",
        "REPORT_PUBLISHED",
        "INVESTIGATION_CANCELLED",
        "INVESTIGATION_FAILED",
    }


def test_termination_values_are_pinned() -> None:
    assert values(TerminationReason) == {
        "investigator_finished",
        "model_refused",
        "provider_failure",
        "turn_limit_reached",
        "budget_exhausted",
        "deadline_reached",
        "finalization_reserve_reached",
        "retries_exhausted",
        "queue_age_exceeded",
        "invalid_setup",
        "no_admissible_report",
        "cancelled",
    }


def test_failure_values_are_pinned() -> None:
    assert values(FailureCategory) == {
        "LEASE_LOST",
        "PROVIDER_ERROR",
        "TELEMETRY_ERROR",
        "DATABASE_ERROR",
        "INVALID_INPUT",
        "FORBIDDEN_SCOPE",
        "CONFIGURATION_ERROR",
        "PROGRAMMING_ERROR",
    }


def test_validation_stage_values_are_pinned() -> None:
    assert values(ValidationStageStatus) == {"passed", "failed", "not_run", "not_assessed"}


@pytest.mark.parametrize(
    "enum",
    [InvestigationStatus, AttemptStatus, ModelCallState, EventType, FailureCategory, ToolErrorCode],
)
def test_upper_case_members_store_their_own_name(enum: type[StrEnum]) -> None:
    assert all(member.name == member.value for member in enum)


@pytest.mark.parametrize(
    "enum",
    [InvestigationStatus, AttemptStatus, ModelCallState, EventType, FailureCategory, ToolErrorCode],
)
def test_stored_values_are_upper_snake_case(enum: type[StrEnum]) -> None:
    assert all(re.fullmatch(r"[A-Z][A-Z_]*", value) for value in values(enum))


@pytest.mark.parametrize(
    "enum",
    [
        RunMode,
        DiagnosticOutcome,
        TerminationReason,
        ValidationStageStatus,
        ClaimKind,
        Aggregation,
        MeasurementUnit,
        CausalRole,
        Confidence,
        AlternativeStatus,
        RecommendationKind,
        ReportAuthor,
        StopReason,
        ToolName,
        ToolStatus,
        MetricKind,
        LogSeverity,
    ],
)
def test_reported_values_are_lower_snake_case(enum: type[StrEnum]) -> None:
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", value) for value in values(enum))
