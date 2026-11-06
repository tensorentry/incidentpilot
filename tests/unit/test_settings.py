from pathlib import Path

import pytest
from pydantic import ValidationError

from incidentpilot.domain.enums import RunMode
from incidentpilot.settings import ConfigurationError, Settings, load_settings

CANARY = "canary-secret-value-7f3a"
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MODE", "PROVIDER_API_KEY", "TRACE_EXPORT_ENABLED"):
        monkeypatch.delenv(f"INCIDENTPILOT_{name}", raising=False)


def test_defaults_are_offline_with_no_egress() -> None:
    settings = load_settings(env_file=None)

    assert settings.mode is RunMode.OFFLINE_DEMO
    assert settings.provider_api_key is None
    assert settings.trace_export_enabled is False
    assert settings.model_egress_enabled is False


def test_live_without_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "live")

    with pytest.raises(ConfigurationError, match="live mode requires"):
        load_settings(env_file=None)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "live")
    monkeypatch.setenv("INCIDENTPILOT_PROVIDER_API_KEY", blank)

    with pytest.raises(ConfigurationError, match="live mode requires"):
        load_settings(env_file=None)


def test_live_with_key_enables_model_egress_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "live")
    monkeypatch.setenv("INCIDENTPILOT_PROVIDER_API_KEY", CANARY)

    settings = load_settings(env_file=None)

    assert settings.model_egress_enabled is True
    assert settings.trace_export_enabled is False


def test_live_may_opt_in_to_trace_export(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "live")
    monkeypatch.setenv("INCIDENTPILOT_PROVIDER_API_KEY", CANARY)
    monkeypatch.setenv("INCIDENTPILOT_TRACE_EXPORT_ENABLED", "true")

    assert load_settings(env_file=None).trace_export_enabled is True


@pytest.mark.parametrize("mode", ["offline_demo", "replay"])
def test_trace_export_rejected_outside_live(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", mode)
    monkeypatch.setenv("INCIDENTPILOT_TRACE_EXPORT_ENABLED", "true")

    with pytest.raises(ConfigurationError, match="only permitted in live mode"):
        load_settings(env_file=None)


@pytest.mark.parametrize("mode", ["offline_demo", "replay"])
def test_key_does_not_enable_egress_outside_live(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", mode)
    monkeypatch.setenv("INCIDENTPILOT_PROVIDER_API_KEY", CANARY)

    assert load_settings(env_file=None).model_egress_enabled is False


def test_unknown_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "production")

    with pytest.raises(ConfigurationError, match="mode"):
        load_settings(env_file=None)


def test_key_is_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INCIDENTPILOT_PROVIDER_API_KEY", CANARY)

    settings = load_settings(env_file=None)

    assert CANARY not in repr(settings)
    assert CANARY not in str(settings)
    assert CANARY not in settings.model_dump_json()


@pytest.mark.parametrize("setting", ["MODE", "TRACE_EXPORT_ENABLED"])
def test_key_pasted_into_wrong_setting_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv(f"INCIDENTPILOT_{setting}", CANARY)

    with pytest.raises(ValidationError) as raw:
        Settings(_env_file=None)
    with pytest.raises(ConfigurationError) as wrapped:
        load_settings(env_file=None)

    assert CANARY not in str(raw.value)
    assert CANARY not in str(wrapped.value)


def test_configuration_error_drops_the_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INCIDENTPILOT_MODE", "live")

    with pytest.raises(ConfigurationError) as caught:
        load_settings(env_file=None)

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_settings_are_immutable() -> None:
    settings = load_settings(env_file=None)

    with pytest.raises(ValidationError):
        settings.mode = RunMode.LIVE  # type: ignore[misc]


def test_env_file_is_read(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INCIDENTPILOT_MODE=replay\n")

    assert load_settings(env_file=env_file).mode is RunMode.REPLAY


def test_default_env_file_is_read_from_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("INCIDENTPILOT_MODE=replay\n")
    monkeypatch.chdir(tmp_path)

    assert load_settings().mode is RunMode.REPLAY


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("INCIDENTPILOT_MODE=replay\n")
    monkeypatch.setenv("INCIDENTPILOT_MODE", "offline_demo")

    assert load_settings(env_file=env_file).mode is RunMode.OFFLINE_DEMO


@pytest.mark.parametrize(
    "line",
    [
        "INCIDENTPILOT_MOD=live",
        "INCIDENTPILOT_TRACE_EXPORT_ENABLD=true",
    ],
)
def test_misspelled_setting_fails_safe(tmp_path: Path, line: str) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{line}\n")

    settings = load_settings(env_file=env_file)

    assert settings.model_egress_enabled is False
    assert settings.trace_export_enabled is False


def test_misspelled_key_stops_live(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"INCIDENTPILOT_MODE=live\nINCIDENTPILOT_PROVIDER_API_KY={CANARY}\n")

    with pytest.raises(ConfigurationError, match="live mode requires") as caught:
        load_settings(env_file=env_file)

    assert CANARY not in str(caught.value)


def test_unrelated_entries_in_env_file_are_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_OTHER_TOOL=1\n")

    assert load_settings(env_file=env_file).mode is RunMode.OFFLINE_DEMO


def test_example_env_file_is_valid_and_complete() -> None:
    example = REPO_ROOT / ".env.example"
    documented = {
        line.split("=", 1)[0].removeprefix("INCIDENTPILOT_").lower()
        for line in example.read_text().splitlines()
        if line.startswith("INCIDENTPILOT_")
    }

    assert documented == set(Settings.model_fields)
    assert load_settings(env_file=example).mode is RunMode.OFFLINE_DEMO
