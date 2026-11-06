# Delivery tracker

Single source of truth for task status. Each task goes through understand, plan, validate plan, implement, validate implementation, test and document, and leaves a record in [`tasks/`](tasks/). Work stops for sign-off after every task.

**Status:** `todo`, `doing`, `done`, `blocked`

To find a task's commit, run `git log --grep='T0.1'`.

**How work lands on `main`:** each task goes through a pull request, and merges only when CI is green. Branch protection enforces this, so direct pushes to `main` are rejected, including from the owner.

**Before opening a pull request:** `make check` and `make mutate` must both pass. Coverage isn't enough: at R1 it was 100% while real defects went unnoticed.

**Reviews:** [R1, staff review of T0.1 to T1.2](reviews/R1.md)

## M0 — Foundation (IP3-001)

| Task | Work | Status | Record |
|---|---|---|---|
| T0.1 | Repo bootstrap | done | [T0.1](tasks/T0.1.md) |
| T0.2 | Toolchain and `make check` | done | [T0.2](tasks/T0.2.md) |
| T0.3 | Settings and modes | done | [T0.3](tasks/T0.3.md) |
| T0.4 | CI workflow | done | [T0.4](tasks/T0.4.md) |
| T0.5 | Spec errata E1–E17 (blocks M1) | done | [T0.5](tasks/T0.5.md) |
| T0.6 | ADRs | done | [T0.6](tasks/T0.6.md) |

## M1 — Contracts (IP3-002)

| Task | Work | Status | Record |
|---|---|---|---|
| T1.1 | Enums, identity types, canonical JSON and digest helper | done | [T1.1](tasks/T1.1.md) |
| T1.2 | Investigation and attempt state machines | done | [T1.2](tasks/T1.2.md) |
| T1.3 | Model-call and tool reservation ledger transitions | todo | |
| T1.4 | Evidence and selector models | todo | |
| T1.5 | `ReportCandidate`, `DiagnosisReport`, `Measurement` | todo | |
| T1.6 | SDK schema spike (gate: provider and API key) | todo | |
| T1.7 | Cross-record validators and citation checker | todo | |
| T1.8 | Tool envelope and error codes | todo | |

## M2 — Fixtures (IP3-003)

| Task | Work | Status | Record |
|---|---|---|---|
| T2.1 | Fixture schema, snapshot manifest, oracle import contract | todo | |
| T2.2 | Seeded generator and first case | todo | |
| T2.3 | Second and third contrasting cases | todo | |
| T2.4 | Non-leakage test | todo | |

## M3 — Durable slice (IP3-004), needs Docker running

| Task | Work | Status | Record |
|---|---|---|---|
| T3.1 | Compose Postgres, Alembic, runtime role, test harness | todo | |
| T3.2 | Snapshot table and import step | todo | |
| T3.3 | Minimal sanitizer | todo | |
| T3.4 | Create-incident service with idempotency | todo | |
| T3.5 | Start-investigation, event append, admission lock | todo | |
| T3.6 | Fence primitive | todo | |
| T3.7 | CLI over application services | todo | |
| T3.8 | Attempts, capacity slots, claim transaction | todo | |
| T3.9 | Worker loop and lease renewal | todo | |
| T3.10 | Sweeper, `LOST`, recovery, shared terminal transition | todo | |
| T3.11 | Scripted fake model with real fenced writes | todo | |
| T3.12 | Atomic terminal publication | todo | |
| T3.13 | Kill/restart demo | todo | |

## M4 — First live agent (IP3-005/006/007)

| Task | Work | Status | Record |
|---|---|---|---|
| T4.1 | Remaining SDK compatibility checks | todo | |
| T4.2 | Fixture telemetry adapter | todo | |
| T4.3 | Gateway with evidence-before-exposure, one tool | todo | |
| T4.4 | Remaining four tools | todo | |
| T4.5 | Provider call gate with minimal ledger | todo | |
| T4.6 | Bounded briefing builder | todo | |
| T4.7 | InvestigatorRunner and prompt | todo | |
| T4.8 | Application-generated abstention fallback | todo | |
| T4.9 | Structural validator wired into publication | todo | |
| T4.10 | Suite spend cap and live three-case run (gate: paid) | todo | |

## Later

Broken into tasks when we reach them.

- **M5 Hardening** (IP3-008..011)
- **M6 Evaluation** (IP3-012)
- **M7 Showcase** (IP3-013)
- **Optional:** IP3-014 simulated approvals, IP3-015 real telemetry and MCP
