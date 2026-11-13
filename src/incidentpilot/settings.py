"""Process configuration and the run-mode contract (spec §2.3, §15.2)."""

import os
from pathlib import Path
from typing import Self

from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from incidentpilot.domain.enums import RunMode

ENV_PREFIX = "INCIDENTPILOT_"
DEFAULT_ENV_FILE = Path(".env")


class ConfigurationError(Exception):
    """Configuration is invalid. The message never contains configured values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        # .env files are often shared with other tools, so entries without our prefix
        # are ignored. load_settings rejects unknown names that have it.
        extra="ignore",
        frozen=True,
        # Validation errors otherwise echo raw input, such as a key pasted into the
        # wrong setting.
        hide_input_in_errors=True,
    )

    mode: RunMode = RunMode.OFFLINE_DEMO
    provider_api_key: SecretStr | None = None
    trace_export_enabled: bool = False
    # Connection strings hold passwords, so they're secrets too. Runtime and migration
    # use different roles (ADR-014).
    database_url: SecretStr | None = None
    migration_database_url: SecretStr | None = None

    @field_validator("provider_api_key", "database_url", "migration_database_url")
    @classmethod
    def _blank_secret_is_missing(cls, value: SecretStr | None) -> SecretStr | None:
        stripped = "" if value is None else value.get_secret_value().strip()
        return SecretStr(stripped) if stripped else None

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


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Load settings, failing fast. Every entry point calls this at startup.

    The default .env is optional, but a path passed explicitly must exist.
    """
    if env_file is not None and env_file != DEFAULT_ENV_FILE and not env_file.is_file():
        raise ConfigurationError("Invalid configuration: env file not found")
    try:
        unknown = _unknown_settings(env_file)
        settings = Settings(_env_file=env_file)
    except ValidationError as exc:
        problems = _describe(exc)
    except (OSError, UnicodeDecodeError):
        problems = "env file can't be read as UTF-8 text"
    else:
        if not unknown:
            return settings
        problems = f"unknown settings: {', '.join(unknown)}"
    # Raised out here so the original exception, which holds raw input, isn't kept as context.
    raise ConfigurationError(f"Invalid configuration: {problems}")


def _unknown_settings(env_file: Path | None) -> list[str]:
    """A misspelled opt-out would otherwise be dropped, leaving live mode or tracing on."""
    names = set(os.environ)
    if env_file is not None and env_file.is_file():
        names |= set(dotenv_values(env_file))
    known = {f"{ENV_PREFIX}{field}".upper() for field in Settings.model_fields}
    return sorted(
        name for name in names if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
    )


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{item['loc'][0] if item['loc'] else 'settings'}: {item['msg']}" for item in error.errors()
    )
