# ADR-015: Hand-written SQL over psycopg, forward-only migrations

**Status:** Accepted
**Date:** 2026-11-13
**Deciders:** Project owner
**Source:** T3.1

## Context

The spec names PostgreSQL and Alembic but no driver or query layer. What matters most in M3 is visible in SQL: row locks, `SKIP LOCKED` claims, epoch fences, row-level security and `SET LOCAL`. The agent runtime and telemetry interfaces are async (§9.1), so database calls shouldn't block the event loop.

## Decision

- **Driver:** psycopg 3, async connections in autocommit mode, with transactions opened explicitly. Tenant queries run inside `owner_transaction` (ADR-014).
- **Queries:** hand-written SQL in repositories, with no ORM.
- **Migrations:** Alembic runs hand-written SQL, as the migrator role. Migrations only go forward; undoing one means restoring a backup.
- **Tests:** each integration test gets its own database, copied from a migrated template, with two owners.

## Options considered

- **psycopg 3 with hand-written SQL (chosen):** Locking and isolation read exactly as they run, and async support is native. It costs more hand-written mapping between rows and contracts.
- **SQLAlchemy ORM:** Mapping comes free, but the queries this project is about would hide behind the session and unit of work, and lock order would be harder to see.
- **SQLAlchemy Core:** A middle path. It adds a second way to write SQL without making locks or policies clearer.
- **asyncpg:** Fast, but it has no synchronous API for Alembic, and its placeholder style differs from psycopg's.
- **Reversible migrations:** Down-migrations for tables that hold evidence and ledgers would destroy data, and nobody would test them, so they'd give false confidence.

## Consequences

- Easier: the SQL a reviewer reads is the SQL that runs, and every test sees a clean database without any cleanup code.
- Harder: rows map to contracts by hand, and mutation testing can't usefully mutate SQL text. Migrations are excluded from mutation, and integration tests check what they do.
- Revisit: copying the template takes about 10 ms per test here. If the integration suite outgrows that, switch to one database per test process.

## Reversal condition

Hand-written mapping becomes the main source of defects, or a query shape appears that an ORM would express more safely.

## Implemented by

T3.1
