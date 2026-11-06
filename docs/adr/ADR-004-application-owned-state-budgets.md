# ADR-004: Application-owned state and budgets

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §1.2, §4.1, §18.1

## Context

An agent SDK tracks turns and traces, but it knows nothing about authorization, billing limits or lease ownership. Crash recovery can't depend on SDK memory.

## Decision

The application and its database own identity, authorization, budgets, state and validation. The SDK owns turn sequencing behind an adapter.

## Options considered

- **Application-owned (chosen):** One authority for state, and the SDK stays replaceable.
- **SDK as state authority:** Less code, but SDK traces and state aren't authoritative for access, billing or recovery.

## Consequences

- Easier: the SDK can be swapped, and recovery rebuilds context from committed evidence.
- Harder: every model call needs an instrumented gate (T4.5), and the agent's mid-turn state is lost on crash by design.

## Reversal condition

A pinned SDK provides durable, fence-aware state and complete pre-call accounting, shown by the compatibility checks in §9.3.

## Implemented by

T1.6, T4.1, T4.5
