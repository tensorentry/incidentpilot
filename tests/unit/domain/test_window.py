from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from incidentpilot.domain.window import TimeWindow

START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
END = START + timedelta(minutes=30)


def window(start: datetime = START, end: datetime = END) -> TimeWindow:
    return TimeWindow(start_at=start, end_at=end)


def test_offsets_are_normalized_to_utc() -> None:
    parsed = TimeWindow.model_validate_json(
        '{"start_at": "2026-11-04T19:00:00+01:00", "end_at": "2026-11-04T18:30:00Z"}'
    )

    assert parsed.start_at == START
    assert parsed.start_at.tzinfo is UTC
    assert parsed.end_at.tzinfo is UTC


def test_python_offsets_are_normalized_to_utc() -> None:
    plus_one = timezone(timedelta(hours=1))

    assert window(start=START.astimezone(plus_one)).start_at.tzinfo is UTC


def test_naive_times_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        TimeWindow.model_validate_json(
            '{"start_at": "2026-11-04T18:00:00", "end_at": "2026-11-04T18:30:00Z"}'
        )


@pytest.mark.parametrize("end", [START, START - timedelta(seconds=1)])
def test_a_window_must_end_after_it_starts(end: datetime) -> None:
    with pytest.raises(ValidationError, match="window must end after it starts"):
        window(end=end)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        TimeWindow.model_validate({"start_at": START, "end_at": END, "timezone": "UTC"})


def test_strings_are_not_coerced_in_python() -> None:
    with pytest.raises(ValidationError):
        TimeWindow.model_validate({"start_at": "2026-11-04T18:00:00Z", "end_at": END})


def test_windows_are_immutable() -> None:
    with pytest.raises(ValidationError, match="frozen"):
        window().start_at = END  # type: ignore[misc]


@pytest.mark.parametrize(
    ("start", "end", "contained"),
    [
        (START, END, True),
        (START + timedelta(minutes=5), END - timedelta(minutes=5), True),
        (START - timedelta(seconds=1), END, False),
        (START, END + timedelta(seconds=1), False),
        (END, END + timedelta(minutes=1), False),
        (START - timedelta(minutes=1), START, False),
    ],
    ids=["equal", "inside", "starts-before", "ends-after", "after", "before"],
)
def test_contains(start: datetime, end: datetime, contained: bool) -> None:
    assert window().contains(window(start, end)) is contained
