"""Alembic runs this to apply migrations. The connection string comes from migrate.upgrade."""

import psycopg
from alembic import context
from sqlalchemy import create_engine, pool

if context.is_offline_mode():
    raise RuntimeError("migrations run against a live database only")

conninfo: str = context.config.attributes["conninfo"]
engine = create_engine(
    "postgresql+psycopg://", creator=lambda: psycopg.connect(conninfo), poolclass=pool.NullPool
)
with engine.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
