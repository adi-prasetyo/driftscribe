.PHONY: install test lint run-agent run-demo dry-recheck ui ui-dev ui-check ui-test ui-smoke

install:
	uv pip install -e ".[dev]"

test:
	uv run pytest -v

lint:
	uv run ruff check .

# --- Frontend (Svelte+Vite operator UI) -------------------------------------
ui:                 ## build the SPA bundle into agent/static/
	cd frontend && npm ci && npm run build

ui-dev:             ## vite dev server (hot reload) — uses the local backend for /chat etc.
	cd frontend && npm run dev

ui-check:           ## svelte-check type-check
	cd frontend && npm run check

ui-test:            ## vitest unit tests for lib/*
	cd frontend && npm run test:unit

ui-smoke:           ## build + boot uvicorn + mock-Playwright smoke
	cd frontend && npm run build && npm run test:smoke

run-agent:
	DRY_RUN=true uv run uvicorn agent.main:app --reload --port 8080

run-demo:
	uv run uvicorn demo.main:app --reload --port 8081

dry-recheck:
	curl -s -X POST http://localhost:8080/recheck | jq .
