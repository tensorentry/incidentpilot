# IncidentPilot

A local-first lab for evidence-linked investigation of simulated service incidents, using one bounded AI investigator. It's a learning and engineering showcase, not a production incident tool.

**Status:** early implementation. The toolchain, settings and core domain types exist. PostgreSQL, its roles and the snapshot registry exist. A worker runs investigations end to end with a scripted stand-in for the model, and recovers from crashes. The agent SDK is pinned and its behaviour checked offline. A tool gateway commits evidence before the model can see it, with the first telemetry tool. There's no live agent yet, and no benchmark has been run. See [`docs/delivery/TASKS.md`](docs/delivery/TASKS.md).

## Design documents

- `docs/IncidentPilot_v3_Technical_Design.md` — canonical full engineering specification with 19 GitHub-rendered Mermaid diagrams.
- `docs/architecture/diagrams/D01_...mmd` through `D19_...mmd` — individual editable Mermaid diagram sources.
- `docs/spec-errata.md` — corrections to the frozen spec. Where the two disagree, the errata win.
- `docs/adr/` — architecture decision records, including what would reverse each decision.

GitHub renders Mermaid code fences within Markdown. Use a compatible Mermaid viewer/editor to edit standalone `.mmd` files.

Read sections 1–3 for design decisions and scope; 4–15 for architecture, workflows, contracts and evaluation; 16–18 for delivery/release decisions.

## Development

Implementation is in progress. See [`docs/delivery/TASKS.md`](docs/delivery/TASKS.md) for status.

Install [uv](https://docs.astral.sh/uv/), then:

```bash
make sync    # install the locked toolchain; needs network once
make check   # lock check, formatting, lint, types and tests; needs no network or keys
make format  # apply formatting and safe lint fixes
make mutate  # mutation testing; fails if any deliberate bug goes unnoticed
```

`uv` downloads its own Python 3.12, so a system Python isn't used.

CI runs `make check` with the network disabled, so a test that needs network or keys fails. A separate CI job runs the integration tests against PostgreSQL started from the project's own Compose files.

### Database

State lives in PostgreSQL, run with Docker Compose. Fill in the database settings in `.env` (see `.env.example`), then:

```bash
make infra-up          # start PostgreSQL, published on 127.0.0.1 only
make migrate           # apply migrations as the migrator role
make import-snapshots  # register scenarios/observations as snapshots for the local owner
make test-integration  # integration tests against the real database
make infra-down        # stop it; the data volume is kept
```

The passwords in `.env` are set once, when the data volume is first created. To change them, run `docker compose down --volumes`, which deletes all local data.

`make mutate` also runs the integration tests, so it needs the database running and configured too.

Two roles connect, and a third owns the few functions that must see every owner ([ADR-014](docs/adr/ADR-014-owner-isolation-in-postgres.md)):

| Role | Used by | Can |
|---|---|---|
| `incidentpilot_migrator` | `make migrate` | Own the schema and change it |
| `incidentpilot_runtime` | API, workers, CLI | Use only what migrations grant; owns nothing and can't bypass row security |
| `incidentpilot_scheduler` | Nobody: it can't log in | Own the scheduler's `SECURITY DEFINER` functions, which return only IDs and counts (erratum E19) |

The app refuses to start its runtime connection with a role that could bypass row security, including a member of one that can.

### Command line

Until the HTTP API exists, the `incidentpilot` command calls the application services directly (erratum E10). It acts for the local owner unless `--owner <uuid>` names another, and prints JSON.

```bash
uv run incidentpilot import-snapshots
uv run incidentpilot create-incident --service checkout-api --severity sev2 \
  --start 2026-11-04T18:15:00Z --end 2026-11-04T18:45:00Z \
  --description "Checkout p95 latency is up" --key my-key-1
uv run incidentpilot start-investigation <incident-id> --snapshot snap-9cbd373fc274 \
  --start 2026-11-04T18:15:00Z --end 2026-11-04T18:45:00Z
uv run incidentpilot show-investigation <investigation-id>
uv run incidentpilot cancel-investigation <investigation-id>
```

Rerunning a command with the same `--key` returns the original result instead of creating another. Without `--key`, a fresh one is generated and printed. Cancelling an investigation that has already ended fails; cancelling one that's already cancelled reports that nothing changed.

### Workers and the kill/restart demo

`uv run incidentpilot worker` claims queued `offline_demo` investigations and runs them until it is stopped. Pass `--jobs N` to stop after N investigations have ended. It plays a scripted stand-in for the model, which writes real ledger, evidence and report rows; the live investigator comes later. Between claims it sweeps up investigations whose worker died, so a worker started after a crash recovers the dead worker's work. Each worker uses two runtime connections.

`make demo` shows that recovery with real processes, using 2-second leases:

1. **Kill and restart.** It kills one worker with SIGKILL while a model call is in flight, then starts another. The second worker finds the lapsed lease, marks the first attempt `LOST` and its call `UNKNOWN`, and completes the investigation at epoch 2 with one report.
2. **A stale worker wakes up.** It freezes a worker with SIGSTOP mid-call, lets a second one recover and publish, then resumes the first. The first worker's late commit, still at epoch 1, is refused.

It checks the database after each scene, and exits non-zero if anything doesn't match. It needs `make migrate` first, and takes about 15 seconds.

## Configuration

Copy `.env.example` to `.env`. With no configuration, the app runs in `offline_demo` mode and sends nothing off this machine.

| Setting | Default | Meaning |
|---|---|---|
| `INCIDENTPILOT_MODE` | `offline_demo` | `offline_demo`, `live` or `replay` |
| `INCIDENTPILOT_PROVIDER_API_KEY` | none | Required in `live` mode |
| `INCIDENTPILOT_TRACE_EXPORT_ENABLED` | `false` | Export SDK traces; `live` mode only |
| `INCIDENTPILOT_DATABASE_URL` | none | Runtime role's connection string, used by the API, workers and CLI |
| `INCIDENTPILOT_MIGRATION_DATABASE_URL` | none | Migrator role's connection string, used by `make migrate` |

Unknown `INCIDENTPILOT_` settings are rejected at startup, so a typo can't silently leave `live` mode or tracing on. Other entries in `.env` are ignored.

## License

[MIT](LICENSE)
