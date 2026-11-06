import re
from enum import StrEnum
from pathlib import Path

import pytest

from incidentpilot.domain.enums import (
    AttemptStatus,
    DiagnosticOutcome,
    EventType,
    FailureCategory,
    InvestigationStatus,
    ModelCallState,
    RunMode,
    TerminationReason,
    ValidationStageStatus,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
D06 = REPO_ROOT / "docs" / "architecture" / "diagrams" / "D06_investigation-state-machine.mmd"


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


def test_events_named_by_spec_exist() -> None:
    assert {"ATTEMPT_STARTED", "REPORT_PUBLISHED"} <= values(EventType)


def test_spec_sample_termination_reason_exists() -> None:
    assert TerminationReason("investigator_finished") is TerminationReason.INVESTIGATOR_FINISHED


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.INVALID_INPUT,
        FailureCategory.FORBIDDEN_SCOPE,
        FailureCategory.CONFIGURATION_ERROR,
        FailureCategory.PROGRAMMING_ERROR,
    ],
)
def test_section_6_6_failures_are_not_retried(category: FailureCategory) -> None:
    assert category.retryable is False


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.LEASE_LOST,
        FailureCategory.PROVIDER_ERROR,
        FailureCategory.TELEMETRY_ERROR,
        FailureCategory.DATABASE_ERROR,
    ],
)
def test_transient_failures_are_retried(category: FailureCategory) -> None:
    assert category.retryable is True


def test_every_failure_category_has_a_tested_retry_decision() -> None:
    assert len(FailureCategory) == 8


@pytest.mark.parametrize(
    "enum",
    [InvestigationStatus, AttemptStatus, ModelCallState, EventType, FailureCategory],
)
def test_stored_values_are_upper_snake_case(enum: type[StrEnum]) -> None:
    assert all(re.fullmatch(r"[A-Z][A-Z_]*", value) for value in values(enum))


@pytest.mark.parametrize(
    "enum",
    [RunMode, DiagnosticOutcome, TerminationReason, ValidationStageStatus],
)
def test_reported_values_are_lower_snake_case(enum: type[StrEnum]) -> None:
    assert all(re.fullmatch(r"[a-z][a-z_]*", value) for value in values(enum))
