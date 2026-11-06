# ADR-007: Offline, live and replay egress modes

**Status:** Accepted
**Date:** 2026-11-05
**Deciders:** Project owner
**Source:** Spec §2.3, §15.2, §18.1

## Context

Demo output could be mistaken for real model results, and SDK tracing could send data out by accident.

## Decision

Three explicit modes, each with its own egress rules. Every UI and report states its mode. Trace export is separately opt-in and allowed only in `live`.

## Options considered

- **Three explicit modes (chosen):** Egress and quality claims are unambiguous.
- **One flag for real or fake model:** Simpler, but replay and trace export stay ambiguous, which risks misleading results.

## Consequences

- Easier: fake results can't pass as real ones, and egress can be tested.
- Harder: every entry point has to honour the mode.

## Reversal condition

A new need appears, such as a local model. That should add a mode with its own contract, not loosen an existing one.

## Implemented by

T0.3, T4.1, T4.5
