# ADR-008: Application-only simulated remediation

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §13, §18.1

## Context

Logs and runbooks are untrusted and could carry injected instructions. If the model could act, an injection could cause an effect.

## Decision

The agent has no remediation tool. It can only recommend. An application controller turns approved recommendations into a transactional simulator change.

## Options considered

- **Application controller with human approval (chosen):** One approval path, and the effect is transactional in the simulator.
- **Rollback tool available to the agent:** More autonomous, but injected or mistaken output could cause effects directly.

## Consequences

- Easier: injection can't cause an action, because no action tool exists.
- Harder: remediation needs its own approval flow and release gate (P1).

## Reversal condition

A real actuator is proposed. Atomicity can no longer be assumed, so §13.2 requires durable operation IDs, reconciliation of unknown outcomes and a fresh safety review.

## Implemented by

IP3-014
