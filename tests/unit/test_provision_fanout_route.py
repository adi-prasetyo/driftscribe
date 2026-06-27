"""Contract for /chat routing of the ``provision`` workload (Phase D5-7).

D5-7 wires ``/chat?workload=provision`` — both the JSON drain and the SSE
stream — through the parallel fan-out orchestrator
:func:`agent.fanout.run_provision_fanout_stream` instead of the single-agent
:func:`agent.adk_agent.run_chat_stream`. The orchestrator yields the SAME
``{"type":"event"|"result"}`` item shapes as ``run_chat_stream`` (it even
internally delegates back to it for a 1-slice/coupled change), so all
downstream framing — the SSE ``meta``/event/``done`` frames and the JSON
drain — stays workload-agnostic.

These tests pin the ROUTING decision only (which generator a workload's
``/chat`` selects). The fan-out engine's own behaviour (decompose, parallel
authoring, single-slice fallback, one editor call) is covered by the D5-1..6
suites and is intentionally NOT re-exercised here — every orchestrator call is
replaced by a trivial stub async-generator.

Two byte-compat guards live alongside this file:

- The drift JSON/SSE cases below are the *positive* regression guard: a
  non-provision workload must still flow through ``run_chat`` (JSON) /
  ``run_chat_stream`` (SSE), and the orchestrator must NOT be touched.
- ``tests/unit/test_chat_sse.py`` is the unchanged byte-compat guard for the
  existing drift/explore paths; D5-7 must leave it fully green.

The ``/recheck?workload=provision`` → 503 chat-only refusal is intentionally
NOT duplicated here: it is already pinned by
``tests/unit/test_provision_workload.py::test_recheck_provision_workload_is_route_refused``.
D5-7 does not touch that guard.

Harness mirrors ``tests/unit/test_chat_sse.py`` exactly (auth is neutralized
via ``app.dependency_overrides`` — patching the module-level ``verify_token``
does not work because it's captured in the route's ``Depends(...)`` at
decoration time). ``load_workload`` + ``_eager_resolve_upgrade_contract`` are
patched so no real workload resolves.

The orchestrator is imported LAZILY by ``agent.main._chat_stream`` as
``from agent.fanout import run_provision_fanout_stream`` (to dodge the
``agent.fanout`` → ``agent.adk_agent`` import cycle), so it must be patched at
its SOURCE module ``agent.fanout`` rather than as an ``agent.main`` attribute.
"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from agent import main as agent_main
from agent.auth import verify_token


def _parse_sse(text: str):
    """Return list of (event_name|None, data_dict|None) from an SSE body.

    Copied verbatim from ``tests/unit/test_chat_sse.py`` so the two route
    suites parse identical frame shapes the same way.
    """
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


async def _stub_fanout(prompt, session_id=None, *, autonomy_mode="propose_apply", prior_turns=None):
    """Stub orchestrator: provision-only, so NO ``workload`` kwarg.

    Matches :func:`agent.fanout.run_provision_fanout_stream`'s
    ``(prompt, session_id=None)`` signature and yields the canonical
    event-then-result item shapes.
    """
    yield {"type": "event", "event": {
        "event": "tool_call", "tool_name": "provision_open_infra_pr",
        "tool_args": {}, "seq": 1, "insert_id": "fanout-1", "timestamp": "t"}}
    yield {"type": "result", "reply": "opened PR #99",
           "tool_calls": ["open_infra_pr"], "session_id": "sid-prov"}


async def _stub_chat(prompt, session_id=None, *, workload="drift", autonomy_mode="propose_apply", prior_turns=None):
    """Stub single-agent stream: keeps the ``workload`` kwarg ``run_chat_stream``
    actually carries, so a mis-route to it would still produce a usable stream
    (the tests assert on the *reply* text, not the shape, to distinguish)."""
    yield {"type": "event", "event": {
        "event": "tool_call", "tool_name": "read_drift", "tool_args": {},
        "seq": 1, "insert_id": "chat-1", "timestamp": "t"}}
    yield {"type": "result", "reply": "single-agent reply",
           "tool_calls": ["read_drift"], "session_id": "sid-chat"}


# --------------------------------------------------------------------------- #
# 1. provision JSON drains the orchestrator's result item.
# --------------------------------------------------------------------------- #
def test_chat_provision_json_drains_fanout_orchestrator(_adk_enabled):
    with patch("agent.fanout.run_provision_fanout_stream", _stub_fanout), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "two iac files",
                                       "workload": "provision"})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    # Drained from the orchestrator stub's ``result`` item — proves the JSON
    # provision path runs the fan-out generator, not run_chat / run_chat_stream.
    assert body["reply"] == "opened PR #99"
    assert body["tool_calls"] == ["open_infra_pr"]
    assert body["session_id"] == "sid-prov"


# --------------------------------------------------------------------------- #
# 2. provision SSE frames the orchestrator's event + result.
# --------------------------------------------------------------------------- #
def test_chat_provision_sse_streams_fanout_orchestrator(_adk_enabled):
    with patch("agent.fanout.run_provision_fanout_stream", _stub_fanout), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "two iac files",
                                       "workload": "provision"},
                        headers={"Accept": "text/event-stream"})

    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers["content-type"]
    frames = _parse_sse(r.text)
    # meta frame with trace_id (unchanged framing).
    assert frames[0][0] == "meta" and "trace_id" in frames[0][1]
    # the orchestrator's event flows through as a default (unnamed) frame.
    assert any(ev is None and d.get("event") == "tool_call"
               and d.get("tool_name") == "provision_open_infra_pr"
               for ev, d in frames)
    # terminal done frame carries the orchestrator's reply/tool_calls/session.
    done = [d for ev, d in frames if ev == "done"]
    assert done, "expected a terminal done frame"
    assert done[0]["reply"] == "opened PR #99"
    assert done[0]["tool_calls"] == ["open_infra_pr"]
    assert done[0]["session_id"] == "sid-prov"


# --------------------------------------------------------------------------- #
# 3. provision routes to the orchestrator, NEVER to run_chat_stream.
# --------------------------------------------------------------------------- #
def test_chat_provision_does_not_invoke_run_chat_stream(_adk_enabled):
    """provision must select the fan-out orchestrator, not the single-agent
    stream. We patch run_chat_stream with a recording stub that BLOWS UP if it
    is ever called for provision (it must not be)."""
    chat_stream_called = Mock()

    def _forbidden_chat_stream(prompt, session_id=None, *, workload="drift", autonomy_mode="propose_apply", prior_turns=None):
        chat_stream_called(workload)
        raise AssertionError(
            "run_chat_stream must NOT be invoked for workload=provision; "
            "the fan-out orchestrator owns provision"
        )

    with patch("agent.fanout.run_provision_fanout_stream", _stub_fanout), \
         patch("agent.adk_agent.run_chat_stream", _forbidden_chat_stream), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "two iac files",
                                       "workload": "provision"})

    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "opened PR #99"
    chat_stream_called.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. REGRESSION: drift (and explore) are byte-unchanged — orchestrator untouched.
# --------------------------------------------------------------------------- #
def test_chat_drift_json_still_goes_through_run_chat_not_fanout(_adk_enabled):
    """The non-provision JSON path must still call ``run_chat`` (the existing
    drain) and must NEVER touch the fan-out orchestrator. An existing test
    (``test_chat_sse.py::test_chat_returns_json_without_accept_header``) patches
    ``agent.adk_agent.run_chat``; this guard pins that drift keeps using it."""
    run_chat_called = Mock()
    fanout_called = Mock()

    async def _run_chat(prompt, session_id=None, *, workload="drift", autonomy_mode="propose_apply", prior_turns=None):
        run_chat_called(workload)
        return {"reply": "drift reply", "tool_calls": [], "session_id": "sid-d"}

    async def _spy_fanout(prompt, session_id=None, *, autonomy_mode="propose_apply", prior_turns=None):
        fanout_called()
        yield {"type": "result", "reply": "x", "tool_calls": [],
               "session_id": "s"}

    with patch("agent.adk_agent.run_chat", _run_chat), \
         patch("agent.fanout.run_provision_fanout_stream", _spy_fanout), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"})

    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "drift reply"
    run_chat_called.assert_called_once_with("drift")
    fanout_called.assert_not_called()


def test_chat_drift_sse_still_goes_through_run_chat_stream_not_fanout(
    _adk_enabled,
):
    """The non-provision SSE path must still stream ``run_chat_stream`` and
    never the fan-out orchestrator (the SSE byte-compat regression guard)."""
    fanout_called = Mock()

    async def _spy_fanout(prompt, session_id=None, *, autonomy_mode="propose_apply", prior_turns=None):
        fanout_called()
        yield {"type": "result", "reply": "x", "tool_calls": [],
               "session_id": "s"}

    with patch("agent.adk_agent.run_chat_stream", _stub_chat), \
         patch("agent.fanout.run_provision_fanout_stream", _spy_fanout), \
         patch.object(agent_main, "load_workload"), \
         patch.object(agent_main, "_eager_resolve_upgrade_contract"):
        client = TestClient(agent_main.app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "drift"},
                        headers={"Accept": "text/event-stream"})

    assert r.status_code == 200, r.text
    frames = _parse_sse(r.text)
    done = [d for ev, d in frames if ev == "done"]
    assert done and done[0]["reply"] == "single-agent reply"
    fanout_called.assert_not_called()
