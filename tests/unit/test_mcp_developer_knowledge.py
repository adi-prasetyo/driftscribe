"""Unit tests for ``agent.mcp.developer_knowledge`` (Phase 17.B.2).

The wrapper around the Developer Knowledge MCP server is the only place
the coordinator's LLM reaches outside the codebase for "what should this
env var be?"-style reference content. We pin these properties:

1. :func:`build_developer_knowledge_toolset` constructs an
   :class:`McpToolset` with the documented URL, the
   ``X-Goog-Api-Key`` header carrying the env-supplied key, the
   verified ``timeout=10.0`` on
   :class:`StreamableHTTPConnectionParams`, and a ``tool_filter``
   that excludes ``answer_query``.
2. Missing :envvar:`DEVELOPER_KNOWLEDGE_API_KEY` raises
   :class:`MissingDeveloperKnowledgeApiKeyError` at build time —
   fail-closed.
3. :func:`search_developer_docs` and :func:`retrieve_developer_doc`
   wrap the underlying MCP ``search_documents`` / ``get_documents``
   calls, applying:
     - max 5 documents / max 4000 chars per response
     - 60-second in-process cache keyed on ``(tool_name, key)``
     - 10-second wall-clock timeout (wrapper-level, separate from the
       SDK's connection timeout)
     - structured log every call with the shape
       ``{trace_id, mcp_server, mcp_tool, query_or_names, doc_count,
       latency_ms}``
     - fail-closed translation of timeouts to a structured tool result
       (so the agent sees a dict, never a raw exception)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.mcp import developer_knowledge as dk
from agent.mcp.developer_knowledge import (
    MissingDeveloperKnowledgeApiKeyError,
    build_developer_knowledge_toolset,
    retrieve_developer_doc,
    search_developer_docs,
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch):
    """Most tests assume the env var is present; the missing-key test
    overrides this with `monkeypatch.delenv`."""
    monkeypatch.setenv("DEVELOPER_KNOWLEDGE_API_KEY", "test-api-key-xyz")


@pytest.fixture(autouse=True)
def _clear_cache():
    """Tests rely on the cache being empty so a previous test's call
    doesn't satisfy this test's lookup; also drop the cached toolset
    so monkeypatched env vars are honored on the next build."""
    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None
    yield
    dk._RESPONSE_CACHE.clear()
    dk._TOOLSET = None


def _make_search_result(*, count: int = 3, content_len: int = 100):
    """Build a fake MCP ``search_documents`` response. Real shape per
    Google docs: each doc has ``parent``, ``content``, ``id`` keys."""
    docs = []
    for i in range(count):
        docs.append(
            {
                "parent": f"documents/cloud.google.com/sample-{i}",
                "content": ("x" * content_len) + f"-doc{i}",
                "id": f"doc-{i}",
            }
        )
    return {"documents": docs}


# --------------------------------------------------------------------------- #
# build_developer_knowledge_toolset()
# --------------------------------------------------------------------------- #


def test_build_toolset_uses_correct_url_and_header():
    toolset = build_developer_knowledge_toolset()
    params = toolset._connection_params  # noqa: SLF001 - inspecting wired shape
    assert params.url == "https://developerknowledge.googleapis.com/mcp"
    assert params.headers == {"X-Goog-Api-Key": "test-api-key-xyz"}


def test_build_toolset_pins_timeout_to_ten_seconds():
    """Codex 2026-05-20: ``timeout`` on ``StreamableHTTPConnectionParams``
    is the SDK connection timeout, default 5.0. We bump it to 10.0 to
    accommodate the Developer Knowledge backend's typical latency. A
    separate wall-clock timeout in the wrapper enforces the same 10s
    budget at the call boundary."""
    toolset = build_developer_knowledge_toolset()
    assert toolset._connection_params.timeout == 10.0  # noqa: SLF001


def test_build_toolset_filters_out_answer_query():
    """``answer_query`` is a Preview-status LLM-reasoning surface we
    deliberately skip — we want raw doc retrieval, not a second LLM."""
    toolset = build_developer_knowledge_toolset()
    assert toolset.tool_filter == ["search_documents", "get_documents"]
    assert "answer_query" not in toolset.tool_filter


def test_build_toolset_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("DEVELOPER_KNOWLEDGE_API_KEY", raising=False)
    with pytest.raises(MissingDeveloperKnowledgeApiKeyError, match="DEVELOPER_KNOWLEDGE_API_KEY"):
        build_developer_knowledge_toolset()


def test_build_toolset_empty_api_key_raises(monkeypatch):
    """Empty string is just as broken as unset — same fail-closed path."""
    monkeypatch.setenv("DEVELOPER_KNOWLEDGE_API_KEY", "")
    with pytest.raises(MissingDeveloperKnowledgeApiKeyError):
        build_developer_knowledge_toolset()


# --------------------------------------------------------------------------- #
# search_developer_docs(): happy path + shape
# --------------------------------------------------------------------------- #


def test_search_returns_structured_doc_refs():
    """Mock the underlying MCP call. The wrapper passes through the
    parent/content/id triple per doc."""
    mock_call = AsyncMock(return_value=_make_search_result(count=2, content_len=50))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        out = asyncio.run(search_developer_docs("how to set Cloud Run env vars"))

    assert "documents" in out
    assert len(out["documents"]) == 2
    for doc in out["documents"]:
        assert set(doc.keys()) >= {"parent", "content", "id"}


def test_search_truncates_to_five_documents():
    """If the MCP server returns 10 docs, the wrapper returns only 5
    (token-budget guardrail — the LLM context window doesn't need to
    absorb a flood)."""
    mock_call = AsyncMock(return_value=_make_search_result(count=10, content_len=50))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        out = asyncio.run(search_developer_docs("anything"))

    assert len(out["documents"]) == 5


def test_search_truncates_doc_content_to_four_thousand_chars():
    """A single doc body over 4000 chars is truncated and gets a
    visible suffix so the LLM knows the body was clipped."""
    mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=8000))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        out = asyncio.run(search_developer_docs("anything"))

    body = out["documents"][0]["content"]
    # 4000 truncated body + the suffix marker; total <= 4000 + suffix length
    assert "... [truncated" in body
    assert "8" in body  # the original length should appear in the marker
    # First 4000 chars preserved verbatim.
    assert body.startswith("x" * 1000)


# --------------------------------------------------------------------------- #
# retrieve_developer_doc(): happy path + shape
# --------------------------------------------------------------------------- #


def test_retrieve_wraps_single_name_into_list():
    """The MCP ``get_documents`` tool takes a list of names; our
    wrapper accepts one name and adapts the call so the LLM doesn't
    need to know the list shape."""
    captured: dict[str, Any] = {}

    async def fake_call(tool_name, payload, *_, **__):
        captured["tool_name"] = tool_name
        captured["payload"] = payload
        return {"documents": [{"parent": "documents/x", "content": "full body", "id": "x"}]}

    with patch.object(dk, "_call_mcp_tool", fake_call):
        out = asyncio.run(retrieve_developer_doc("documents/cloud.google.com/foo"))

    assert captured["tool_name"] == "get_documents"
    assert captured["payload"] == {"names": ["documents/cloud.google.com/foo"]}
    assert out["documents"][0]["content"] == "full body"


def test_retrieve_truncates_long_content():
    mock_call = AsyncMock(
        return_value={
            "documents": [{"parent": "p", "content": "y" * 8000, "id": "x"}],
        }
    )
    with patch.object(dk, "_call_mcp_tool", mock_call):
        out = asyncio.run(retrieve_developer_doc("documents/anything"))
    assert "... [truncated" in out["documents"][0]["content"]


# --------------------------------------------------------------------------- #
# Cache bounding (17.B.2 review fix I-1)
# --------------------------------------------------------------------------- #


def test_cache_size_bounded_under_many_distinct_queries():
    """Phase 17.B.2 (Codex review I-1): the response cache is FIFO-bounded
    at ``_CACHE_MAX_ENTRIES`` (1024).

    Lazy TTL eviction only removes entries on lookup, so a query that
    misses once and is never repeated stays forever. A long-lived
    coordinator instance issuing many distinct queries (or a
    misbehaving LLM) could exhaust memory. The bound is enforced in
    :func:`_cache_put`: when an insert would push over the cap, drop
    expired entries first, then evict the oldest-inserted entries.

    Insert 1100 distinct entries and assert the cache size stays at or
    below the cap. The exact post-insert size depends on whether any
    inserts triggered TTL-expired evictions, but the invariant
    ``len(cache) <= _CACHE_MAX_ENTRIES`` must always hold.
    """
    mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        for i in range(1100):
            asyncio.run(search_developer_docs(f"unique-query-{i}"))

    assert len(dk._RESPONSE_CACHE) <= dk._CACHE_MAX_ENTRIES
    # And the cap is actually reached — if it weren't, the eviction
    # code didn't run and we can't claim the bounded behavior was
    # exercised.
    assert len(dk._RESPONSE_CACHE) == dk._CACHE_MAX_ENTRIES


def test_cache_evicts_oldest_inserted_on_overflow():
    """FIFO eviction: when the cap is reached, the oldest-inserted
    entry is the first dropped. Pin the order so a future refactor
    that swaps to LRU surfaces here — the semantic is intentional
    (see module docstring).
    """
    mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        # Fill the cache to the cap.
        for i in range(dk._CACHE_MAX_ENTRIES):
            asyncio.run(search_developer_docs(f"q-{i}"))
        # The first entry is "q-0".
        assert ("search_documents", "q-0") in dk._RESPONSE_CACHE
        # Inserting one more must evict the oldest.
        asyncio.run(search_developer_docs("q-new"))

    assert ("search_documents", "q-0") not in dk._RESPONSE_CACHE
    assert ("search_documents", "q-new") in dk._RESPONSE_CACHE
    assert len(dk._RESPONSE_CACHE) == dk._CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------- #
# Cache behavior
# --------------------------------------------------------------------------- #


def test_search_cache_hits_within_ttl(monkeypatch):
    """Two calls with identical args inside the 60s TTL hit the cache
    — the underlying MCP call must run exactly once."""
    now = [1000.0]
    monkeypatch.setattr(dk.time, "monotonic", lambda: now[0])

    mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        asyncio.run(search_developer_docs("same query"))
        now[0] += 30  # still within TTL
        asyncio.run(search_developer_docs("same query"))

    assert mock_call.call_count == 1


def test_search_cache_expires_after_ttl(monkeypatch):
    """After 60s the cache entry is stale; the wrapper must call MCP
    again rather than serve a stale doc."""
    now = [1000.0]
    monkeypatch.setattr(dk.time, "monotonic", lambda: now[0])

    mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        asyncio.run(search_developer_docs("same query"))
        now[0] += 61  # cache expired
        asyncio.run(search_developer_docs("same query"))

    assert mock_call.call_count == 2


def test_retrieve_cache_hits_within_ttl(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(dk.time, "monotonic", lambda: now[0])

    mock_call = AsyncMock(
        return_value={"documents": [{"parent": "p", "content": "c", "id": "x"}]}
    )
    with patch.object(dk, "_call_mcp_tool", mock_call):
        asyncio.run(retrieve_developer_doc("documents/foo"))
        now[0] += 10
        asyncio.run(retrieve_developer_doc("documents/foo"))

    assert mock_call.call_count == 1


def test_search_and_retrieve_caches_are_independent():
    """``search_developer_docs("x")`` must not satisfy a subsequent
    ``retrieve_developer_doc("x")`` — they hit different MCP tools and
    have semantically different results."""
    search_mock = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
    get_mock = AsyncMock(
        return_value={"documents": [{"parent": "p", "content": "c", "id": "x"}]}
    )

    async def dispatch(tool_name, payload, *_, **__):
        if tool_name == "search_documents":
            return await search_mock(tool_name, payload)
        return await get_mock(tool_name, payload)

    with patch.object(dk, "_call_mcp_tool", dispatch):
        asyncio.run(search_developer_docs("x"))
        asyncio.run(retrieve_developer_doc("x"))

    assert search_mock.call_count == 1
    assert get_mock.call_count == 1


# --------------------------------------------------------------------------- #
# Timeout + fail-closed
# --------------------------------------------------------------------------- #


def test_search_timeout_returns_structured_error_not_exception():
    """A slow MCP call must be cancelled by the wrapper-level
    ``asyncio.wait_for`` and translated to a tool-result dict — the
    agent's LLM should see a structured error, never a runtime crash."""

    async def slow_call(*_args, **_kwargs):
        await asyncio.sleep(60)  # would exceed the 10s wall clock

    # Shrink the per-test timeout so the test itself doesn't take 10s.
    with patch.object(dk, "_MCP_CALL_TIMEOUT_S", 0.05), patch.object(
        dk, "_call_mcp_tool", slow_call
    ):
        out = asyncio.run(search_developer_docs("anything"))

    assert out.get("error") == "mcp_timeout"
    assert out.get("tool") == "search_documents"
    # No raw documents field on the error path — the agent should treat
    # this as "I couldn't reach the docs".
    assert "documents" not in out


def test_retrieve_timeout_returns_structured_error():
    async def slow_call(*_args, **_kwargs):
        await asyncio.sleep(60)

    with patch.object(dk, "_MCP_CALL_TIMEOUT_S", 0.05), patch.object(
        dk, "_call_mcp_tool", slow_call
    ):
        out = asyncio.run(retrieve_developer_doc("documents/x"))

    assert out.get("error") == "mcp_timeout"
    assert out.get("tool") == "get_documents"


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #


def test_search_emits_structured_log_with_required_fields(caplog):
    """Every call must emit one log line with the documented shape.
    Mirrors the JSON-extras pattern already used by
    :mod:`driftscribe_lib.logging` — ``extra={...}`` keys surface as
    top-level fields in the JSON output. We assert on the LogRecord
    attributes directly since ``caplog`` doesn't run the JSON
    formatter."""
    mock_call = AsyncMock(return_value=_make_search_result(count=2, content_len=10))
    with patch.object(dk, "_call_mcp_tool", mock_call):
        with caplog.at_level(logging.INFO, logger="agent.mcp.developer_knowledge"):
            asyncio.run(search_developer_docs("hello"))

    # Find the wrapper's log line (one record per call).
    records = [r for r in caplog.records if getattr(r, "mcp_tool", None) == "search_documents"]
    assert len(records) == 1
    rec = records[0]
    # Required fields per the plan. ``mcp_server`` carries the MCP
    # target identity (renamed from ``workload`` in 17.B.2 follow-up
    # — the previous name was confusing because ``workload`` reads
    # as "the caller's workload" (drift/upgrade), not "which MCP
    # server we called". 17.B.3 will ADD a separate ``workload``
    # field carrying the actual caller.
    assert hasattr(rec, "trace_id")
    assert getattr(rec, "mcp_server", None) == "developer_knowledge"
    assert getattr(rec, "mcp_tool", None) == "search_documents"
    assert getattr(rec, "query_or_names", None) == "hello"
    assert getattr(rec, "doc_count", None) == 2
    assert isinstance(getattr(rec, "latency_ms", None), (int, float))
    # ``workload`` field must NOT be present yet — 17.B.3 will add it
    # carrying drift/upgrade. Pinning its absence here prevents a
    # confusing dual-meaning during the gap between rename and the
    # workload-aware wiring landing.
    assert not hasattr(rec, "workload")


def test_retrieve_emits_structured_log_with_required_fields(caplog):
    mock_call = AsyncMock(
        return_value={"documents": [{"parent": "p", "content": "c", "id": "x"}]}
    )
    with patch.object(dk, "_call_mcp_tool", mock_call):
        with caplog.at_level(logging.INFO, logger="agent.mcp.developer_knowledge"):
            asyncio.run(retrieve_developer_doc("documents/foo"))

    records = [r for r in caplog.records if getattr(r, "mcp_tool", None) == "get_documents"]
    assert len(records) == 1
    rec = records[0]
    assert getattr(rec, "mcp_server", None) == "developer_knowledge"
    assert getattr(rec, "query_or_names", None) == "documents/foo"
    assert getattr(rec, "doc_count", None) == 1


def test_log_includes_real_trace_id_when_request_scoped(caplog):
    """When called inside a request scope the trace id ContextVar is
    populated and our log line MUST carry it (Phase 15.2 invariant)."""
    from driftscribe_lib.logging import reset_trace_id, set_trace_id

    token = set_trace_id("a" * 32)
    try:
        mock_call = AsyncMock(return_value=_make_search_result(count=1, content_len=10))
        with patch.object(dk, "_call_mcp_tool", mock_call):
            with caplog.at_level(logging.INFO, logger="agent.mcp.developer_knowledge"):
                asyncio.run(search_developer_docs("q"))
    finally:
        reset_trace_id(token)

    records = [r for r in caplog.records if getattr(r, "mcp_tool", None) == "search_documents"]
    assert records[0].trace_id == "a" * 32


# --------------------------------------------------------------------------- #
# Registry wiring sanity
# --------------------------------------------------------------------------- #


def test_registry_resolves_search_developer_docs_to_callable():
    """The reserved-but-not-yet-implemented slots in TOOL_REGISTRY for
    ``search_developer_docs`` and ``retrieve_developer_doc`` MUST now
    point at the real callables — that's what 17.B.2 ships."""
    from agent.workloads.registry import TOOL_REGISTRY

    assert TOOL_REGISTRY["search_developer_docs"] is search_developer_docs
    assert TOOL_REGISTRY["retrieve_developer_doc"] is retrieve_developer_doc


# --------------------------------------------------------------------------- #
# Real MCP dispatch — exercised through the SDK session manager
# --------------------------------------------------------------------------- #


def test_call_mcp_tool_invokes_session_call_tool_and_returns_structured():
    """:func:`_call_mcp_tool` opens an MCP session via the toolset's
    session manager and returns
    :attr:`mcp.types.CallToolResult.structuredContent`. Pin both: the
    session method is called with our arguments, and the structured
    content flows through unchanged.
    """
    fake_result = type(
        "FakeCallToolResult",
        (),
        {
            "isError": False,
            "structuredContent": {"documents": [{"parent": "p", "content": "c", "id": "x"}]},
            "content": [],
        },
    )()

    fake_session = AsyncMock()
    fake_session.call_tool = AsyncMock(return_value=fake_result)

    fake_session_mgr = type("FakeSessionMgr", (), {})()
    fake_session_mgr.create_session = AsyncMock(return_value=fake_session)

    # Build a toolset, then stub its session manager so the dispatch
    # path doesn't touch the real MCP server.
    toolset = build_developer_knowledge_toolset()
    toolset._mcp_session_manager = fake_session_mgr  # noqa: SLF001
    dk._TOOLSET = toolset  # bypass lazy build for this test

    out = asyncio.run(dk._call_mcp_tool("search_documents", {"query": "foo"}))

    fake_session.call_tool.assert_awaited_once_with("search_documents", {"query": "foo"})
    assert out == {"documents": [{"parent": "p", "content": "c", "id": "x"}]}


def test_call_mcp_tool_isError_raises_so_wrapper_can_translate():
    """When the server returns ``isError=True``, dispatch raises a
    plain exception. The wrapper layer (``_wrap_mcp_call``) catches it
    and emits a structured ``mcp_error`` tool result — tested
    separately below."""
    fake_result = type(
        "FakeCallToolResult",
        (),
        {"isError": True, "structuredContent": None, "content": "boom"},
    )()
    fake_session = AsyncMock()
    fake_session.call_tool = AsyncMock(return_value=fake_result)
    fake_session_mgr = type("FakeSessionMgr", (), {})()
    fake_session_mgr.create_session = AsyncMock(return_value=fake_session)

    toolset = build_developer_knowledge_toolset()
    toolset._mcp_session_manager = fake_session_mgr  # noqa: SLF001
    dk._TOOLSET = toolset

    with pytest.raises(RuntimeError, match="isError=True"):
        asyncio.run(dk._call_mcp_tool("search_documents", {"query": "x"}))


# --------------------------------------------------------------------------- #
# Generic MCP error fail-closed
# --------------------------------------------------------------------------- #


def test_search_generic_error_returns_structured_error(caplog):
    """Any non-timeout error from the MCP dispatch path (auth failure,
    session creation failure, server isError=True, parse error, ...)
    MUST be translated to a structured tool result so the agent's LLM
    sees a tool-result dict, not a crash. Mirrors the timeout-path
    test but for the catch-all branch."""

    async def boom(*_args, **_kwargs):
        raise ConnectionError("MCP server unreachable")

    with patch.object(dk, "_call_mcp_tool", boom):
        with caplog.at_level(logging.INFO, logger="agent.mcp.developer_knowledge"):
            out = asyncio.run(search_developer_docs("anything"))

    assert out["error"] == "mcp_error"
    assert out["tool"] == "search_documents"
    assert "ConnectionError" in out.get("detail", "")
    assert "documents" not in out

    records = [r for r in caplog.records if getattr(r, "mcp_tool", None) == "search_documents"]
    assert records and records[0].error == "mcp_error"


def test_public_wrapper_propagates_missing_api_key(monkeypatch):
    """Missing :envvar:`DEVELOPER_KNOWLEDGE_API_KEY` at first-call time
    is a deploy/config failure, NOT a per-call MCP failure. The
    public wrapper must propagate the
    :class:`MissingDeveloperKnowledgeApiKeyError` so the operator
    sees the missing Secret-Manager binding in Cloud Run logs —
    squashing it to a structured ``mcp_error`` result would mask
    config failure as transient flakiness.

    Pins the boundary between "config is wrong" (loud) and "MCP call
    failed" (quiet, structured)."""
    monkeypatch.delenv("DEVELOPER_KNOWLEDGE_API_KEY", raising=False)
    # The toolset hasn't been built yet (fixture cleared _TOOLSET).
    # First call triggers lazy build, which must fail closed.
    with pytest.raises(MissingDeveloperKnowledgeApiKeyError):
        asyncio.run(search_developer_docs("anything"))


def test_generic_error_response_is_not_cached():
    """A failed call must NOT poison the cache — the next call has to
    retry the MCP server (the failure may be transient). Pin this with
    a two-call sequence: first call errors, second call succeeds, and
    both reach the underlying dispatch."""
    calls = {"n": 0}

    async def flake(_tool_name, _payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("first try fails")
        return {"documents": [{"parent": "p", "content": "c", "id": "x"}]}

    with patch.object(dk, "_call_mcp_tool", flake):
        first = asyncio.run(search_developer_docs("q"))
        second = asyncio.run(search_developer_docs("q"))

    assert first["error"] == "mcp_error"
    assert second["documents"][0]["id"] == "x"
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# ADK get_tools() — filter excludes answer_query end-to-end
# --------------------------------------------------------------------------- #


def test_missing_dk_key_exception_inherits_from_runtime_error():
    """Phase 17.B.2 (Codex review I-5): pin the exception's MRO.

    :class:`MissingDeveloperKnowledgeApiKeyError` inherits from
    ``RuntimeError`` (not from
    :class:`agent.workloads.MissingWorkerEnvError`) because the
    developer-knowledge API key is NOT a worker env var — collapsing
    the hierarchies would muddy the exception taxonomy. The
    handler-level 503 mapping is achieved by adding the class to each
    exception tuple in ``agent/main.py``, not by inheritance. See
    ``test_main_exception_tuples_include_missing_dk_key`` below for
    the matching wiring assertion.
    """
    from agent.workloads import MissingWorkerEnvError

    assert issubclass(MissingDeveloperKnowledgeApiKeyError, RuntimeError)
    assert not issubclass(MissingDeveloperKnowledgeApiKeyError, MissingWorkerEnvError)


def test_get_tools_filter_excludes_answer_query():
    """End-to-end shape check: the toolset's ``get_tools()`` (which
    ADK calls when materializing tools for the agent) goes through
    ``session.list_tools()`` and then applies our ``tool_filter``. We
    mock ``list_tools`` to return all three Developer Knowledge tools
    and assert the filter drops ``answer_query``.
    """
    from mcp.types import ListToolsResult, Tool

    fake_tools = ListToolsResult(
        tools=[
            Tool(name="search_documents", description="search", inputSchema={"type": "object"}),
            Tool(name="get_documents", description="get", inputSchema={"type": "object"}),
            Tool(name="answer_query", description="preview", inputSchema={"type": "object"}),
        ]
    )
    fake_session = AsyncMock()
    fake_session.list_tools = AsyncMock(return_value=fake_tools)
    fake_session_mgr = type("FakeSessionMgr", (), {})()
    fake_session_mgr.create_session = AsyncMock(return_value=fake_session)

    toolset = build_developer_knowledge_toolset()
    toolset._mcp_session_manager = fake_session_mgr  # noqa: SLF001

    tools = asyncio.run(toolset.get_tools(None))
    names = {t.name for t in tools}
    assert "search_documents" in names
    assert "get_documents" in names
    assert "answer_query" not in names
