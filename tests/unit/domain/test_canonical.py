from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, StrEnum
from uuid import UUID

import pytest

from incidentpilot.domain.canonical import CanonicalizationError, canonical_json, sha256_digest
from incidentpilot.domain.enums import RunMode

INSTANT = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
GOLDEN_DIGEST = "368e3128550aa25548613da2eba0c503f83bd6f382b227771bbcf31971057479"


class Colour(StrEnum):
    RED = "red"


class Plain(Enum):
    OBJECT = object()
    TEXT = "red"


def test_golden_digest_computed_outside_python() -> None:
    value = {
        "m": RunMode.LIVE,
        "b": [1, "é", None, True],
        "a": {
            "t": datetime(2026, 11, 4, 13, 0, tzinfo=timezone(timedelta(hours=-5))),
            "id": UUID(int=1),
        },
    }

    assert sha256_digest(value) == GOLDEN_DIGEST


def test_output_is_compact_utf8_with_sorted_keys() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


def test_key_order_does_not_change_digest() -> None:
    assert sha256_digest({"a": 1, "b": {"c": 2, "d": 3}}) == sha256_digest(
        OrderedDict([("b", OrderedDict([("d", 3), ("c", 2)])), ("a", 1)])
    )


def test_list_order_is_significant() -> None:
    assert sha256_digest([1, 2]) != sha256_digest([2, 1])


def test_tuples_match_lists() -> None:
    assert canonical_json((1, 2)) == canonical_json([1, 2])


def test_same_instant_in_any_timezone_has_one_digest() -> None:
    elsewhere = INSTANT.astimezone(timezone(timedelta(hours=9)))

    assert sha256_digest(INSTANT) == sha256_digest(elsewhere)


def test_datetimes_use_utc_z_suffix() -> None:
    assert canonical_json(INSTANT) == b'"2026-11-04T18:00:00Z"'


def test_microseconds_are_preserved() -> None:
    assert canonical_json(INSTANT.replace(microsecond=5)) == b'"2026-11-04T18:00:00.000005Z"'


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="timezone"):
        canonical_json(datetime(2026, 11, 4, 18, 0))  # noqa: DTZ001


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="finite"):
        canonical_json({"x": [value]})


def test_negative_zero_equals_zero() -> None:
    assert sha256_digest(-0.0) == sha256_digest(0.0)


def test_enums_serialize_as_their_values() -> None:
    assert canonical_json({Colour.RED: Colour.RED}) == b'{"red":"red"}'


@pytest.mark.parametrize(
    "value",
    [{1, 2}, frozenset({1}), b"bytes", Decimal("1.0"), date(2026, 11, 4), object(), Plain.OBJECT],
)
def test_unsupported_types_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalizationError, match="unsupported"):
        canonical_json({"x": value})


def test_non_string_keys_are_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="keys must be strings"):
        canonical_json({1: "a"})


def test_keys_colliding_after_normalization_are_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="duplicate key"):
        canonical_json({Plain.TEXT: 1, "red": 2})


def test_lone_surrogates_are_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="UTF-8"):
        canonical_json("\ud800")


def test_errors_never_contain_the_value() -> None:
    canary = "canary-secret-value-7f3a"

    with pytest.raises(CanonicalizationError) as caught:
        canonical_json({"x": {canary}})

    assert canary not in str(caught.value)
