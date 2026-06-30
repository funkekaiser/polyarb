# polyarb — convenience targets
# All targets wrap the canonical `uv run` / `docker compose` commands documented
# in CLAUDE.md and README.md. No new behaviour is added here.
#
# Prerequisites: uv installed (https://docs.astral.sh/uv/getting-started/installation/)
#   Run `make sync` once per session before anything else.

.PHONY: help sync scan monitor backtest replay record \
        test lint typecheck gate \
        docker-build docker-up docker-down docker-logs

DOCKER_COMPOSE := docker compose -f docker/docker-compose.yml

# Default target — print this help.
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

sync: ## Install / update all dependencies (run once per session)
	uv sync --dev

# ---------------------------------------------------------------------------
# Scanner — local
# ---------------------------------------------------------------------------

scan: ## One read-only scanner pass (discover → detect → rank → emit, then exit)
	uv run polyarb scan --passes 1

monitor: ## Continuous read-only scanner loop — runs until Ctrl-C / SIGTERM
	uv run polyarb scan

backtest: ## Summarise stored opportunity history from the SQLite database
	uv run polyarb backtest

replay: ## Re-print stored opportunities in chronological order
	uv run polyarb replay

record: ## Capture live read-only API samples into test-fixture files
	uv run polyarb record

# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

lint: ## Lint and check formatting (ruff)
	uv run ruff check . && uv run ruff format --check .

typecheck: ## Run mypy strict type checks
	uv run mypy src

test: ## Run the full offline test suite (no network, fixture-based)
	uv run pytest -q

gate: lint typecheck test ## Full pre-commit gate: lint + typecheck + test

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker-build: ## Build the container image
	$(DOCKER_COMPOSE) build

docker-up: ## Start the containerised scanner in the background
	$(DOCKER_COMPOSE) up -d

docker-down: ## Stop and remove the containerised scanner (data volume preserved)
	$(DOCKER_COMPOSE) down

docker-logs: ## Tail the containerised scanner logs (Ctrl-C to stop tailing)
	$(DOCKER_COMPOSE) logs -f
