"""Contract for the SSE branch of POST /chat (Phase 22).

- ``Accept: text/event-stream`` → 200 ``text/event-stream`` whose frames
  are: a ``meta`` frame with ``trace_id``, default (unnamed) frames for
  each timeline event, and a terminal ``done`` frame with the reply.
- No such Accept header → unchanged JSON dict.
- A failure inside the agent loop (status already committed to 200)
  surfaces as an ``error`` frame carrying a ``status_hint``, not an HTTP
  error code.

Auth is neutralized via ``app.dependency_overrides`` (the project pattern
in tests/integration/conftest.py) — patching the module-level
``verify_token`` does NOT work because it's captured in the route's
``Depends(...)`` at decoration time.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent import main as agent_main
from agent.auth import verify_token


def _parse_sse(text: str):
    """Return list of (event_name|None, data_dict|None) from an SSE body."""
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue  # blank or heartbeat comment
        ev = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        frames.append((ev, data))
    return frames


@pytest.fixture
def _adk_enabled(monkeypatch):
    monkeypatch.setenv("USE_ADK", "true")
    agent_main.get_settings.cache_clear()
    agent_main.app.dependency_overrides[verify_token] = lambda: None
    yield
    agent_main.app.dependency_overrides.pop(verify_token, None)
    agent_main.get_settings.cache_clear()


async def _stub_stream(prompt, session_id=None, *, workload="drift"):
    yield {"type": "event", "event": {
        "event": "tool_call", "tool_name": "read_drift", "tool_args": {},
        "seq": 1, "insert_id": "stream-1", "timestamp": "t"}}
    yield {"type": "result", "reply": "all good",
           "tool_calls": ["read_drift"], "session_id": "sid"}


def test_chat_streams_sse_when_accept_header(_adk_enabled):
    with patch("agent.adk_agent.run_chat_stream", _stub_stream), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"},
                        headers={"Accept": "text/event-stream"})

    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    frames = _parse_sse(r.text)
    assert frames[0][0] == "meta" and "trace_id" in frames[0][1]
    assert any(ev is None and d.get("event") == "tool_call"
               for ev, d in frames)
    done = [d for ev, d in frames if ev == "done"]
    assert done and done[0]["reply"] == "all good"
    assert done[0]["tool_calls"] == ["read_drift"]


async def _stub_stream_with_iac_pr(prompt, session_id=None, *, workload="drift"):
    yield {"type": "event", "event": {
        "event": "tool_call", "tool_name": "open_infra_pr", "tool_args": {},
        "seq": 1, "insert_id": "stream-1", "timestamp": "t"}}
    yield {"type": "result", "reply": "Opened infrastructure PR #73.",
           "tool_calls": ["open_infra_pr"], "session_id": "sid",
           "iac_pr": {"pr_number": 73, "pr_url": "https://x/pull/73"}}


def test_chat_sse_done_carries_iac_pr_when_present(_adk_enabled):
    """A first-authoring run's terminal ``iac_pr`` pointer is passed through to
    the ``done`` frame so the SPA can render a clickable approval CTA."""
    with patch("agent.adk_agent.run_chat_stream", _stub_stream_with_iac_pr), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "provision a bucket", "workload": "drift"},
                        headers={"Accept": "text/event-stream"})

    frames = _parse_sse(r.text)
    done = [d for ev, d in frames if ev == "done"]
    assert done and done[0]["iac_pr"] == {"pr_number": 73, "pr_url": "https://x/pull/73"}


def test_chat_sse_done_omits_iac_pr_when_absent(_adk_enabled):
    """A plain run (no infra PR) carries NO ``iac_pr`` key on the done frame."""
    with patch("agent.adk_agent.run_chat_stream", _stub_stream), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"},
                        headers={"Accept": "text/event-stream"})

    frames = _parse_sse(r.text)
    done = [d for ev, d in frames if ev == "done"]
    assert done and "iac_pr" not in done[0]


@pytest.mark.asyncio
async def test_drain_chat_stream_result_preserves_iac_pr():
    """The JSON (non-SSE) drain projects ``iac_pr`` through when the terminal
    item carries it, and omits it otherwise — contract parity with the SSE path."""
    async def _with(*_a, **_k):
        yield {"type": "result", "reply": "ok", "tool_calls": [], "session_id": "s",
               "iac_pr": {"pr_number": 5, "pr_url": "https://x/pull/5"}}

    async def _without(*_a, **_k):
        yield {"type": "result", "reply": "ok", "tool_calls": [], "session_id": "s"}

    got = await agent_main._drain_chat_stream_result(_with())
    assert got["iac_pr"] == {"pr_number": 5, "pr_url": "https://x/pull/5"}
    got2 = await agent_main._drain_chat_stream_result(_without())
    assert "iac_pr" not in got2


def test_chat_returns_json_without_accept_header(_adk_enabled):
    async def _run_chat(prompt, session_id=None, *, workload="drift"):
        return {"reply": "all good", "tool_calls": [], "session_id": "sid"}

    with patch("agent.adk_agent.run_chat", _run_chat), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["reply"] == "all good"


def test_chat_sse_rebinds_contextvars_for_the_stream_body(_adk_enabled):
    """The generator must re-bind trace_id + workload INSIDE its body.

    Regression guard (Codex): the trace-id middleware and /chat's workload
    `finally` both reset before the StreamingResponse body iterates. If the
    generator doesn't re-bind, events emitted during the stream get a
    fresh, uncorrelated trace_id. This stub reads the live ContextVars and
    echoes them so we can assert they match the meta frame's trace_id and
    the requested workload.
    """
    from agent.workload_context import current_workload
    from driftscribe_lib.logging import get_trace_id

    async def _ctx_echo_stream(prompt, session_id=None, *, workload="drift"):
        yield {"type": "event", "event": {
            "event": "tool_call", "tool_name": "x", "tool_args": {},
            "bound_trace_id": get_trace_id(),
            "bound_workload": current_workload(),
            "seq": 1, "insert_id": "stream-1", "timestamp": "t"}}
        yield {"type": "result", "reply": "ok",
               "tool_calls": [], "session_id": "sid"}

    with patch("agent.adk_agent.run_chat_stream", _ctx_echo_stream), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "explore"},
                        headers={"Accept": "text/event-stream"})

    frames = _parse_sse(r.text)
    meta_tid = frames[0][1]["trace_id"]
    ev = next(d for evn, d in frames if evn is None and d.get("event") == "tool_call")
    assert ev["bound_trace_id"] == meta_tid
    assert ev["bound_workload"] == "explore"


def test_chat_sse_emits_error_frame_on_inloop_failure(_adk_enabled):
    async def _boom(prompt, session_id=None, *, workload="drift"):
        raise RuntimeError("model misbehaved")
        yield  # pragma: no cover — makes this an async generator

    with patch("agent.adk_agent.run_chat_stream", _boom), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"},
                        headers={"Accept": "text/event-stream"})

    # Status already committed to 200 before the failure → error frame.
    assert r.status_code == 200
    frames = _parse_sse(r.text)
    err = [d for ev, d in frames if ev == "error"]
    assert err and err[0]["status_hint"] == 502
    assert "model misbehaved" in err[0]["detail"]
