"""Process configuration and the run-mode contract (spec §2.3, §15.2)."""

from pathlib import Path
from typing import Self

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from incidentpilot.domain.enums import RunMode


class ConfigurationError(Exception):
    """Configuration is invalid. The message never contains configured values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INCIDENTPILOT_",
        # .env files are often shared with other tools. Every misspelled setting
        # here fails safe: no egress, no trace export, or live refuses to start.
        extra="ignore",
        frozen=True,
        # Validation errors otherwise echo raw input, such as a key pasted into the
        # wrong setting.
        hide_input_in_errors=True,
    )

    mode: RunMode = RunMode.OFFLINE_DEMO
    provider_api_key: SecretStr | None = None
    trace_export_enabled: bool = False

    @field_validator("provider_api_key")
    @classmethod
    def _blank_key_is_missing(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @model_validator(mode="after")
    def _enforce_mode_contract(self) -> Self:
        if self.mode is RunMode.LIVE and self.provider_api_key is None:
            raise ValueError("live mode requires INCIDENTPILOT_PROVIDER_API_KEY")
        if self.trace_export_enabled and self.mode is not RunMode.LIVE:
            raise ValueError("trace export is only permitted in live mode")
        return self

    @property
    def model_egress_enabled(self) -> bool:
        return self.mode is RunMode.LIVE


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    """Load settings, failing fast. Every entry point calls this at startup."""
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'settings'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        )
        raise ConfigurationError(f"Invalid configuration: {problems}") from None
