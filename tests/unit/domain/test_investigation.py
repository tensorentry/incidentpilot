"""The start request and the configuration it freezes (spec §2.4, §12.2; erratum E8)."""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from incidentpilot.domain.investigation import FrozenConfig, InvestigationLimits, StartInvestigation

START = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def exactly(message: str) -> str:
    return rf"Value error, {re.escape(message)} \["


def window(minutes: float) -> dict[str, str]:
    return {
        "start_at": START.isoformat(),
        "end_at": (START + timedelta(minutes=minutes)).isoformat(),
    }


def start(**changes: object) -> StartInvestigation:
    fields: dict[str, object] = {
        "mode": "live",
        "snapshot_id": "snap-example-01",
        "time_window": window(30),
        "model_profile": "single-investigator-default",
    }
    return StartInvestigation.model_validate_json(json.dumps(fields | changes))


def config(**changes: object) -> FrozenConfig:
    fields: dict[str, object] = {
        "mode": "offline_demo",
        "model_profile": "single-investigator-default",
        "snapshot_id": "snap-example-01",
        "window": window(30),
        "allowed_services": ["checkout-api", "inventory"],
        "limits": {},
    }
    return FrozenConfig.model_validate_json(json.dumps(fields | changes))


def test_the_default_limits_are_section_2_4s() -> None:
    assert InvestigationLimits().model_dump() == {
        "tool_calls": 15,
        "calls_per_tool": 6,
        "model_requests": 10,
        "recovery_attempts": 2,
        "deadline_seconds": 180,
        "finalization_reserve_seconds": 15,
        "spend_cap_micro_usd": 500_000,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"calls_per_tool": 16}, "a per-tool cap can't exceed the total tool cap"),
        (
            {"finalization_reserve_seconds": 180},
            "the finalization reserve must leave time before the deadline",
        ),
    ],
)
def test_limits_must_fit_together(changes: dict[str, int], message: str) -> None:
    with pytest.raises(ValidationError, match=exactly(message)):
        InvestigationLimits(**changes)


def test_limits_at_their_edges_are_accepted() -> None:
    limits = InvestigationLimits(
        calls_per_tool=15, finalization_reserve_seconds=179, recovery_attempts=0
    )

    assert (limits.calls_per_tool, limits.finalization_reserve_seconds) == (15, 179)


@pytest.mark.parametrize(
    "field", ["tool_calls", "model_requests", "deadline_seconds", "spend_cap_micro_usd"]
)
def test_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        InvestigationLimits(**{field: 0})


def test_a_two_hour_window_is_accepted() -> None:
    assert start(time_window=window(120)).time_window.end_at == START + timedelta(hours=2)


def test_a_window_over_two_hours_is_refused() -> None:
    with pytest.raises(
        ValidationError, match=exactly("an investigation window can't exceed 2 hours")
    ):
        start(time_window=window(120.5))


def test_an_unknown_model_profile_is_refused() -> None:
    with pytest.raises(ValidationError):
        start(model_profile="bigger-model")


def test_a_frozen_config_cannot_run_in_replay() -> None:
    with pytest.raises(ValidationError):
        config(mode="replay")


def test_allowed_services_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="items must not repeat"):
        config(allowed_services=["checkout-api", "checkout-api"])


def test_the_config_digest_is_pinned() -> None:
    # Workers compare this digest with the stored values, so the encoding can't drift.
    assert config().digest == ("24dc9e47a4cbfbc4bd981f2b709d1a6b719df1462f8cd4f4f109fc4c06ae93ee")


def test_any_change_to_the_config_changes_its_digest() -> None:
    assert config(allowed_services=["checkout-api"]).digest != config().digest
    assert config(limits={"tool_calls": 14}).digest != config().digest
