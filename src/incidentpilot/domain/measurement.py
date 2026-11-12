"""Typed quantities: erratum E2. Numbers live here, never in report prose.

The checks here are structural. Whether a value matches its inputs is the numeric
stage's job, and that needs the evidence rows.
"""

from collections.abc import Hashable, Sequence
from typing import Annotated, Self

from pydantic import AfterValidator, Field, model_validator

from incidentpilot.domain.contract import Contract
from incidentpilot.domain.enums import Aggregation, MeasurementUnit
from incidentpilot.domain.evidence import EvidenceSelector, MetricSeriesWindowSelector
from incidentpilot.domain.window import TimeWindow

MAX_ITEMS = 20


def distinct[T: Hashable](items: list[T]) -> list[T]:
    if len(set(items)) != len(items):
        raise ValueError("items must not repeat")
    return items


MetricName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Selectors = Annotated[
    list[EvidenceSelector],
    Field(min_length=1, max_length=MAX_ITEMS),
    AfterValidator(distinct),
]


def require_exact_windows(inputs: Sequence[EvidenceSelector], window: TimeWindow) -> None:
    # A wider selection includes data the value didn't use; a narrower one can't produce it.
    for selector in inputs:
        if isinstance(selector, MetricSeriesWindowSelector) and selector.window != window:
            raise ValueError("a series input must select exactly the measured window")


class _Aggregate(Contract):
    metric: MetricName
    aggregation: Aggregation
    value: Annotated[float, Field(allow_inf_nan=False)]
    unit: MeasurementUnit
    window: TimeWindow
    inputs: Selectors

    @model_validator(mode="after")
    def _unit_fits(self) -> Self:
        if self.aggregation is Aggregation.RATE and self.unit is not MeasurementUnit.PER_SECOND:
            raise ValueError("a rate is measured per second")
        if self.aggregation is Aggregation.RATIO and self.unit is not MeasurementUnit.RATIO:
            raise ValueError("a ratio is measured as a ratio")
        return self


class Quantity(_Aggregate):
    """One side of a ratio, aggregated directly from its inputs."""

    @model_validator(mode="after")
    def _direct(self) -> Self:
        if self.aggregation is Aggregation.RATIO:
            raise ValueError("a ratio term can't be a ratio itself")
        require_exact_windows(self.inputs, self.window)
        return self


class Measurement(_Aggregate):
    """A computed value. A ratio carries both terms so each can be recomputed."""

    numerator: Quantity | None
    denominator: Quantity | None

    @model_validator(mode="after")
    def _terms(self) -> Self:
        if self.aggregation is not Aggregation.RATIO:
            if self.numerator is not None or self.denominator is not None:
                raise ValueError("only a ratio has a numerator or a denominator")
            require_exact_windows(self.inputs, self.window)
            return self
        if self.numerator is None or self.denominator is None:
            raise ValueError("a ratio needs a numerator and a denominator")
        # Lets a ratio compare an incident window with a baseline window.
        if self.window != self.numerator.window:
            raise ValueError("a ratio covers its numerator's window")
        if set(self.inputs) != {*self.numerator.inputs, *self.denominator.inputs}:
            raise ValueError("a ratio's inputs must be its terms' inputs")
        return self
