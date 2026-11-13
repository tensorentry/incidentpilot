"""Apply database migrations as the migrator role: `python -m incidentpilot.persistence.migrate`.

Migrations only go forward. To undo one, restore a backup taken before it.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

from incidentpilot.persistence.database import required
from incidentpilot.settings import ConfigurationError, load_settings

MIGRATIONS = Path(__file__).parent / "migrations"
# Alembic reads option names in any case, so this one is kept out of reach of mutation.
SCRIPT_LOCATION = "script_location"


def alembic_config(conninfo: str) -> Config:
    config = Config(attributes={"conninfo": conninfo})
    config.set_main_option(SCRIPT_LOCATION, str(MIGRATIONS))
    return config


def upgrade(conninfo: str) -> None:
    command.upgrade(alembic_config(conninfo), "head")


def main() -> None:
    try:
        settings = load_settings()
        conninfo = required(settings.migration_database_url, "INCIDENTPILOT_MIGRATION_DATABASE_URL")
    except ConfigurationError as error:
        raise SystemExit(str(error)) from None
    upgrade(conninfo)


if __name__ == "__main__":
    main()
