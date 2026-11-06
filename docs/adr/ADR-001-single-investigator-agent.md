# ADR-001: One agent for the initial benchmark

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §1.1, §18.1

## Context

The project needs to show what an agent adds over simpler approaches. Each extra agent adds handoffs, cost and variables, which makes results harder to attribute.

## Decision

Start with a single investigator agent. Add specialist agents only if measurements justify them.

## Options considered

- **Single agent (chosen):** Simple to attribute and evaluate, but one context has to carry the whole investigation.
- **Specialist multi-agent:** Could divide the work, but adds handoff errors, cost and an evaluation confound.

## Consequences

- Easier: evaluation results map to one component, and there are no handoff failures to debug.
- Harder: one bounded context has to hold all the evidence an investigation needs.

## Reversal condition

A paired comparison on identical snapshots shows a specialist design beats the single agent by more than its added cost. This is the optional multi-agent benchmark in §16.2.

## Implemented by

T4.7
