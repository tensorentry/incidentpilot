# ADR-010: No vector database or MCP in P0

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §2.2, §18.1

## Context

Both are popular in agent systems, but the P0 cases don't need them, and they would widen the attack surface and the work.

## Decision

Leave both out of P0. All tools go through one typed gateway, so either could be added behind it later.

## Options considered

- **Typed gateway only (chosen):** Smallest capability surface.
- **Add vector retrieval and MCP now:** Broader capability, but unjustified by any current requirement.

## Consequences

- Easier: a small, closed tool set that's simple to secure and test.
- Harder: runbook search and real telemetry wait for P1 and P2.

## Reversal condition

User or problem evidence justifies them, such as runbook retrieval that plain search can't serve, or a real adapter that needs MCP (IP3-015).

## Implemented by

T4.3, IP3-015
