"""Smoke-test the seam between the FastAPI middleware (which binds
X-Trace-Id to the ContextVar) and the agent event loop (which reads
the same ContextVar through current_trace_id_or_new). If this test
passes, every log line written during a /chat invocation will carry
the request's trace_id — which is what makes the 365-day Logs
Explorer replay work.

Fixture / auth pattern is copied from tests/integration/test_chat_endpoint.py:
- The autouse fixture in tests/integration/conftest.py bypasses
  verify_token via app.dependency_overrides; no header is needed.
- The autouse fixture sets USE_ADK=false. We opt in via monkeypatch
  + get_settings.cache_clear() (cached Settings would otherwise still
  say false and the endpoint would 503 before reaching the Runner).
- We patch agent.adk_agent.Runner (NOT run_chat) so the REAL event
  loop runs and emits the new log lines.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import adk_agent
from agent.config import get_settings
from agent.main import app
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


async def _stub_run(*args, **kwargs):
    yield _Ev(
        [_P(text="thinking about contract", thought=True)],
        partial=False,
    )
    yield _Ev(
        [_P(function_call=SimpleNamespace(name="read_drift"))],
        partial=False,
    )
    yield _Ev(
        [_P(text='{"action":"no_op"}')],
        partial=False,
        final=True,
    )


def test_chat_thought_log_carries_request_trace_id(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    # Opt in to the ADK path — autouse fixture pins USE_ADK=false otherwise.
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()

    fixed_trace = "0" * 32

    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        client = TestClient(app)
        resp = client.post(
            "/chat",
            headers={"X-Trace-Id": fixed_trace},
            json={"prompt": "what is the current drift?", "workload": "drift"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("X-Trace-Id") == fixed_trace

    thoughts = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_thought"
    ]
    assert thoughts, "expected at least one llm_thought log line"
    assert getattr(thoughts[0], "trace_id") == fixed_trace
    assert getattr(thoughts[0], "workload") == "drift"
