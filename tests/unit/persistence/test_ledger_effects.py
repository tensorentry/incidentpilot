"""How each model-call change moves the investigation's counters (E18)."""

import pytest

from incidentpilot.domain.enums import ModelCallState
from incidentpilot.domain.transitions import MODEL_CALL_TRANSITIONS, IllegalTransitionError
from incidentpilot.persistence.ledger import Usage, counter_changes

RESERVED = ModelCallState.RESERVED
DISPATCHED = ModelCallState.DISPATCHED
SETTLED = ModelCallState.SETTLED
UNKNOWN = ModelCallState.UNKNOWN
FAILED = ModelCallState.FAILED
USAGE = Usage(input_tokens=1_200, output_tokens=300, cost_micro_usd=7_500)
COST = 20_000

EXPECTED = {
    (RESERVED, DISPATCHED): {"model_requests_dispatched": 1},
    (RESERVED, FAILED): {"model_requests_reserved": -1, "cost_reserved_micro_usd": -COST},
    (DISPATCHED, SETTLED): {
        "model_requests_settled": 1,
        "cost_reserved_micro_usd": -COST,
        "cost_known_micro_usd": 7_500,
        "input_tokens_actual": 1_200,
        "output_tokens_actual": 300,
    },
    (DISPATCHED, UNKNOWN): {
        "model_requests_unknown": 1,
        "cost_reserved_micro_usd": -COST,
        "cost_unknown_micro_usd": COST,
    },
    (DISPATCHED, FAILED): {"cost_reserved_micro_usd": -COST, "cost_unknown_micro_usd": COST},
}


def test_every_legal_change_has_an_expected_effect() -> None:
    legal = {(a, b) for a, targets in MODEL_CALL_TRANSITIONS.items() for b in targets}
    assert set(EXPECTED) == legal


@pytest.mark.parametrize(("change", "expected"), list(EXPECTED.items()))
def test_each_change_moves_the_counters_by_its_effect(
    change: tuple[ModelCallState, ModelCallState], expected: dict[str, int]
) -> None:
    assert counter_changes(*change, COST, USAGE) == expected


def test_only_settling_reads_the_usage() -> None:
    for change in set(EXPECTED) - {(DISPATCHED, SETTLED)}:
        assert counter_changes(*change, COST) == counter_changes(*change, COST, USAGE)


def test_settling_needs_usage() -> None:
    with pytest.raises(ValueError, match=r"^settling a call needs its usage$"):
        counter_changes(DISPATCHED, SETTLED, COST)


@pytest.mark.parametrize(
    "change", [(RESERVED, SETTLED), (RESERVED, UNKNOWN), (SETTLED, FAILED), (UNKNOWN, SETTLED)]
)
def test_illegal_changes_are_refused(change: tuple[ModelCallState, ModelCallState]) -> None:
    with pytest.raises(IllegalTransitionError):
        counter_changes(*change, COST, USAGE)


@pytest.mark.parametrize(
    "fields", [(-1, 0, 0), (0, -1, 0), (0, 0, -1)], ids=["input", "output", "cost"]
)
def test_usage_cannot_be_negative(fields: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError, match=r"^usage can't be negative$"):
        Usage(*fields)


def test_zero_usage_is_allowed() -> None:
    assert Usage(0, 0, 0).provider_request_id is None
