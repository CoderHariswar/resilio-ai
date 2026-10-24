.DEFAULT_GOAL := help
PG_TEST_URL ?= postgresql://postgres:resilio@localhost:55432/postgres

.PHONY: help install hooks dev test test-pg lint fmt typecheck check up down logs watch simulate simulate-webhook eval seed clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + dev dependencies with uv
	uv sync --extra dev

hooks: install ## Install pre-commit hooks
	uv run pre-commit install

dev: ## Run the API with hot reload (Mock Mode, in-memory storage)
	uv run resilio serve --reload

test: ## Run the full test suite with coverage
	uv run pytest --cov

test-pg: ## Run integration tests against a throwaway pgvector container
	docker run -d --rm --name resilio-pgtest -p 55432:5432 -e POSTGRES_PASSWORD=resilio pgvector/pgvector:pg16
	@until docker exec resilio-pgtest pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
	TEST_DATABASE_URL=$(PG_TEST_URL) uv run pytest -m integration; status=$$?; docker stop resilio-pgtest; exit $$status

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy --strict
	uv run mypy

check: lint typecheck test ## Everything CI runs

up: ## Boot the full stack (api + postgres + redis) in Mock Mode
	docker compose up -d --build --wait

down: ## Stop the stack
	docker compose down

logs: ## Follow API logs (terminal notifications show up here)
	docker compose logs -f api

watch: ## Hot-reload the containerised API on code changes
	docker compose watch

simulate: ## Fire a synthetic memory leak into the running stack
	docker compose exec api resilio dev simulate --type memory-leak

simulate-webhook: ## Send a Sentry-format webhook to the running stack
	uv run python scripts/mock_outage_simulator.py --type bad-deploy --source sentry

eval: ## Run the 50-scenario offline benchmark
	uv run resilio eval run

seed: ## Write benchmarks/scenarios.json
	uv run python scripts/seed_eval_benchmark.py

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
