.DEFAULT_GOAL := check
.PHONY: sync lock-check format lint types test check

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
