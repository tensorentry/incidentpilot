import re
from collections.abc import Mapping
from enum import StrEnum
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from incidentpilot.domain.enums import AttemptStatus, InvestigationStatus
from incidentpilot.domain.transitions import (
    ATTEMPT_TRANSITIONS,
    INVESTIGATION_TRANSITIONS,
    IllegalTransitionError,
    is_terminal_attempt,
    is_terminal_investigation,
    require_attempt_transition,
    require_investigation_transition,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DIAGRAMS = REPO_ROOT / "docs" / "architecture" / "diagrams"
ORIGINAL_D06 = DIAGRAMS / "D06_investigation-state-machine.mmd"
ERRATA = REPO_ROOT / "docs" / "spec-errata.md"

Edges = set[tuple[str, str]]


def parse_edges(diagram: str) -> Edges:
    return set(re.findall(r"^\s*([A-Z_]+) --> ([A-Z_]+)", diagram, re.MULTILINE))


def corrected_d06() -> str:
    match = re.search(r"```mermaid\n(.*?)```", ERRATA.read_text(), re.DOTALL)
    assert match is not None
    return match.group(1)


CORRECTED_EDGES = parse_edges(corrected_d06())
ATTEMPT_EDGES: Edges = {
    ("RUNNING", "SUCCEEDED"),
    ("RUNNING", "FAILED"),
    ("RUNNING", "LOST"),
    ("RUNNING", "CANCELLED"),
}


def reachable[S: StrEnum](table: Mapping[S, frozenset[S]], start: S) -> set[S]:
    seen = {start}
    frontier = [start]
    while frontier:
        for target in table[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def test_corrected_diagram_adds_exactly_the_e1_edge() -> None:
    original = parse_edges(ORIGINAL_D06.read_text())

    assert CORRECTED_EDGES - original == {("VALIDATING", "RETRY_WAIT")}
    assert original - CORRECTED_EDGES == set()


@pytest.mark.parametrize(
    ("current", "target"), list(product(InvestigationStatus, InvestigationStatus))
)
def test_every_investigation_pair(
    current: InvestigationStatus, target: InvestigationStatus
) -> None:
    if (current.value, target.value) in CORRECTED_EDGES:
        require_investigation_transition(current, target)
    else:
        with pytest.raises(IllegalTransitionError):
            require_investigation_transition(current, target)


@pytest.mark.parametrize(("current", "target"), list(product(AttemptStatus, AttemptStatus)))
def test_every_attempt_pair(current: AttemptStatus, target: AttemptStatus) -> None:
    if (current.value, target.value) in ATTEMPT_EDGES:
        require_attempt_transition(current, target)
    else:
        with pytest.raises(IllegalTransitionError):
            require_attempt_transition(current, target)


def test_tables_cover_every_status() -> None:
    assert set(INVESTIGATION_TRANSITIONS) == set(InvestigationStatus)
    assert set(ATTEMPT_TRANSITIONS) == set(AttemptStatus)


def test_terminal_investigations_are_exactly_the_spec_terminals() -> None:
    terminal = {status for status in InvestigationStatus if is_terminal_investigation(status)}

    assert terminal == {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
    }


def test_terminal_attempts_are_everything_but_running() -> None:
    terminal = {status for status in AttemptStatus if is_terminal_attempt(status)}

    assert terminal == set(AttemptStatus) - {AttemptStatus.RUNNING}


def test_every_investigation_status_is_reachable_from_queued() -> None:
    assert reachable(INVESTIGATION_TRANSITIONS, InvestigationStatus.QUEUED) == set(
        InvestigationStatus
    )


@pytest.mark.parametrize("status", list(InvestigationStatus))
def test_no_investigation_status_is_a_dead_end(status: InvestigationStatus) -> None:
    assert any(is_terminal_investigation(s) for s in reachable(INVESTIGATION_TRANSITIONS, status))


@pytest.mark.parametrize(
    "status",
    [InvestigationStatus.QUEUED, InvestigationStatus.RUNNING, InvestigationStatus.RETRY_WAIT],
)
def test_cancel_is_possible_before_terminal(status: InvestigationStatus) -> None:
    require_investigation_transition(status, InvestigationStatus.CANCELLED)


def test_cancel_is_possible_during_validation() -> None:
    require_investigation_transition(InvestigationStatus.VALIDATING, InvestigationStatus.CANCELLED)


@pytest.mark.parametrize("status", [InvestigationStatus.RUNNING, InvestigationStatus.VALIDATING])
def test_a_lost_worker_can_recover_from_every_leased_status(status: InvestigationStatus) -> None:
    require_investigation_transition(status, InvestigationStatus.RETRY_WAIT)


def test_only_validation_can_complete() -> None:
    sources = {
        status
        for status, targets in INVESTIGATION_TRANSITIONS.items()
        if InvestigationStatus.COMPLETED in targets
    }

    assert sources == {InvestigationStatus.VALIDATING}


def test_error_names_both_statuses() -> None:
    with pytest.raises(IllegalTransitionError, match="COMPLETED cannot change to RUNNING"):
        require_investigation_transition(InvestigationStatus.COMPLETED, InvestigationStatus.RUNNING)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (AttemptStatus.RUNNING, InvestigationStatus.FAILED),
        (InvestigationStatus.RUNNING, AttemptStatus.FAILED),
        ("RUNNING", InvestigationStatus.FAILED),
        (InvestigationStatus.RUNNING, "FAILED"),
        ("RUNNING", "FAILED"),
    ],
)
def test_investigation_check_rejects_other_status_types(current: Any, target: Any) -> None:
    with pytest.raises(TypeError, match="InvestigationStatus"):
        require_investigation_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (InvestigationStatus.RUNNING, AttemptStatus.FAILED),
        (AttemptStatus.RUNNING, InvestigationStatus.FAILED),
        ("RUNNING", "FAILED"),
    ],
)
def test_attempt_check_rejects_other_status_types(current: Any, target: Any) -> None:
    with pytest.raises(TypeError, match="AttemptStatus"):
        require_attempt_transition(current, target)


@pytest.mark.parametrize("status", [AttemptStatus.FAILED, AttemptStatus.LOST, "FAILED"])
def test_terminal_investigation_check_rejects_other_status_types(status: Any) -> None:
    with pytest.raises(TypeError, match="InvestigationStatus"):
        is_terminal_investigation(status)


@pytest.mark.parametrize("status", [InvestigationStatus.RUNNING, "RUNNING"])
def test_terminal_attempt_check_rejects_other_status_types(status: Any) -> None:
    with pytest.raises(TypeError, match="AttemptStatus"):
        is_terminal_attempt(status)


def test_tables_cannot_be_modified() -> None:
    with pytest.raises(TypeError):
        INVESTIGATION_TRANSITIONS[InvestigationStatus.COMPLETED] = frozenset()  # type: ignore[index]
    with pytest.raises(TypeError):
        ATTEMPT_TRANSITIONS[AttemptStatus.LOST] = frozenset()  # type: ignore[index]
