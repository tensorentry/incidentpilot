# ADR-014: PostgreSQL enforces owner isolation

**Status:** Proposed
**Date:** 2026-11-08
**Deciders:** Project owner
**Source:** Erratum E19

## Context

The spec keeps owners apart with a check in each route (§12.4), so one missed filter leaks another owner's data. Snapshots and evidence are shared across owners. The project stays a local installation, and §1.4 still excludes multi-tenant hosting, but separate users of one installation must not see each other's incidents, evidence or reports.

## Decision

Every row holding tenant data has an `owner_id`. Composite foreign keys keep references inside one owner, and forced row-level security limits every runtime query to the owner set for the transaction. A small, closed set of `SECURITY DEFINER` functions lets the scheduler see every owner, and they return only IDs and counts.

## Options considered

- **Database-enforced owner isolation (chosen):** a missed filter returns nothing, and cross-owner references are impossible in the schema. It costs wider keys and a second access path for the scheduler.
- **Owner checks in application code only (spec as written):** simpler, but every route, gateway path and worker query has to remember the filter, and nothing catches one that doesn't.
- **A schema or database per owner:** stronger separation, but migrations, the job queue and capacity slots would have to span schemas, which is too much for a local lab.
- **Incident isolation only:** already designed through snapshots and visibility grants, but it doesn't separate users.

## Consequences

- Easier: deleting one owner's data doesn't touch another's, and a bug in the gateway or a route can't cross owners.
- Harder: the claim and sweeper code uses two access paths, fixtures are stored once per owner, and every unique key includes the owner.
- Deferred: per-owner queue limits and spend caps, and real authentication (IP3-010).

## Reversal condition

Row-level security policies become a measured query-plan problem, or the project becomes single-user by decision.

## Implemented by

T1.4, T3.1, T3.2, T3.8, T3.10, T3.11, T3.14
