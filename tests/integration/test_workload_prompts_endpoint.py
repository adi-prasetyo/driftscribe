"""GET /workloads/{name}/prompts — open, read-only crew prompt view."""
import pytest
from fastapi.testclient import TestClient

from agent.main import app


def test_drift_prompts_ok_and_shaped():
    r = TestClient(app).get("/workloads/drift/prompts")
    assert r.status_code == 200
    b = r.json()
    assert b["workload"] == "drift"
    assert b["display_name"] == "Anchor"
    assert b["descriptor"]
    assert b["recheck_prompt"].strip()
    assert b["chat_prompt_distinct"] is True
    assert b["chat_prompt"].strip()
    assert b["source_dir"] == "workloads/drift"
    assert b["revision"]                  # K_REVISION or "local"
    assert "demo" in b["demo_note"].lower()


def test_explore_prompts_single():
    b = TestClient(app).get("/workloads/explore/prompts").json()
    assert b["chat_prompt_distinct"] is False
    assert b["chat_prompt"] is None
    assert b["recheck_prompt"].strip()


def test_unknown_workload_404():
    assert TestClient(app).get("/workloads/nope/prompts").status_code == 404


@pytest.mark.no_auth_override
def test_prompts_open_without_token(monkeypatch):
    from agent.config import get_settings
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "tok-prompts-123")
    get_settings.cache_clear()
    r = TestClient(app).get("/workloads/drift/prompts")   # no X-DriftScribe-Token header
    assert r.status_code == 200
