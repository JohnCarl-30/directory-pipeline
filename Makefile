.DEFAULT_GOAL := help
VENV := .venv
PY   := $(VENV)/bin/python

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

install: ## Create the venv and install the package with dev extras
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

demo: ## Run the full pipeline locally (no Docker, no credentials)
	$(PY) scripts/run_local.py

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Lint and format-check
	$(VENV)/bin/ruff check src tests scripts
	$(VENV)/bin/ruff format --check src tests scripts

fmt: ## Auto-format
	$(VENV)/bin/ruff format src tests scripts
	$(VENV)/bin/ruff check --fix src tests scripts

up: ## Start the full stack (OpenSearch + Temporal + workers + API)
	docker compose up -d --build
	@echo "Temporal UI       http://localhost:8080"
	@echo "OpenSearch Dash   http://localhost:5601"
	@echo "API docs          http://localhost:8000/docs"

down: ## Stop the stack
	docker compose down

clean: ## Stop the stack and delete volumes
	docker compose down -v
	rm -rf reports .pytest_cache .ruff_cache

logs: ## Tail worker logs
	docker compose logs -f worker

crawl: ## Trigger a crawl through the running API
	curl -sS -X POST localhost:8000/ingest/crawl \
	  -H 'content-type: application/json' \
	  -d '{"categories":["software","logistics"],"max_pages":3}' | python3 -m json.tool

search: ## Sample search against the running API
	curl -sS 'localhost:8000/search?q=analytics&size=5' | python3 -m json.tool

reindex: ## Trigger a zero-downtime reindex
	curl -sS -X POST localhost:8000/ingest/reindex \
	  -H 'content-type: application/json' \
	  -d '{"alias":"companies","reason":"mapping change"}' | python3 -m json.tool

worker: ## Run a worker against localhost Temporal
	$(PY) -m directory_pipeline.orchestration.worker

api: ## Run the API locally
	$(PY) -m directory_pipeline.api.main

.PHONY: help install demo test lint fmt up down clean logs crawl search reindex worker api
