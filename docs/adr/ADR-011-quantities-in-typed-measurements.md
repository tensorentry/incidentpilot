# ADR-011: Quantities only in typed measurements

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Erratum E2

## Context

The numeric validation stage has to recompute claims, which it can't do from free text.

## Decision

Quantities live in a typed `Measurement`. Numbers with units are rejected from model-written free text, and the application renders values.

## Options considered

- **Typed measurements, no quantities in prose (chosen):** One deterministic rule that the validator can enforce.
- **Match numbers in prose to measurements:** More natural output, but parsing is fragile and can pass wrong numbers.

## Consequences

- Easier: numeric validation is deterministic.
- Harder: the model has to follow the rule, and a breach uses the single repair.

## Reversal condition

T1.6 or the live run in T4.10 shows the model routinely breaks the rule, so repairs go on formatting instead of real errors.

## Implemented by

T1.5, T1.6, T1.7, M5
