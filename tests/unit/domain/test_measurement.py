import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from incidentpilot.domain.enums import Aggregation, EvidenceKind, MeasurementUnit
from incidentpilot.domain.measurement import MAX_ITEMS, Measurement, Quantity, distinct
from incidentpilot.domain.window import TimeWindow

START = datetime(2026, 11, 4, 18, 15, tzinfo=UTC)
WINDOW = TimeWindow(start_at=START, end_at=START + timedelta(minutes=15))
BASELINE = TimeWindow(start_at=START - timedelta(hours=1), end_at=START - timedelta(minutes=45))
WIDER = TimeWindow(start_at=START - timedelta(minutes=1), end_at=WINDOW.end_at)


def exactly(message: str) -> str:
    """Match the whole validation message, so a changed message fails the test."""
    return rf"Value error, {re.escape(message)} \["


WINDOW_MISMATCH = exactly("a series input must select exactly the measured window")


def parse[M: BaseModel](model: type[M], fields: dict[str, Any]) -> M:
    """Validate as the model's JSON output would be: strict, with strings for enums."""
    return model.model_validate_json(json.dumps(to_jsonable_python(fields)))


def evidence_id(n: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{n:012d}")


def series(n: int = 1, window: TimeWindow = WINDOW) -> dict[str, Any]:
    return {"kind": "metric_series_window", "evidence_id": evidence_id(n), "window": window}


def log(n: int) -> dict[str, Any]:
    return {"kind": "log_record", "evidence_id": evidence_id(n)}


def quantity(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "metric": "request_count",
        "aggregation": Aggregation.SUM,
        "value": 240,
        "unit": MeasurementUnit.COUNT,
        "window": WINDOW,
        "inputs": [series()],
    }
    return fields | changes


def measurement(**changes: Any) -> dict[str, Any]:
    return quantity(numerator=None, denominator=None) | changes


def ratio(**changes: Any) -> dict[str, Any]:
    errors = quantity(metric="error_count", value=12, inputs=[series(2)])
    requests = quantity(inputs=[series(1)])
    fields = measurement(
        metric="error_ratio",
        aggregation=Aggregation.RATIO,
        value=0.05,
        unit=MeasurementUnit.RATIO,
        inputs=[series(2), series(1)],
        numerator=errors,
        denominator=requests,
    )
    return fields | changes


def test_a_direct_measurement_is_accepted() -> None:
    parsed = parse(Measurement, measurement())

    assert parsed.value == 240.0
    assert parsed.inputs[0].kind is EvidenceKind.METRIC_SERIES_WINDOW


def test_a_ratio_is_accepted() -> None:
    parsed = parse(Measurement, ratio())

    assert parsed.numerator is not None
    assert parsed.numerator.value == 12.0


def test_a_measurement_round_trips_through_python_and_json() -> None:
    parsed = parse(Measurement, ratio())

    assert Measurement.model_validate(parsed.model_dump()) == parsed
    assert Measurement.model_validate_json(parsed.model_dump_json()) == parsed


def test_a_ratio_can_compare_an_incident_with_a_baseline() -> None:
    incident = quantity(
        metric="latency", aggregation=Aggregation.P95, unit=MeasurementUnit.MILLISECONDS
    )
    baseline = incident | {"window": BASELINE, "inputs": [series(2, BASELINE)]}
    parse(
        Measurement,
        ratio(
            metric="latency_vs_baseline",
            inputs=[series(1), series(2, BASELINE)],
            numerator=incident,
            denominator=baseline,
        ),
    )


def test_inputs_that_are_not_series_need_no_window() -> None:
    parse(Measurement, measurement(metric="error_logs", inputs=[log(3), log(4)]))


BOTH_KINDS = pytest.mark.parametrize(
    ("model", "fields"),
    [(Measurement, measurement()), (Quantity, quantity())],
    ids=["measurement", "quantity"],
)


@BOTH_KINDS
def test_a_series_input_must_select_exactly_the_measured_window(
    model: type[BaseModel], fields: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match=WINDOW_MISMATCH):
        parse(model, fields | {"inputs": [series(1), series(2, WIDER)]})


@BOTH_KINDS
def test_a_rate_is_per_second(model: type[BaseModel], fields: dict[str, Any]) -> None:
    parse(model, fields | {"aggregation": Aggregation.RATE, "unit": "per_second"})
    with pytest.raises(ValidationError, match="a rate is measured per second"):
        parse(model, fields | {"aggregation": Aggregation.RATE})


def test_other_aggregations_may_use_per_second() -> None:
    parse(Measurement, measurement(aggregation=Aggregation.MEAN, unit="per_second"))


def test_a_ratio_is_measured_as_a_ratio() -> None:
    with pytest.raises(ValidationError, match="a ratio is measured as a ratio"):
        parse(Measurement, ratio(unit=MeasurementUnit.COUNT))


def test_other_aggregations_may_use_the_ratio_unit() -> None:
    parse(Measurement, measurement(aggregation=Aggregation.MEAN, unit="ratio"))


def test_a_ratio_term_cannot_be_a_ratio() -> None:
    with pytest.raises(ValidationError, match="a ratio term can't be a ratio itself"):
        parse(Quantity, quantity(aggregation=Aggregation.RATIO, unit="ratio"))


@pytest.mark.parametrize("missing", ["numerator", "denominator"])
def test_a_ratio_needs_both_terms(missing: str) -> None:
    with pytest.raises(ValidationError, match="a ratio needs a numerator and a denominator"):
        parse(Measurement, ratio(**{missing: None}))


@pytest.mark.parametrize("term", ["numerator", "denominator"])
def test_only_a_ratio_has_terms(term: str) -> None:
    with pytest.raises(ValidationError, match="only a ratio has a numerator or a denominator"):
        parse(Measurement, measurement(**{term: quantity()}))


def test_a_ratio_covers_its_numerators_window() -> None:
    with pytest.raises(ValidationError, match="covers its numerator's window"):
        parse(Measurement, ratio(window=BASELINE))


@pytest.mark.parametrize(
    "inputs",
    [[series(2)], [series(2), series(1), log(3)], [series(1), series(3)]],
    ids=["missing", "extra", "different"],
)
def test_a_ratios_inputs_are_its_terms_inputs(inputs: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError, match="must be its terms' inputs"):
        parse(Measurement, ratio(inputs=inputs))


@pytest.mark.parametrize(
    "inputs", [[], [series(1), series(1)], [log(n) for n in range(MAX_ITEMS + 1)]]
)
def test_inputs_are_bounded_and_distinct(inputs: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        parse(Measurement, measurement(inputs=inputs))


def test_inputs_reach_the_bound() -> None:
    parse(Measurement, measurement(inputs=[log(n) for n in range(MAX_ITEMS)]))


@pytest.mark.parametrize(
    "changes",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": "240"},
        {"unit": "percent"},
        {"aggregation": "median"},
        {"metric": "Request Count"},
        {"metric": "request_count\n"},
        {"metric": "r" * 65},
        {"inputs": [{"kind": "metric_series_window", "evidence_id": evidence_id(1)}]},
        {"inputs": [{"kind": "row", "evidence_id": evidence_id(1)}]},
        {"note": "computed by hand"},
    ],
    ids=[
        "nan",
        "infinity",
        "string-value",
        "unknown-unit",
        "unknown-aggregation",
        "bad-metric",
        "metric-newline",
        "long-metric",
        "series-without-window",
        "dropped-kind",
        "extra-field",
    ],
)
def test_malformed_measurements_are_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(Measurement, measurement(**changes))


def test_the_longest_metric_name_is_accepted() -> None:
    parse(Measurement, measurement(metric="r" * 64))


def test_terms_are_required_even_when_empty() -> None:
    fields = measurement()
    del fields["numerator"]
    with pytest.raises(ValidationError, match="numerator"):
        parse(Measurement, fields)


def test_distinct_returns_its_input() -> None:
    items = [1, 2, 3]

    assert distinct(items) is items
    with pytest.raises(ValueError, match=r"^items must not repeat$"):
        distinct([1, 1])
