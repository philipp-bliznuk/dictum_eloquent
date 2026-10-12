.DEFAULT_GOAL := help
SHELL := /bin/bash

PY := uv run --
UI := cd ui && bun
PORT ?= 3000
LOCAL_ENV := ENVIRONMENT=local DATA_DIR=./data DATABASE_URL=sqlite+aiosqlite:///./data/dictum.db

.PHONY: help install fmt lint test check clean ui-install ui-dev ui-build dev build up down logs migration migration_auto migrate

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## sync python + ui deps, install git hooks (when inside a git repo)
	uv sync --frozen
	$(UI) install --frozen-lockfile
	@if [ -d .git ]; then $(PY) pre-commit install --install-hooks; else echo "no .git, skipping hooks"; fi

fmt: ## format python + ui
	$(PY) ruff format .
	$(PY) ruff check --fix .
	$(UI) run format

lint: ## lint python + ui
	$(PY) ruff format --check .
	$(PY) ruff check .
	$(PY) bandit -c pyproject.toml -r app.py dictum gunicorn.conf.py
	$(UI) run check

test: ## run tests with coverage
	$(PY) pytest

check: lint test ## lint + test

clean: ## remove caches and build artifacts
	rm -rf .ruff_cache .pytest_cache .coverage htmlcov ui/dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

ui-install: ## bun install
	$(UI) install --frozen-lockfile

ui-dev: ## vite dev server with api proxy
	$(UI) run dev

ui-build: ## build spa into ui/dist
	$(UI) run build

dev: ## run api locally with reload (ENVIRONMENT=local)
	$(LOCAL_ENV) $(PY) uvicorn app:create_app --factory --reload --port $(PORT)

build: ## build container image (docker format keeps HEALTHCHECK under podman)
	BUILDAH_FORMAT=docker podman compose build

up: ## start container (recreates it when the image changed)
	podman compose up -d --force-recreate

down: ## stop container
	podman compose down

logs: ## follow container logs
	podman compose logs -f

migration: ## new empty migration: make migration m="message"
	$(LOCAL_ENV) $(PY) alembic revision -m "$(m)"

migration_auto: ## autogenerate migration against local db: make migration_auto m="message"
	$(LOCAL_ENV) $(PY) alembic revision --autogenerate -m "$(m)"

migrate: ## apply migrations to local db
	$(LOCAL_ENV) $(PY) alembic upgrade head
