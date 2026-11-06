# ADR-012: Request mode must match process mode

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Erratum E8

## Context

Mode arrives per request, but it's also a process setting. Without a rule, an offline worker could claim a live job, or a process could make paid calls by accident.

## Decision

A request's mode must equal the API process's mode, and workers claim only jobs in their own mode. `replay` never creates an investigation.

## Options considered

- **Strict match (chosen):** Accidental paid calls become structurally impossible.
- **One process serving several modes:** More flexible, but it relies on per-request checks to prevent paid calls.

## Consequences

- Easier: egress can be reasoned about per process.
- Harder: switching mode needs a restart, and mixed-mode work needs separate processes.

## Reversal condition

A real workflow needs mixed modes in one process, such as an evaluation suite where restarts become a measured burden.

## Implemented by

T3.5, T3.8
