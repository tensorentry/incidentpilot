# IncidentPilot

A local-first lab for evidence-linked investigation of simulated service incidents, using one bounded AI investigator. It's a learning and engineering showcase, not a production incident tool.

**Status:** early implementation. The toolchain, settings and core domain types exist. There's no worker, database or agent yet, and no benchmark has been run. See [`docs/delivery/TASKS.md`](docs/delivery/TASKS.md).

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

CI runs `make check` with the network disabled, so a test that needs network or keys fails.

## Configuration

Copy `.env.example` to `.env`. With no configuration, the app runs in `offline_demo` mode and sends nothing off this machine.

| Setting | Default | Meaning |
|---|---|---|
| `INCIDENTPILOT_MODE` | `offline_demo` | `offline_demo`, `live` or `replay` |
| `INCIDENTPILOT_PROVIDER_API_KEY` | none | Required in `live` mode |
| `INCIDENTPILOT_TRACE_EXPORT_ENABLED` | `false` | Export SDK traces; `live` mode only |

Unknown `INCIDENTPILOT_` settings are rejected at startup, so a typo can't silently leave `live` mode or tracing on. Other entries in `.env` are ignored.

## License

[MIT](LICENSE)
