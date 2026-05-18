.PHONY: install test lint run-agent run-demo dry-recheck

install:
	uv pip install -e ".[dev]"

test:
	uv run pytest -v

lint:
	uv run ruff check .

run-agent:
	DRY_RUN=true uv run uvicorn agent.main:app --reload --port 8080

run-demo:
	uv run uvicorn demo.main:app --reload --port 8081

dry-recheck:
	curl -s -X POST http://localhost:8080/recheck | jq .
