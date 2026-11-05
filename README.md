# IncidentPilot v3 Design Package

This package contains the **proposed** IncidentPilot v3 architecture. No code, tests or benchmarks are implemented by this design package.

- `docs/IncidentPilot_v3_Technical_Design.md` — canonical full engineering specification with 19 GitHub-rendered Mermaid diagrams.
- `docs/architecture/diagrams/D01_...mmd` through `D19_...mmd` — individual editable Mermaid diagram sources.

GitHub renders Mermaid code fences within Markdown. Use a compatible Mermaid viewer/editor to edit standalone `.mmd` files.

Read sections 1–3 for design decisions and scope; 4–15 for architecture, workflows, contracts and evaluation; 16–18 for delivery/release decisions.

## Development

Implementation is in progress. See [`docs/delivery/TASKS.md`](docs/delivery/TASKS.md) for status.

Install [uv](https://docs.astral.sh/uv/), then:

```bash
make sync    # install the locked toolchain; needs network once
make check   # lock check, formatting, lint, types and tests; needs no network or keys
make format  # apply formatting and safe lint fixes
```

`uv` downloads its own Python 3.12, so a system Python isn't used.
