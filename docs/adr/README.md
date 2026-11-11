# Architecture decision records

Each record covers context, options, the decision, consequences and what would reverse it, as spec §18.1 requires. ADR-001 to 010 come from the spec, and ADR-011 to 014 come from accepted errata.

**Accepted means decided, not proven.** No ADR has been validated by a running system yet. Most errata are corrections rather than trade-offs, so they stay in [`spec-errata.md`](../spec-errata.md).

| ADR | Decision |
|---|---|
| [ADR-001](ADR-001-single-investigator-agent.md) | One agent for the initial benchmark |
| [ADR-002](ADR-002-modular-python-monolith.md) | Modular Python monolith |
| [ADR-003](ADR-003-postgres-job-queue-fenced-leases.md) | PostgreSQL job queue and fenced leases |
| [ADR-004](ADR-004-application-owned-state-budgets.md) | Application-owned state and budgets |
| [ADR-005](ADR-005-immutable-snapshot-evidence-visibility.md) | Immutable snapshots and evidence visibility |
| [ADR-006](ADR-006-report-validation-separation.md) | Structural, numeric and semantic report validation |
| [ADR-007](ADR-007-offline-live-replay-egress.md) | Offline, live and replay egress modes |
| [ADR-008](ADR-008-application-only-simulated-remediation.md) | Application-only simulated remediation |
| [ADR-009](ADR-009-three-baseline-family-held-out-evaluation.md) | Three-baseline, family-held-out evaluation |
| [ADR-010](ADR-010-no-vector-db-or-mcp-in-p0.md) | No vector database or MCP in P0 |
| [ADR-011](ADR-011-quantities-in-typed-measurements.md) | Quantities only in typed measurements |
| [ADR-012](ADR-012-request-mode-matches-process-mode.md) | Request mode must match process mode |
| [ADR-013](ADR-013-evidence-row-is-smallest-citable-record.md) | An evidence row is the smallest citable record |
| [ADR-014](ADR-014-owner-isolation-in-postgres.md) | PostgreSQL enforces owner isolation |
