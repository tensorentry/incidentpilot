# ADR-006: Structural, numeric and semantic report validation

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §10, §18.1

## Context

Citing a real log line doesn't prove a causal statement. A single "verified" label would overclaim.

## Decision

Report structural, numeric and semantic validation as separate statuses. The worker never assesses semantics (E6).

## Options considered

- **Three separate stages (chosen):** Each label states exactly what was checked.
- **One verified status:** Simpler to display, but it implies proof that deterministic checks can't give.

## Consequences

- Easier: reports are honest about what was checked.
- Harder: the UI has to explain several statuses, and semantic review needs a separate oracle or human process.

## Reversal condition

A method is shown to assess causal support reliably on held-out cases. Even then, it should stay a separate labelled stage.

## Implemented by

T1.5, T1.7, T4.9, M5
