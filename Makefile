# FinGuard AI developer commands (local, without Docker). See README.md.
PY := backend/.venv/bin/python
.PHONY: install db-create migrate seed api worker scheduler tick web test eval build compose-up

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
	cd frontend && npm ci

db-create:  ## local PostgreSQL role + databases (dev/test)
	psql -d postgres -c "CREATE ROLE finguard LOGIN PASSWORD 'finguard_dev_only' CREATEDB;" || true
	createdb -O finguard finguard || true
	createdb -O finguard finguard_test || true

migrate:
	cd backend && .venv/bin/alembic upgrade head && .venv/bin/python -m finguard.cli bootstrap

seed:       ## requires DEMO_PASSWORD
	cd backend && .venv/bin/python -m finguard.cli seed-demo

api:
	cd backend && .venv/bin/uvicorn finguard.main:app --reload --port 8010

worker:
	cd backend && .venv/bin/python -m finguard.worker.main worker

scheduler:
	cd backend && .venv/bin/python -m finguard.worker.main scheduler

tick:
	cd backend && .venv/bin/python -m finguard.worker.main tick

web:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pytest
	cd frontend && npx tsc -b

eval:
	cd backend && .venv/bin/python -m finguard.evaluation.run_all --record

build:
	cd frontend && npm run build

compose-up:
	docker compose up --build
