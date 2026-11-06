# ADR-002: Modular Python monolith

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §1.2, §18.1

## Context

The system runs as one local installation, built by one owner. It still needs separate API and worker processes, so that recovery can be demonstrated.

## Decision

One Python codebase with separate API and worker entry points, sharing domain modules and one database.

## Options considered

- **Modular monolith (chosen):** One deployable codebase with internal module boundaries.
- **Microservices:** Independent deployment, but premature for one local installation, and it adds network failure modes with no benefit here.

## Consequences

- Easier: shared typed contracts, and transactions that span the whole domain.
- Harder: module boundaries, such as the oracle boundary (E12), need tooling to enforce them, because the runtime won't.

## Reversal condition

Components need to scale or deploy independently, or the project moves beyond one local installation.

## Implemented by

T0.2, T2.1, T3.9
