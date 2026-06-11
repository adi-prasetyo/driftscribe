"""Integration tests for the /chat endpoint (Phase 11.7).

The /chat endpoint is the operator's natural-language interface. These
tests cover the wiring around the ADK runner — the actual LLM-driven
turn is mocked at the ``agent.adk_agent.run_chat`` level so we don't
need a live Gemini call.

Coverage:
- The X-DriftScribe-Token guard applies (same Phase 11.1 surface as
  /recheck).
- USE_ADK=false returns 503 with a clear "ADK not enabled" detail.
- USE_ADK=true wires through to run_chat and surfaces the
  ``{reply, tool_calls, session_id}`` payload.
- Errors inside the ADK turn surface as 502 (distinguishable from the
  503 disabled state).
- Extra fields in the request body are rejected at 422 (closed schema).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app

# Phase 17.A.3: the four drift worker URL env vars (READER_URL etc.)
# AND the workload cache are wired by the autouse fixture in
# ``tests/integration/conftest.py``. /chat's workload pre-resolve step
# needs those env vars; without them the handler returns 503 before
# any ``run_chat`` mock fires. The conftest-level setup keeps each test
# body uncluttered. Tests that specifically exercise the missing-env
# 503 path (test_workload_routing.py) clear those vars on entry.


def test_chat_returns_503_when_use_adk_false(monkeypatch) -> None:
    """USE_ADK=false: /chat has no engine. 503 (not 501) because the
    feature exists at this revision; it's just disabled."""
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post("/chat", json={"prompt": "hi"})
    assert r.status_code == 503
    assert "adk" in r.json()["detail"].lower()


def test_chat_happy_path_returns_reply_and_tool_calls(monkeypatch) -> None:
    """USE_ADK=true: /chat invokes run_chat and surfaces the result.

    We mock at agent.adk_agent.run_chat so we don't need a live LLM.
    The /chat endpoint imports run_chat lazily so the patch site needs
    to be the agent module (the import target), not agent.main."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    fake = AsyncMock(
        return_value={
            "reply": "Live env shows PAYMENT_MODE=live (drifted from mock)",
            "tool_calls": ["read_live_env_tool", "load_contract_tool"],
            "session_id": "abc-123",
        }
    )
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "what's the live state?"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert "PAYMENT_MODE" in body["reply"]
    assert body["tool_calls"] == ["read_live_env_tool", "load_contract_tool"]
    assert body["session_id"] == "abc-123"
    # Phase 17.A.3: workload="drift" is passed through by default. Pin
    # the full call signature so a future routing-layer regression
    # doesn't silently drop the workload kwarg.
    fake.assert_awaited_once_with(
        "what's the live state?", session_id=None, workload="drift",
        autonomy_mode="propose_apply",
    )


def test_chat_passes_session_id_through(monkeypatch) -> None:
    """A caller-supplied session_id is forwarded to run_chat unchanged.
    In-memory sessions only in 11.7 — the session_id is currently used
    as a label but is accepted for forward compatibility."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    fake = AsyncMock(return_value={"reply": "ok", "tool_calls": [], "session_id": "s1"})
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        client.post("/chat", json={"prompt": "hi", "session_id": "s1"})

    # Phase 17.A.3: workload="drift" is the default; session_id flows
    # through unchanged.
    fake.assert_awaited_once_with(
        "hi", session_id="s1", workload="drift", autonomy_mode="propose_apply"
    )


def test_chat_surfaces_runtime_error_as_502(monkeypatch) -> None:
    """If run_chat raises (LLM failure, worker error, parse failure),
    /chat surfaces it as 502 with an informative detail. 502 (not 500)
    so operator can distinguish "model misbehaved" from "coordinator
    deploy is broken"."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    fake = AsyncMock(side_effect=RuntimeError("ADK chat agent produced no final response"))
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi"})

    assert r.status_code == 502
    # The chat handler narrows by exception type — RuntimeError from the
    # ADK parse path surfaces as "chat agent failed", WorkerClientError
    # surfaces as "chat worker call failed". Either way, the detail
    # contains the operative phrase "agent failed" so the operator
    # knows it's a model/upstream issue, not a coordinator bug.
    assert "agent failed" in r.json()["detail"]


def test_chat_surfaces_worker_client_error_as_502(monkeypatch) -> None:
    """A WorkerClientError from inside run_chat (the LLM's tool call hit
    a worker error) surfaces as 502 with a "worker call failed" detail.
    The worker's status code is NOT echoed — a worker's 422 (schema
    rejection from the LLM's tool call) shouldn't make /chat return 422,
    which would imply the /chat REQUEST was malformed."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    from agent import worker_client

    fake = AsyncMock(
        side_effect=worker_client.WorkerClientError(422, "bad field", "reader")
    )
    with patch("agent.adk_agent.run_chat", fake):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi"})

    assert r.status_code == 502
    assert "worker call failed" in r.json()["detail"]


def test_chat_rejects_extra_field_with_422(monkeypatch) -> None:
    """ChatRequest has ``extra="forbid"`` — typo'd fields fail closed
    rather than being silently dropped."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post(
        "/chat",
        json={"prompt": "hi", "unknown_field": "x"},
    )
    assert r.status_code == 422


def test_chat_requires_prompt_field(monkeypatch) -> None:
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post("/chat", json={})
    assert r.status_code == 422
