import json
from typing import Any

import pytest
from pydantic import ValidationError

from incidentpilot.evaluation.oracle import Oracle


def cause(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "component": "checkout-api",
        "category": "deployment_regression",
        "role": "trigger",
    }
    return fields | changes


def oracle(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": "1",
        "snapshot_id": "snap-3f9a",
        "causes": [cause()],
        "acceptable_outcomes": ["diagnosed", "partially_diagnosed"],
        "discriminating_tests": ["Checkout-to-inventory span time rises after the deploy"],
    }
    return fields | changes


def parse(fields: dict[str, Any]) -> Oracle:
    return Oracle.model_validate_json(json.dumps(fields))


def test_an_answerable_oracle_is_accepted() -> None:
    assert parse(oracle()).answerable is True


def test_an_unanswerable_oracle_is_accepted() -> None:
    parsed = parse(
        oracle(causes=[], acceptable_outcomes=["insufficient_evidence", "partially_diagnosed"])
    )

    assert parsed.answerable is False


def test_an_unanswerable_case_cannot_accept_a_diagnosis() -> None:
    with pytest.raises(ValidationError, match="an unanswerable case can't accept a diagnosis"):
        parse(oracle(causes=[], acceptable_outcomes=["diagnosed"]))


def test_an_answerable_case_cannot_accept_an_abstention() -> None:
    with pytest.raises(ValidationError, match="an answerable case can't accept an abstention"):
        parse(oracle(acceptable_outcomes=["diagnosed", "insufficient_evidence"]))


def test_several_causes_may_be_expected() -> None:
    parse(oracle(causes=[cause(), cause(component="payments", role="contributing")]))


@pytest.mark.parametrize(
    "changes",
    [
        {"causes": [cause(), cause()]},
        {"acceptable_outcomes": []},
        {"acceptable_outcomes": ["diagnosed", "diagnosed"]},
        {"causes": [cause(role="root")]},
        {"causes": [cause(category="Deployment Regression")]},
        {"discriminating_tests": [""]},
        {"snapshot_id": "Snap-3F9A"},
        {"schema_version": "2"},
        {"notes": "anything"},
    ],
    ids=[
        "repeated-cause",
        "no-outcome",
        "repeated-outcome",
        "unknown-role",
        "bad-category",
        "empty-test",
        "bad-snapshot",
        "schema",
        "extra-field",
    ],
)
def test_malformed_oracles_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(oracle(**changes))
