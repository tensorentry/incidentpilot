"""The statements that put a tenant table under owner isolation (ADR-014)."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from incidentpilot.persistence import migrate
from incidentpilot.persistence.database import required
from incidentpilot.persistence.isolation import owner_isolation
from incidentpilot.settings import ConfigurationError


def test_isolation_enables_and_forces_one_policy_for_reads_and_writes() -> None:
    assert owner_isolation("notes") == [
        "ALTER TABLE notes ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE notes FORCE ROW LEVEL SECURITY",
        "CREATE POLICY owner_isolation ON notes"
        " USING (owner_id = current_owner_id()) WITH CHECK (owner_id = current_owner_id())",
    ]


def test_the_longest_plain_name_is_accepted() -> None:
    assert owner_isolation("a" * 63)[0] == f"ALTER TABLE {'a' * 63} ENABLE ROW LEVEL SECURITY"


@pytest.mark.parametrize(
    "table", ["", "Notes", "1notes", "notes; DROP TABLE owners", "public.notes", "a" * 64]
)
def test_only_plain_table_names_are_accepted(table: str) -> None:
    with pytest.raises(ValueError, match=rf"^{re.escape(f'not a plain table name: {table!r}')}$"):
        owner_isolation(table)


def test_a_missing_connection_string_names_its_setting() -> None:
    message = "Invalid configuration: INCIDENTPILOT_DATABASE_URL is required"
    with pytest.raises(ConfigurationError, match=rf"^{re.escape(message)}$"):
        required(None, "INCIDENTPILOT_DATABASE_URL")


def test_a_configured_connection_string_is_returned() -> None:
    assert required(SecretStr("dbname=x"), "INCIDENTPILOT_DATABASE_URL") == "dbname=x"


def test_migrating_without_a_connection_string_exits_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INCIDENTPILOT_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    message = "Invalid configuration: INCIDENTPILOT_MIGRATION_DATABASE_URL is required"
    with pytest.raises(SystemExit, match=rf"^{re.escape(message)}$"):
        migrate.main()
