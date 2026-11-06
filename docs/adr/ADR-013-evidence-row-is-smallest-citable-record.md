# ADR-013: An evidence row is the smallest citable record

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Erratum E15

## Context

Selectors, dedup and numeric recomputation all depend on what one evidence row contains, and the spec doesn't say.

## Decision

One row is the smallest independently citable source record, and a time series counts as one record. Series points are stored in the row.

## Options considered

- **Smallest citable record (chosen):** Dedup works across queries, and citations are precise.
- **One row per tool response:** Fewer rows, but the same log line returned twice becomes two pieces of evidence.

## Consequences

- Easier: precise citations, and numbers can be recomputed without returning to the source.
- Harder: more rows per log query, bounded by the result limit.

## Reversal condition

Row counts or visibility grants become a measured performance problem, or real telemetry has records with no stable identity.

## Implemented by

T1.4, T3.11
