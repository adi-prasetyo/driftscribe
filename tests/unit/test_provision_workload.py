"""End-to-end wiring test for the provision workload (Phase D2-4).

The provision workload is chat-only (like explore) but, unlike explore,
it intentionally carries ONE mutation tool: ``provision_open_infra_pr``.
It authors validated iac/-only file writes and opens ONE PR via the
tofu-editor worker; it never touches live infra directly.

Pins three properties:

1. ``load_workload("provision")`` resolves end-to-end with the read +
   editor worker URL env vars set — its tool and worker names all resolve
   against the registries — and exposes ``provision_open_infra_pr`` plus
   the read tools.
2. ``/recheck?workload=provision`` is route-refused (chat-only) BEFORE
   any workload resolution — mirrors the explore /recheck refusal.
3. ``/chat?workload=provision`` builds an agent that exposes
   ``provision_open_infra_pr`` — mirrors the explore /chat exposure.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent.adk_agent import PROVISION_WORKLOAD_TOOL_NAMES
from agent.auth import verify_token
from agent.config import get_settings
from agent.main import app


@pytest.fixture
def _bypass_auth():
    """Bypass the /recheck + /chat token guard for the route-level tests.

    Mirrors the integration conftest's ``app.dependency_overrides[verify_token]``
    bypass — without it the route guard 503s ("DRIFTSCRIBE_TOKEN unset")
    before the chat-only refusal / workload routing under test is reached.
    The token guard itself is covered elsewhere (test_token_guard.py); these
    tests only care about workload routing, so the override is the right
    isolation. Popped on teardown so it doesn't leak into other tests.
    """
    app.dependency_overrides[verify_token] = lambda: None
    yield
    app.dependency_overrides.pop(verify_token, None)


@pytest.fixture
def provision_workload_env(monkeypatch):
    """Set the worker URL env vars the provision workload resolves at load
    time: ``drift_reader`` (READER_URL), ``infra_reader`` (INFRA_READER_URL),
    and ``tofu_editor`` (TOFU_EDITOR_URL). Mirrors the explore fixture's
    cache-clear discipline on setup and teardown."""
    monkeypatch.setenv("READER_URL", "https://reader.test")
    monkeypatch.setenv("INFRA_READER_URL", "https://infra-reader.test")
    monkeypatch.setenv("TOFU_EDITOR_URL", "https://tofu-editor.test")
    import agent.workloads.registry as registry_mod
    registry_mod._WORKLOAD_CACHE.clear()
    yield
    registry_mod._WORKLOAD_CACHE.clear()


def test_load_workload_provision_resolves_end_to_end(provision_workload_env):
    """``load_workload("provision")`` resolves with the read + editor worker
    env vars set, and exposes the IaC-authoring tool plus the read tools."""
    from agent.workloads import load_workload

    resolution = load_workload("provision")
    assert resolution.spec.name == "provision"
    assert resolution.spec.observation_kind == "none"
    assert resolution.spec.action_names == []
    # No contract / no chat_system_prompt_file (mirrors explore): chat prompt
    # falls back to system_prompt.
    assert resolution.contract_path is None
    assert resolution.chat_system_prompt == resolution.system_prompt

    tools = resolution.tools
    # The tool keys equal the manifest's enabled_tool_names, in order.
    assert tuple(tools.keys()) == PROVISION_WORKLOAD_TOOL_NAMES
    for fn in tools.values():
        assert callable(fn)
    # The mutation tool the provision workload introduces.
    assert "provision_open_infra_pr" in tools
    assert tools["provision_open_infra_pr"].__name__ == "open_infra_pr_tool"
    # The read tools resolve too.
    assert tools["drift_read_live_env"].__name__ == "read_live_env_tool"
    assert tools["read_project_inventory"].__name__ == "read_project_inventory_tool"
    assert tools["load_contract"].__name__ == "load_contract_tool"


def test_recheck_provision_workload_is_route_refused(_bypass_auth) -> None:
    """``POST /recheck`` with ``{"workload": "provision"}`` → 503, refused
    as chat-only BEFORE any workload resolution.

    Provision has no autonomous /recheck path (it authors IaC edits from
    /chat). Its refusal lives at the top of ``_do_recheck``, ahead of the
    workload pre-resolve — same shape as the explore refusal. We prove the
    ordering by patching ``load_workload`` and asserting it is never called.
    """
    with patch("agent.main.load_workload") as m_load:
        client = TestClient(app)
        r = client.post("/recheck", json={"workload": "provision"})

    assert r.status_code == 503, r.text
    detail = r.json()["detail"].lower()
    assert "provision" in detail
    assert "chat-only" in detail or "chat only" in detail
    assert "/chat" in detail
    m_load.assert_not_called()


def test_chat_provision_workload_agent_exposes_open_infra_pr_tool(
    monkeypatch, provision_workload_env, _bypass_auth,
) -> None:
    """``POST /chat`` with ``workload="provision"`` routes through the fan-out
    orchestrator and builds a chat agent that exposes the IaC-authoring tool
    ``provision_open_infra_pr`` (callable ``open_infra_pr_tool``).

    Phase D5-7 routes the provision ``/chat`` JSON path through the parallel
    fan-out orchestrator ``agent.fanout.run_provision_fanout_stream`` (drained
    to the same ``{reply, tool_calls, session_id}`` dict), NOT through
    ``run_chat`` — so we patch the orchestrator (at its source module, since
    ``agent.main._chat_stream`` imports it lazily) to a no-op stub async-gen
    rather than ``run_chat``. The route-level assertion is just that provision
    is accepted (200, not 422) and the stub's result drains through.

    The capability-bound proof — that the agent built for
    ``workload="provision"`` carries ``open_infra_pr_tool`` — is unchanged and
    remains the load-bearing check: the orchestrator's single-slice fallback
    delegates to ``run_chat_stream`` over exactly this ``build_chat_agent``
    output, so the tool exposure still gates provision's one mutation path.
    """
    monkeypatch.setenv("USE_ADK", "true")
    monkeypatch.setenv("DEVELOPER_KNOWLEDGE_API_KEY", "test-key")
    get_settings.cache_clear()

    async def _stub_fanout(prompt, session_id=None, *, autonomy_mode="propose_apply"):
        yield {"type": "result", "reply": "ok", "tool_calls": [],
               "session_id": "sid"}

    with patch("agent.fanout.run_provision_fanout_stream", _stub_fanout):
        client = TestClient(app)
        r = client.post("/chat", json={"prompt": "hi", "workload": "provision"})

    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "ok"

    # The capability-bound proof: the agent built for workload=provision
    # carries the IaC-authoring tool.
    from agent.adk_agent import build_chat_agent
    from agent.workloads import load_workload

    resolution = load_workload("provision")
    agent = build_chat_agent(resolution, autonomy_mode="propose_apply")
    tool_names = {getattr(t, "__name__", repr(t)) for t in agent.tools}
    assert "open_infra_pr_tool" in tool_names, (
        f"provision chat agent must expose open_infra_pr_tool; "
        f"got {sorted(tool_names)}"
    )
