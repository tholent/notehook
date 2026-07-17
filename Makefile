.DEFAULT_GOAL := check
PACKAGES := protocol server client

.PHONY: sync lint lint-fix typecheck test $(addprefix test-,$(PACKAGES)) check clean serve

sync: ## Install/refresh all workspace packages
	uv sync --all-packages

lint: ## Ruff over the whole workspace
	uv run ruff check .

lint-fix: ## Ruff with auto-fix
	uv run ruff check --fix .

typecheck: ## Strict mypy over all package sources
	uv run mypy packages/protocol/src packages/server/src packages/client/src

test: $(addprefix test-,$(PACKAGES)) ## All test suites (with coverage gates)

test-protocol:
	cd packages/protocol && uv run pytest

test-server:
	cd packages/server && uv run pytest

test-client:
	cd packages/client && uv run pytest

check: lint typecheck test ## Everything CI runs

serve: ## Run the server locally (expects NOTED_* env vars)
	uv run noted-server

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache
	find packages -type d \( -name __pycache__ -o -name .pytest_cache \) -exec rm -rf {} +
	rm -f packages/*/.coverage
