# ADR-005: Immutable snapshots and evidence visibility

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §1.2, §7.3, §18.1

## Context

A recovered attempt must reuse earlier evidence without accepting evidence from outside the investigation, and every citation must stay resolvable.

## Decision

Each investigation reads one frozen snapshot. Evidence is immutable, and an explicit visibility grant makes it available to an investigation.

## Options considered

- **Frozen snapshot with visibility grants (chosen):** Cross-attempt reuse works, and foreign evidence can't be cited.
- **Query live telemetry per attempt:** Fresher data, but attempts could disagree and citations could stop resolving.
- **Globally addressable evidence IDs:** Simpler lookups, but any ID could be cited across investigations.

## Consequences

- Easier: results are reproducible, and recovery is safe.
- Harder: post-action state needs a new linked snapshot, and real telemetry (P2) has to be frozen on ingest.

## Reversal condition

Real telemetry adapters can't freeze data at acceptable cost, which would force an explicit bounded-staleness model.

## Implemented by

T1.4, T3.2, T3.11
