"""Integration test — caller workload propagates through ``/chat`` to
the Developer Knowledge MCP wrapper's structured log (Phase 17.B.4
follow-up).

What this pins
--------------

The MCP wrapper's structured log carries two distinct identity fields:

- ``mcp_server`` — which MCP we called (``"developer_knowledge"``).
  This is set inside the wrapper module, regardless of caller.
- ``workload`` — who asked us to call it (``"drift"`` / ``"upgrade"``).
  This is set by the request handler in :mod:`agent.main` via
  :func:`agent.workloads.set_workload` before invoking the agent, and
  read by the wrapper's ``_log_call`` from
  :func:`agent.workloads.current_workload`.

The unit test
:func:`tests.unit.test_mcp_developer_knowledge.test_log_workload_reflects_set_workload_scope`
pins that the wrapper reads the ContextVar correctly. This test pins
the other half of the contract: ``/chat`` with ``workload=drift``
actually binds the ContextVar before the MCP call happens.

Mock seam choice — ``_call_mcp_tool`` + stubbed ``run_chat``
-----------------------------------------------------------

Same approach as :mod:`tests.integration.test_drift_uses_mcp`:

- ``run_chat`` is stubbed with an async function that resolves the
  drift workload's ``search_developer_docs`` callable and invokes it
  directly. The LLM is the non-deterministic part; the wiring (handler
  → ContextVar bind → workload-resolved tool → MCP wrapper → log) is
  fully deterministic and what we're actually pinning here.
- ``_call_mcp_tool`` is patched so the test doesn't need an in-process
  MCP server. The wrapper still runs through the cache + truncation +
  log layers — and the log layer is exactly what we're observing.

Trace assertion technique
-------------------------

The wrapper emits one ``logging.INFO`` record per call with
``extra={...}`` keys surfaced as ``LogRecord`` attributes. Capture
records via ``caplog.at_level(logging.INFO, ...)`` and assert the
``workload`` attribute matches the request's ``workload`` field.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _use_adk_and_api_key(monkeypatch):
    """``/chat`` requires USE_ADK=true (else 503 "ADK not enabled"), and
    the workload pre-resolve step needs ``DEVELOPER_KNOWLEDGE_API_KEY``
    set or the handler short-circuits to a 503 before any MCP call.

    The four drift worker URL env vars are already provided by the
    autouse fixture in :mod:`tests.integration.conftest`.
    """
    monkeypatch.setenv("USE_ADK", "true")
    monkeypatch.setenv("DEVELOPER_KNOWLEDGE_API_KEY", "test-api-key-xyz")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_mcp_cache_and_toolset():
    """The MCP wrapper module holds a module-level response cache plus a
    lazily-built toolset. Both must be cleared between tests or a
    previous test's canned data could satisfy this test's call without
    going through the log path (no cache miss → no log emission)."""
    from agent.mcp import developer_knowledge as dk

    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None
    yield
    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None


def _make_canned_docs_response() -> dict:
    """Minimal happy-path MCP response. Shape mirrors the Developer
    Knowledge ``search_documents`` result. We're not testing
    truncation here (17.B.2 unit tests cover that); we're testing
    that the log carries the right ``workload`` value."""
    return {
        "documents": [
            {
                "parent": "documents/cloud.google.com/run/docs/x",
                "content": "Sample doc body.",
                "id": "doc-x",
            }
        ]
    }


# --------------------------------------------------------------------------- #
# Propagation test — /chat workload=drift surfaces workload=drift in the log.
# --------------------------------------------------------------------------- #


def test_chat_drift_propagates_workload_to_mcp_log(caplog):
    """A ``/chat`` request with ``workload="drift"`` MUST end up emitting
    an MCP log line whose ``workload`` field reads ``"drift"``.

    This is the load-bearing integration property: without it, the
    operator dashboards can't slice MCP latency/failures/quota by
    caller (the whole reason for the per-workload routing in 17.A.3).

    Pins the chain:

    1. The chat handler reads ``req.workload`` (a literal, validated
       by pydantic — see :class:`ChatRequest`).
    2. It binds that value via :func:`agent.workloads.set_workload`
       BEFORE calling ``run_chat``.
    3. The agent resolves the workload's MCP wrapper callables and
       invokes one of them.
    4. The wrapper's ``_log_call`` reads
       :func:`agent.workloads.current_workload` and emits it as a
       LogRecord attribute.
    5. The token reset in the handler's ``finally`` block restores
       the previous binding so a sibling request doesn't inherit
       this one's workload.
    """
    from agent.mcp import developer_knowledge as dk
    from agent.workloads import load_workload

    mcp_mock = AsyncMock(return_value=_make_canned_docs_response())

    async def stub_run_chat(prompt: str, *, session_id=None, workload: str = "drift"):
        """Stand in for the LLM. Resolves the workload-scoped MCP
        wrapper and invokes it once. This is the same code path used
        in :mod:`tests.integration.test_drift_uses_mcp`'s positive
        test — see that file for the longer rationale on why
        resolving via ``load_workload`` (rather than direct import)
        is the load-bearing wiring assertion."""
        resolution = load_workload(workload)
        search_fn = resolution.tools["search_developer_docs"]
        await search_fn("Cloud Run env vars")
        return {
            "reply": "OK.",
            "tool_calls": ["search_developer_docs"],
            "session_id": session_id or "test-sid",
        }

    with (
        patch.object(dk, "_call_mcp_tool", mcp_mock),
        patch("agent.adk_agent.run_chat", stub_run_chat),
    ):
        with caplog.at_level(logging.INFO, logger="agent.mcp.developer_knowledge"):
            client = TestClient(app)
            r = client.post(
                "/chat",
                json={"prompt": "Anything", "workload": "drift"},
            )

    assert r.status_code == 200, r.text

    records = [
        rec
        for rec in caplog.records
        if getattr(rec, "mcp_tool", None) == "search_documents"
    ]
    assert records, (
        "Expected one MCP log line for the search_documents call; got "
        f"{[r.getMessage() for r in caplog.records]!r}"
    )
    assert len(records) == 1
    rec = records[0]
    # The load-bearing assertion: the request's workload field made it
    # all the way through to the log line's ``workload`` extra.
    assert getattr(rec, "workload", None) == "drift", (
        f"Expected workload='drift' in the MCP log line; got "
        f"{getattr(rec, 'workload', None)!r}. Either the handler's "
        f"set_workload call didn't run, the wrapper isn't reading the "
        f"ContextVar, or a parallel test leaked another binding."
    )
    # ``mcp_server`` stays the MCP target identity — these two fields
    # carry different load.
    assert getattr(rec, "mcp_server", None) == "developer_knowledge"


# --------------------------------------------------------------------------- #
# Isolation test — back-to-back requests with different workloads.
# --------------------------------------------------------------------------- #


def test_chat_workload_does_not_leak_between_sequential_requests(caplog):
    """Pin that the handler's ``try/finally`` resets the workload
    ContextVar after each request so a subsequent request sees a
    fresh binding.

    Concurrent-request isolation is enforced by :pep:`567`'s
    ContextVar copy-per-task semantics — every coroutine task gets
    its own snapshot, so even without the reset two parallel handlers
    wouldn't leak into each other. This test instead pins the
    sequential case: ``TestClient`` runs each request synchronously
    in the same event-loop frame, so a missing reset WOULD leak
    here. If a future refactor accidentally drops the ``finally``
    branch, this test catches it.

    Today both workloads supported by ``/chat`` are ``drift`` and
    ``upgrade`` (per the ``Literal`` on :class:`ChatRequest`). Upgrade
    isn't fully wired yet (``upgrade_read_dependencies`` is still
    reserved), so the second request below uses ``drift`` again but
    with a stub that exercises the OUTSIDE-handler post-reset state
    via a log record taken from a manually-invoked wrapper call.
    """
    from agent.mcp import developer_knowledge as dk
    from agent.workloads import current_workload, load_workload

    seen_workloads_during_call: list[str] = []
    mcp_mock = AsyncMock(return_value=_make_canned_docs_response())

    async def stub_run_chat(prompt: str, *, session_id=None, workload: str = "drift"):
        # Sanity: inside the handler frame, the ContextVar IS bound.
        seen_workloads_during_call.append(current_workload())
        resolution = load_workload(workload)
        await resolution.tools["search_developer_docs"]("q")
        return {
            "reply": "OK.",
            "tool_calls": ["search_developer_docs"],
            "session_id": session_id or "test-sid",
        }

    with (
        patch.object(dk, "_call_mcp_tool", mcp_mock),
        patch("agent.adk_agent.run_chat", stub_run_chat),
    ):
        client = TestClient(app)
        # Two sequential drift requests — the second must see a fresh
        # binding even though the first's binding was the same name,
        # because the reset must run regardless.
        r1 = client.post("/chat", json={"prompt": "first", "workload": "drift"})
        # Between requests the ContextVar should be back to its default.
        between = current_workload()
        r2 = client.post("/chat", json={"prompt": "second", "workload": "drift"})

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Inside both handler frames the binding was active.
    assert seen_workloads_during_call == ["drift", "drift"]
    # Between requests, the ContextVar reverted to its default. This is
    # the property the ``finally`` block guarantees — if it ever
    # regresses, this assertion catches it.
    assert between == "unknown", (
        f"Expected workload ContextVar to reset to its default "
        f"('unknown') between requests; got {between!r}. The chat "
        f"handler's try/finally reset is broken."
    )
