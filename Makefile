.DEFAULT_GOAL := help
PYTHON ?= python

.PHONY: help install dev run test lint fmt docker-build docker-up docker-down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install the package
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install .

dev: ## Install with development extras
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"

run: ## Start the console on http://localhost:8501
	$(PYTHON) -m dcc_console

test: ## Run the test suite
	$(PYTHON) -m pytest

lint: ## Check formatting and lint rules
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

fmt: ## Auto-format the codebase
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

docker-build: ## Build the container image
	docker compose build

docker-up: ## Start the console in Docker
	docker compose up -d

docker-down: ## Stop the container
	docker compose down

clean: ## Remove build and cache artefacts
	$(PYTHON) -c "import pathlib,shutil; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
	$(PYTHON) -c "import shutil; [shutil.rmtree(d, ignore_errors=True) for d in ('build','dist','.pytest_cache','.ruff_cache')]"
