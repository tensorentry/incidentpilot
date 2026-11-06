# ADR-003: PostgreSQL job queue and fenced leases

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §6, §18.1

## Context

Work has to survive worker crashes, and a stale worker must never commit. Queue state and domain state need to change together.

## Decision

Use PostgreSQL as the queue, with `FOR UPDATE SKIP LOCKED` claims, leases, and an epoch fence checked on every worker write.

## Options considered

- **PostgreSQL queue (chosen):** Queue and domain state commit in one transaction, with no extra infrastructure.
- **In-memory scheduler:** Simple, but fails crash safety.
- **Kafka or Celery:** Mature, but premature here, and the queue would no longer commit atomically with domain state.

## Consequences

- Easier: claiming, fencing and publishing are ordinary transactions.
- Harder: lock order has to stay consistent, and all claims need testing against real PostgreSQL rather than SQLite.

## Reversal condition

Measured queue contention becomes the bottleneck at a load the project actually needs, or the deployment outgrows one database.

## Implemented by

T3.5, T3.6, T3.8, T3.9, T3.10
