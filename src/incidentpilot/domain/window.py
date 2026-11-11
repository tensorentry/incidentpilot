"""Time windows: UTC and half-open, `[start_at, end_at)`, as spec §8.3 requires."""

from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import AfterValidator, AwareDatetime, model_validator

from incidentpilot.domain.contract import Contract


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class TimeWindow(Contract):
    start_at: UtcDatetime
    end_at: UtcDatetime

    @model_validator(mode="after")
    def _ends_after_start(self) -> Self:
        if self.end_at <= self.start_at:
            raise ValueError("window must end after it starts")
        return self

    def contains(self, other: "TimeWindow") -> bool:
        return self.start_at <= other.start_at and other.end_at <= self.end_at
