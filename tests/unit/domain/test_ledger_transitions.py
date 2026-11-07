import re
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from incidentpilot.domain.enums import InvestigationStatus, ModelCallState, ToolCallState
from incidentpilot.domain.transitions import (
    MODEL_CALL_TRANSITIONS,
    TOOL_CALL_TRANSITIONS,
    IllegalTransitionError,
    ReservationEffect,
    is_terminal_model_call,
    is_terminal_tool_call,
    model_call_effect,
    model_call_state_on_close,
    require_model_call_transition,
    require_tool_call_transition,
    tool_call_effect,
    tool_call_state_on_close,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "docs" / "IncidentPilot_v3_Technical_Design.md"
ERRATA = REPO_ROOT / "docs" / "spec-errata.md"

Effects = dict[tuple[str, str], str]


def e18_effects(call: str) -> Effects:
    section = ERRATA.read_text().split("## E18.")[1]
    rows = re.findall(r"^\| (\w+) \| `([A-Z]+)` \| `([A-Z]+)` \| (\w+) \|$", section, re.MULTILINE)
    return {(source, target): effect for kind, source, target, effect in rows if kind == call}


def spec_chain_edges() -> set[tuple[str, str]]:
    match = re.search(r"`(\w+) → (\w+) → ([\w|]+)`", SPEC.read_text())
    assert match is not None
    first, second, finals = match.groups()
    return {(first, second)} | {(second, final) for final in finals.split("|")}


MODEL_EFFECTS = e18_effects("model")
TOOL_EFFECTS = e18_effects("tool")


def test_e18_adds_exactly_the_e9_edge_to_the_spec_chain() -> None:
    assert set(MODEL_EFFECTS) - spec_chain_edges() == {("RESERVED", "FAILED")}
    assert spec_chain_edges() - set(MODEL_EFFECTS) == set()


def test_tool_calls_differ_from_model_calls_only_in_their_success_state() -> None:
    renamed = {
        (source, "SETTLED" if target == "COMPLETED" else target) for source, target in TOOL_EFFECTS
    }

    assert renamed == set(MODEL_EFFECTS)


@pytest.mark.parametrize(("current", "target"), list(product(ModelCallState, ModelCallState)))
def test_every_model_call_pair(current: ModelCallState, target: ModelCallState) -> None:
    if (current.value, target.value) in MODEL_EFFECTS:
        require_model_call_transition(current, target)
    else:
        with pytest.raises(IllegalTransitionError):
            require_model_call_transition(current, target)


@pytest.mark.parametrize(("current", "target"), list(product(ToolCallState, ToolCallState)))
def test_every_tool_call_pair(current: ToolCallState, target: ToolCallState) -> None:
    if (current.value, target.value) in TOOL_EFFECTS:
        require_tool_call_transition(current, target)
    else:
        with pytest.raises(IllegalTransitionError):
            require_tool_call_transition(current, target)


def test_tables_cover_every_state() -> None:
    assert set(MODEL_CALL_TRANSITIONS) == set(ModelCallState)
    assert set(TOOL_CALL_TRANSITIONS) == set(ToolCallState)


@pytest.mark.parametrize(("current", "target"), list(product(ModelCallState, ModelCallState)))
def test_every_model_call_effect(current: ModelCallState, target: ModelCallState) -> None:
    effect = MODEL_EFFECTS.get((current.value, target.value))
    if effect is None:
        with pytest.raises(IllegalTransitionError):
            model_call_effect(current, target)
    else:
        assert model_call_effect(current, target) is ReservationEffect[effect.upper()]


@pytest.mark.parametrize(("current", "target"), list(product(ToolCallState, ToolCallState)))
def test_every_tool_call_effect(current: ToolCallState, target: ToolCallState) -> None:
    effect = TOOL_EFFECTS.get((current.value, target.value))
    if effect is None:
        with pytest.raises(IllegalTransitionError):
            tool_call_effect(current, target)
    else:
        assert tool_call_effect(current, target) is ReservationEffect[effect.upper()]


def test_a_failure_after_dispatch_keeps_the_reservation() -> None:
    assert (
        model_call_effect(ModelCallState.DISPATCHED, ModelCallState.FAILED)
        is ReservationEffect.RETAIN
    )
    assert (
        tool_call_effect(ToolCallState.DISPATCHED, ToolCallState.FAILED) is ReservationEffect.RETAIN
    )


def test_only_a_call_that_was_never_sent_releases() -> None:
    released = {
        (source, target)
        for source, targets in MODEL_CALL_TRANSITIONS.items()
        for target in targets
        if model_call_effect(source, target) is ReservationEffect.RELEASE
    }

    assert released == {(ModelCallState.RESERVED, ModelCallState.FAILED)}


def test_a_reserved_call_can_never_look_sent() -> None:
    assert MODEL_CALL_TRANSITIONS[ModelCallState.RESERVED] == {
        ModelCallState.DISPATCHED,
        ModelCallState.FAILED,
    }
    assert TOOL_CALL_TRANSITIONS[ToolCallState.RESERVED] == {
        ToolCallState.DISPATCHED,
        ToolCallState.FAILED,
    }


def test_a_late_response_cannot_settle_an_unknown_call() -> None:
    with pytest.raises(IllegalTransitionError, match="UNKNOWN cannot change to SETTLED"):
        require_model_call_transition(ModelCallState.UNKNOWN, ModelCallState.SETTLED)
    with pytest.raises(IllegalTransitionError, match="UNKNOWN cannot change to COMPLETED"):
        require_tool_call_transition(ToolCallState.UNKNOWN, ToolCallState.COMPLETED)


def test_terminal_model_calls_are_the_three_outcomes() -> None:
    terminal = {state for state in ModelCallState if is_terminal_model_call(state)}

    assert terminal == {ModelCallState.SETTLED, ModelCallState.UNKNOWN, ModelCallState.FAILED}


def test_terminal_tool_calls_are_the_three_outcomes() -> None:
    terminal = {state for state in ToolCallState if is_terminal_tool_call(state)}

    assert terminal == {ToolCallState.COMPLETED, ToolCallState.UNKNOWN, ToolCallState.FAILED}


@pytest.mark.parametrize(
    ("state", "closed"),
    [
        (ModelCallState.RESERVED, ModelCallState.FAILED),
        (ModelCallState.DISPATCHED, ModelCallState.UNKNOWN),
        (ModelCallState.SETTLED, ModelCallState.SETTLED),
        (ModelCallState.UNKNOWN, ModelCallState.UNKNOWN),
        (ModelCallState.FAILED, ModelCallState.FAILED),
    ],
)
def test_closing_an_attempt_resolves_model_calls(
    state: ModelCallState, closed: ModelCallState
) -> None:
    assert model_call_state_on_close(state) is closed


@pytest.mark.parametrize(
    ("state", "closed"),
    [
        (ToolCallState.RESERVED, ToolCallState.FAILED),
        (ToolCallState.DISPATCHED, ToolCallState.UNKNOWN),
        (ToolCallState.COMPLETED, ToolCallState.COMPLETED),
        (ToolCallState.UNKNOWN, ToolCallState.UNKNOWN),
        (ToolCallState.FAILED, ToolCallState.FAILED),
    ],
)
def test_closing_an_attempt_resolves_tool_calls(
    state: ToolCallState, closed: ToolCallState
) -> None:
    assert tool_call_state_on_close(state) is closed


@pytest.mark.parametrize("state", list(ModelCallState))
def test_closing_leaves_a_model_call_terminal_by_a_legal_change(state: ModelCallState) -> None:
    closed = model_call_state_on_close(state)

    assert is_terminal_model_call(closed)
    if closed is not state:
        require_model_call_transition(state, closed)


@pytest.mark.parametrize("state", list(ToolCallState))
def test_closing_leaves_a_tool_call_terminal_by_a_legal_change(state: ToolCallState) -> None:
    closed = tool_call_state_on_close(state)

    assert is_terminal_tool_call(closed)
    if closed is not state:
        require_tool_call_transition(state, closed)


def test_error_names_both_states() -> None:
    with pytest.raises(IllegalTransitionError, match="SETTLED cannot change to DISPATCHED"):
        require_model_call_transition(ModelCallState.SETTLED, ModelCallState.DISPATCHED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ToolCallState.RESERVED, ModelCallState.FAILED),
        (ModelCallState.RESERVED, ToolCallState.FAILED),
        ("RESERVED", ModelCallState.FAILED),
        (ModelCallState.RESERVED, "FAILED"),
        ("RESERVED", "FAILED"),
    ],
)
def test_model_call_checks_reject_other_state_types(current: Any, target: Any) -> None:
    with pytest.raises(TypeError, match="ModelCallState"):
        require_model_call_transition(current, target)
    with pytest.raises(TypeError, match="ModelCallState"):
        model_call_effect(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ModelCallState.RESERVED, ToolCallState.FAILED),
        (ToolCallState.RESERVED, ModelCallState.FAILED),
        ("RESERVED", "FAILED"),
    ],
)
def test_tool_call_checks_reject_other_state_types(current: Any, target: Any) -> None:
    with pytest.raises(TypeError, match="ToolCallState"):
        require_tool_call_transition(current, target)
    with pytest.raises(TypeError, match="ToolCallState"):
        tool_call_effect(current, target)


@pytest.mark.parametrize(
    "state", [ToolCallState.RESERVED, InvestigationStatus.FAILED, "RESERVED", "DISPATCHED"]
)
def test_single_state_model_call_checks_reject_other_types(state: Any) -> None:
    with pytest.raises(TypeError, match="ModelCallState"):
        is_terminal_model_call(state)
    with pytest.raises(TypeError, match="ModelCallState"):
        model_call_state_on_close(state)


@pytest.mark.parametrize(
    "state", [ModelCallState.RESERVED, InvestigationStatus.FAILED, "RESERVED", "DISPATCHED"]
)
def test_single_state_tool_call_checks_reject_other_types(state: Any) -> None:
    with pytest.raises(TypeError, match="ToolCallState"):
        is_terminal_tool_call(state)
    with pytest.raises(TypeError, match="ToolCallState"):
        tool_call_state_on_close(state)


def test_tables_cannot_be_modified() -> None:
    with pytest.raises(TypeError):
        MODEL_CALL_TRANSITIONS[ModelCallState.UNKNOWN] = frozenset()  # type: ignore[index]
    with pytest.raises(TypeError):
        TOOL_CALL_TRANSITIONS[ToolCallState.UNKNOWN] = frozenset()  # type: ignore[index]
