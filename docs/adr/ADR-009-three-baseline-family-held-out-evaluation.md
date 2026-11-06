# ADR-009: Three-baseline, family-held-out evaluation

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §14, §18.1

## Context

Comparing rules against an agent mixes two effects: language synthesis and adaptive tool selection. Tuning on all cases would also inflate results.

## Decision

Compare fixed rules, fixed queries with LLM synthesis, and the adaptive agent, on identical snapshots, with causal families held out.

## Options considered

- **Three baselines with held-out families (chosen):** Separates synthesis gain from tool-selection gain.
- **Rules against agent only:** Cheaper, but the two effects can't be separated.

## Consequences

- Easier: claims about the agent can be defended.
- Harder: more paid runs, and the splits need freezing before tuning.

## Reversal condition

A new strategy introduces a confound these baselines can't separate, or a challenge set has been studied repeatedly. §14.1 then retires it to regression.

## Implemented by

M6
