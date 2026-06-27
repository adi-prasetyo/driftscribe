"""Integration test — the drift workload actually invokes the Developer
Knowledge MCP during agent reasoning (Phase 17.B.4).

What this pins
--------------

Earlier sub-tasks landed the pieces in isolation:

- 17.B.1 — Secret Manager binding for ``DEVELOPER_KNOWLEDGE_API_KEY`` is
  wired into deploy infra.
- 17.B.2 — :mod:`agent.mcp.developer_knowledge` exports
  :func:`search_developer_docs` and :func:`retrieve_developer_doc` async
  wrappers with cache + timeout + truncation + structured logging.
- 17.B.3 — the drift workload's ``enabled_tool_names`` includes both MCP
  tools; ``COORDINATOR_TOOLS`` exposes the wrappers; the agent factory
  passes ``list(workload.tools.values())`` so the LLM actually sees them;
  the drift ``system_prompt.md`` instructs the agent to call
  :func:`search_developer_docs` BEFORE proposing a docs_pr.

This task verifies the END-TO-END path: a drift ``/chat`` request →
agent reasoning → MCP tool invocation before docs delegation. AND the
negative: a ``no_op`` decision does NOT spend an MCP call (latency
optimization — there's nothing to ground a citation against when
nothing changed).

Mock seam choice — (b) ``_call_mcp_tool``
-----------------------------------------

The 17.B.4 plan lists three seams:

  (a) Mock ``search_developer_docs`` / ``retrieve_developer_doc``
      directly — easiest, but bypasses the ADK toolset wiring entirely.
      Doesn't actually exercise the wrapper's cache/timeout/log path,
      and doesn't go through the workload-resolved callable.
  (b) Mock ``_call_mcp_tool`` — the single seam already used by the
      17.B.2 unit tests. Goes through the real toolset wiring (the
      wrappers, the cache, the structured log) WITHOUT requiring an
      in-process MCP server. Each test only has to seed the canned
      response and assert the call happened.
  (c) Mock the MCP session manager (``MCPSessionManager.create_session``)
      so the dispatch path includes the real ``CallToolResult``
      ``structuredContent`` parse. Closest to a real wire-format test.

We picked (b) for the two main tests — positive (docs_pr triggers
MCP) and negative (no_op skips MCP). It exercises the bulk of the
wrapper code (cache, log, truncation, fail-closed translation) while
keeping the assertion surface clean.

We added ONE (c)-style test as the third assertion (Codex 17.B.2 review
follow-up: "an end-to-end smoke against an in-process MCP server would
validate the real wire-format parse"). It uses the same fake session
manager pattern as
:func:`test_mcp_developer_knowledge.test_call_mcp_tool_invokes_session_call_tool_and_returns_structured`,
but drives it through the ``/chat`` endpoint so the workload resolution
+ wrapper + dispatch path are all exercised in one shot.

LLM-stub strategy
-----------------

We do NOT run a real Gemini call (cost, flakiness, slow CI). Instead,
each test patches ``agent.adk_agent.run_chat`` with an async stub that
directly invokes the workload-resolved tools in the order an LLM SHOULD
pick them per the drift ``system_prompt.md``:

- positive test → call ``search_developer_docs("...")`` first, then
  ``patch_docs_tool(...)``. Asserts the recorded call timeline shows
  MCP-before-docs.
- negative test → produce a ``no_op`` reply without ever invoking MCP.
  Asserts ``_call_mcp_tool`` was never called.

This is the canonical ADK-integration-test pattern: the LLM is the
non-deterministic part, but the WIRING (workload → tool → MCP/worker)
is fully deterministic and what we're actually pinning here. The
LLM-prompt-adherence half (does Gemini actually choose this order?)
lives in the byte-for-byte system-prompt goldens pinned in
:mod:`tests/unit/test_drift_workload_loads`. The two halves compose:

- "the prompt asks for MCP-before-docs" — pinned by the goldens.
- "if the LLM follows the prompt, the wiring delivers MCP-before-docs"
  — pinned here.

If a future change adds a step between MCP and docs (e.g., a
cache-hit fast path that skips the docs delegation), it'll surface
in either this test (the timeline shape changes) or in the goldens
(the prompt's "before" wording changes), not silently.

Trace assertion technique
-------------------------

Both the MCP wrapper and the worker call go through clean seams:
``_call_mcp_tool`` is the bottom of the MCP wrapper, and
``agent.adk_tools.worker_client.call`` is what every worker-delegating
tool calls. We patch each with a ``MagicMock``-recording wrapper that
appends to a shared timeline list, then assert ordering by index. No
timestamps needed — Python's GIL guarantees the recorded order is the
invocation order.
"""
from __future__ import annotations

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
    previous test's canned data could satisfy this test's call (false
    pass: the assertion would see "MCP returned docs" but the recorded
    call timeline would be empty)."""
    from agent.mcp import developer_knowledge as dk

    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None
    yield
    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None


def _make_canned_docs_response() -> dict:
    """A minimal happy-path response shape matching the Developer
    Knowledge MCP's ``search_documents`` result. The truncation step in
    the wrapper is a no-op for this small payload — we're testing
    invocation ordering, not body shape (the 17.B.2 unit tests pin
    truncation)."""
    return {
        "documents": [
            {
                "parent": (
                    "documents/cloud.google.com/run/docs/configuring/"
                    "environment-variables"
                ),
                "content": (
                    "Setting environment variables for Cloud Run "
                    "services. Use gcloud run deploy --set-env-vars..."
                ),
                "id": "env-vars-doc",
            }
        ]
    }


# --------------------------------------------------------------------------- #
# Positive test — docs_pr decision triggers MCP-before-docs-delegation.
# --------------------------------------------------------------------------- #


def test_drift_chat_docs_pr_calls_mcp_before_delegating_to_docs(monkeypatch):
    """A ``/chat`` request whose reasoning ends in a docs_pr action
    MUST first call ``search_developer_docs`` (to ground the citation),
    THEN call the Docs Worker.

    Drives the workload's real tool callables — the LLM is stubbed but
    everything below the LLM (workload resolution → tool callable →
    wrapper → ``_call_mcp_tool`` for MCP, → ``worker_client.call`` for
    docs) is exercised for real.

    Timeline pin: ``_call_mcp_tool("search_documents", ...)`` appears
    in the call timeline BEFORE
    ``worker_client.call("docs", ...)``. The assertion compares list
    indices, which is sound because Python's GIL serializes the
    recorded ``append`` calls.
    """
    from agent.mcp import developer_knowledge as dk
    from agent.workloads import load_workload

    timeline: list[tuple[str, object]] = []

    async def fake_call_mcp_tool(tool_name: str, payload: dict) -> dict:
        timeline.append(("mcp", (tool_name, payload)))
        return _make_canned_docs_response()

    def fake_worker_call(worker: str, payload: dict, *args, **kwargs) -> dict:
        timeline.append(("worker", (worker, payload)))
        # Docs Worker /patch response shape — the LLM's tool wrapper
        # returns this dict to the LLM. Real shape (validated by Phase
        # 11.7 worker tests) is ``{"pr_url": ..., "dry_run": ...}``.
        return {
            "pr_url": "https://github.com/x/y/pull/42",
            "dry_run": True,
            "branch": "driftscribe/payment-mode-1234-ab",
        }

    async def stub_run_chat(prompt: str, *, session_id=None, workload: str = "drift", autonomy_mode: str = "propose_apply", prior_turns=None):
        """Stand in for the LLM. Resolves the workload, then calls the
        workload-scoped tools in the order the drift system_prompt.md
        instructs: search_developer_docs FIRST (to ground the docs PR
        citation), patch_docs_tool SECOND.

        The workload resolution is the same code path ``run_chat`` uses
        in prod (``load_workload(workload)``), so the resolved callables
        ARE the ones the agent factory would hand to the LLM via
        ``Agent(tools=list(workload.tools.values()))``. That's the
        load-bearing property the integration test pins.
        """
        resolution = load_workload(workload)
        # Resolve the canonical tool callables via the workload map —
        # NOT via direct import. This is what verifies the wiring: if
        # 17.B.3 had wired the wrong callable (or 17.A.4 had filtered
        # the MCP tool out of drift's enabled set), this call would
        # fail or hit the wrong function and the timeline assertion
        # below would surface it.
        search_fn = resolution.tools["search_developer_docs"]
        patch_docs_fn = resolution.tools["drift_patch_docs"]
        await search_fn("Cloud Run environment variables")
        # patch_docs_tool is a sync function; call directly.
        patch_docs_fn(
            file_path="demo/docs/runbook.md",
            new_content="updated body",
            title="docs(driftscribe): update PAYMENT_MODE",
            body="See https://cloud.google.com/run/docs/configuring/environment-variables",
        )
        return {
            "reply": "Opened docs PR with MCP-grounded citation.",
            "tool_calls": ["search_developer_docs", "drift_patch_docs"],
            "session_id": session_id or "test-sid",
        }

    with (
        patch.object(dk, "_call_mcp_tool", fake_call_mcp_tool),
        patch("agent.adk_tools.worker_client.call", side_effect=fake_worker_call),
        patch("agent.adk_agent.run_chat", stub_run_chat),
    ):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "PAYMENT_MODE drifted; update docs.", "workload": "drift"},
        )

    assert r.status_code == 200, r.text

    # Ordering pin: MCP search happened, then docs worker call. Use
    # the timeline indices for an unambiguous assertion.
    kinds = [entry[0] for entry in timeline]
    assert "mcp" in kinds, (
        f"Expected an MCP call in the timeline; got {kinds!r}. The drift "
        f"system_prompt.md instructs the agent to call "
        f"search_developer_docs before proposing a docs_pr."
    )
    assert "worker" in kinds, (
        f"Expected a docs-worker call in the timeline; got {kinds!r}."
    )
    mcp_idx = kinds.index("mcp")
    # Find the docs worker call specifically (filter by payload).
    docs_idxs = [
        i
        for i, entry in enumerate(timeline)
        if entry[0] == "worker" and entry[1][0] == "docs"
    ]
    assert docs_idxs, (
        f"Expected a worker call to 'docs'; timeline payloads: "
        f"{[e[1] for e in timeline if e[0] == 'worker']!r}"
    )
    assert mcp_idx < docs_idxs[0], (
        f"MCP search_documents must precede docs worker call; "
        f"timeline={timeline!r}"
    )

    # Pin which MCP tool was called. The drift system prompt names
    # search_developer_docs explicitly — that translates to the
    # ``search_documents`` MCP tool name (vs ``get_documents`` for
    # retrieve_developer_doc).
    mcp_entry = timeline[mcp_idx]
    assert mcp_entry[1][0] == "search_documents", (
        f"Expected MCP tool 'search_documents' (the search wrapper); "
        f"got {mcp_entry[1][0]!r}"
    )


# --------------------------------------------------------------------------- #
# Negative test — no_op decision must NOT spend an MCP call.
# --------------------------------------------------------------------------- #


def test_drift_chat_no_op_does_not_call_mcp(monkeypatch):
    """A ``/chat`` turn that ends in a ``no_op`` decision MUST NOT
    invoke the Developer Knowledge MCP.

    Rationale: latency + cost optimization. There's nothing to ground a
    citation against when nothing has changed — calling MCP would just
    burn the per-request budget for no observable benefit. The system
    prompt only directs MCP calls when proposing a docs_pr.

    The negative is enforceable here because every MCP call goes
    through the single ``_call_mcp_tool`` seam — patching it with a
    spy lets us assert ``call_count == 0`` at the end of the request.
    """
    from agent.mcp import developer_knowledge as dk

    mcp_spy = AsyncMock(return_value=_make_canned_docs_response())

    async def stub_run_chat(prompt: str, *, session_id=None, workload: str = "drift", autonomy_mode: str = "propose_apply", prior_turns=None):
        """Stand in for the LLM on the no_op path. Does NOT invoke any
        MCP wrapper — matches the system prompt's instruction (only
        call search_developer_docs WHEN proposing a docs_pr)."""
        # Important: load_workload still runs (the /chat handler's
        # pre-resolve already did it before this stub is reached), but
        # we deliberately don't call any of the workload's tools — the
        # LLM would just emit a natural-language "nothing changed"
        # response and return.
        return {
            "reply": "Live env matches the contract; nothing to do.",
            "tool_calls": [],
            "session_id": session_id or "test-sid",
        }

    with (
        patch.object(dk, "_call_mcp_tool", mcp_spy),
        patch("agent.adk_agent.run_chat", stub_run_chat),
    ):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "What's the live state?", "workload": "drift"},
        )

    assert r.status_code == 200, r.text
    mcp_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# Stretch (c)-style — wire-format parse through the real session manager.
# --------------------------------------------------------------------------- #


def test_drift_chat_docs_pr_exercises_real_mcp_wire_format(monkeypatch):
    """Stretch: exercise the real ``_call_mcp_tool`` dispatch path
    (rather than mocking it at the function seam). Stubs only the
    :class:`MCPSessionManager`'s ``create_session()`` so the test
    doesn't open a real network connection — but the rest of the
    dispatch (session.call_tool → CallToolResult → structuredContent
    parse → ``_wrap_mcp_call`` truncation + log) runs for real.

    Mirrors the unit-test pattern in
    :func:`test_mcp_developer_knowledge.test_call_mcp_tool_invokes_session_call_tool_and_returns_structured`
    but drives it through ``/chat`` so the workload resolution +
    wrapper + dispatch are all chained together in one assertion.

    Codex 17.B.2 follow-up: this is the "in-process MCP server smoke
    test" the reviewer flagged as missing — the (b)-seam tests above
    don't exercise the ``CallToolResult.structuredContent`` → dict
    parse, which is the most likely place a future ADK/MCP SDK
    version bump would silently break.
    """
    from agent.mcp import developer_knowledge as dk
    from agent.workloads import load_workload

    # Build a fake CallToolResult matching the shape ``_call_mcp_tool``
    # destructures: ``isError``, ``structuredContent`` (dict), ``content``.
    fake_result = type(
        "FakeCallToolResult",
        (),
        {
            "isError": False,
            "structuredContent": _make_canned_docs_response(),
            "content": [],
        },
    )()

    fake_session = AsyncMock()
    fake_session.call_tool = AsyncMock(return_value=fake_result)

    fake_session_mgr = type("FakeSessionMgr", (), {})()
    fake_session_mgr.create_session = AsyncMock(return_value=fake_session)

    # Build the real toolset (which reads DEVELOPER_KNOWLEDGE_API_KEY
    # from env — provided by the autouse fixture above) then graft on
    # the fake session manager. Pre-set _TOOLSET so the lazy build in
    # _get_toolset short-circuits to ours.
    from agent.mcp.developer_knowledge import build_developer_knowledge_toolset

    toolset = build_developer_knowledge_toolset()
    toolset._mcp_session_manager = fake_session_mgr  # noqa: SLF001
    dk._TOOLSET = toolset

    docs_calls: list[tuple[str, dict]] = []

    def fake_worker_call(worker: str, payload: dict, *args, **kwargs) -> dict:
        docs_calls.append((worker, payload))
        return {
            "pr_url": "https://github.com/x/y/pull/99",
            "dry_run": True,
            "branch": "driftscribe/runbook-1234-cd",
        }

    async def stub_run_chat(prompt: str, *, session_id=None, workload: str = "drift", autonomy_mode: str = "propose_apply", prior_turns=None):
        """Same canonical sequence as the (b)-seam positive test —
        search_developer_docs first, then patch_docs_tool. The
        difference is that search here actually runs through the real
        ``_call_mcp_tool`` and its CallToolResult parse."""
        resolution = load_workload(workload)
        search_fn = resolution.tools["search_developer_docs"]
        patch_docs_fn = resolution.tools["drift_patch_docs"]
        search_out = await search_fn("Cloud Run env vars")
        # Sanity: the wrapper's truncation step preserved the documents
        # list from the canned structuredContent. If the
        # CallToolResult-shape destructuring in _call_mcp_tool ever
        # regressed (e.g., reaches for ``.result`` instead of
        # ``.structuredContent``), this would catch it.
        assert isinstance(search_out, dict)
        assert search_out.get("documents"), (
            f"Expected the canned documents to flow through the real "
            f"_call_mcp_tool parse; got {search_out!r}"
        )
        patch_docs_fn(
            file_path="demo/docs/runbook.md",
            new_content="updated body",
            title="docs(driftscribe): update PAYMENT_MODE",
            body="See https://cloud.google.com/run/docs/configuring/environment-variables",
        )
        return {
            "reply": "Opened docs PR with MCP-grounded citation.",
            "tool_calls": ["search_developer_docs", "drift_patch_docs"],
            "session_id": session_id or "test-sid",
        }

    with (
        patch("agent.adk_tools.worker_client.call", side_effect=fake_worker_call),
        patch("agent.adk_agent.run_chat", stub_run_chat),
    ):
        client = TestClient(app)
        r = client.post(
            "/chat",
            json={"prompt": "Update docs for PAYMENT_MODE.", "workload": "drift"},
        )

    assert r.status_code == 200, r.text

    # Pin that the real session.call_tool path was taken with the
    # expected arguments — the wire-format invariant.
    fake_session.call_tool.assert_awaited_once_with(
        "search_documents", {"query": "Cloud Run env vars"}
    )

    # Pin that the docs worker was called after the MCP dispatch.
    # asserting docs_calls non-empty is enough: the stub_run_chat
    # body awaits search_fn() before invoking patch_docs_fn(), so
    # docs_calls is only populated AFTER the MCP path returned.
    assert docs_calls, "Expected the docs worker to be called after the MCP dispatch"
    assert docs_calls[0][0] == "docs"
