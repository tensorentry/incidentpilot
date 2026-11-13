.DEFAULT_GOAL := check
.PHONY: sync lock-check format lint types test check mutate fixtures \
	infra-up infra-down migrate test-integration

UV_RUN := uv run --frozen
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.dev.yml

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
	$(UV_RUN) pytest tests/unit

check: lock-check lint types test

mutate:
	$(UV_RUN) python scripts/mutate.py

fixtures:
	$(UV_RUN) python -m incidentpilot.evaluation.generate --root scenarios

# PostgreSQL on loopback for development and integration tests (erratum E10).
infra-up:
	$(COMPOSE) up --detach --wait

infra-down:
	$(COMPOSE) down

migrate:
	$(UV_RUN) python -m incidentpilot.persistence.migrate

test-integration:
	$(UV_RUN) pytest tests/integration
