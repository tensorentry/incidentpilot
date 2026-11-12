.DEFAULT_GOAL := check
.PHONY: sync lock-check format lint types test check mutate fixtures

UV_RUN := uv run --frozen

sync:
	uv sync --frozen

lock-check:
	uv lock --check

format:
	$(UV_RUN) ruff check --fix .
	$(UV_RUN) ruff format .

lint:
	$(UV_RUN) ruff format --check .
	$(UV_RUN) ruff check .

types:
	$(UV_RUN) mypy

test:
	$(UV_RUN) pytest

check: lock-check lint types test

mutate:
	$(UV_RUN) python scripts/mutate.py

fixtures:
	$(UV_RUN) python -m incidentpilot.evaluation.generate --root scenarios
