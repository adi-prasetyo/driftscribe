"""Developer Knowledge MCP toolset + ADK function-tool wrappers (Phase 17.B.2).

The Google Developer Knowledge MCP server is a remote Streamable HTTP
endpoint exposing tools that search and fetch authoritative product
documentation (Cloud Run, GitHub Actions, etc.). The coordinator's LLM
uses these as a "ground in real docs before writing a docs PR" surface.

Verified install state (2026-05-20):

- ``google-adk == 1.33.0``, ``mcp == 1.27.1``
- :class:`~google.adk.tools.mcp_tool.mcp_session_manager.StreamableHTTPConnectionParams`
  signature::

      StreamableHTTPConnectionParams(
          *,
          url: str,
          headers: dict[str, Any] | None = None,
          timeout: float = 5.0,
          sse_read_timeout: float = 300.0,
          terminate_on_close: bool = True,
          httpx_client_factory: ...,
      )

  The ``timeout`` kwarg is the *SDK connection timeout* — distinct from
  the total wall-clock budget for a tool call. We set ``timeout=10.0``
  here AND enforce a 10-second :func:`asyncio.wait_for` in the wrapper.
  The two bounds compose: a stalled connect blows up at the SDK layer
  in 10s; a slow body / slow tool execution blows up at the wrapper
  layer in 10s. Both translate to a structured error result
  (``{"error": "mcp_timeout", ...}``) so the LLM never sees a raw
  exception.

Tool filter: ``["search_documents", "get_documents"]``. ``answer_query``
(Preview status) is deliberately excluded — it's a second LLM-reasoning
surface we don't want; we ground the coordinator's own LLM in raw doc
content instead.

Wrapper guardrails layered on top of the raw MCP calls:

- 10s wall-clock timeout per call (separate from the SDK's connection
  timeout — both apply).
- 60s in-process response cache keyed by ``(tool_name, key)``. Saves
  cost + latency when the LLM searches the same term twice in one
  turn. Cleared on coordinator restart. The cache is bounded at
  ``_CACHE_MAX_ENTRIES`` (1024) with FIFO eviction on overflow so a
  long-lived coordinator that sees many distinct queries can't
  exhaust memory.

  Concurrency note (single-writer assumption): the cache is a plain
  ``OrderedDict`` with no per-key lock. Two concurrent ``/chat``
  requests issuing the same query inside the TTL window may both
  miss the cache and dispatch the MCP call twice — the loser
  overwrites the winner's entry. This is harmless duplication
  (identical results), not a correctness bug. If concurrent
  duplication becomes operationally significant (e.g. it shows up as
  spend on the Developer Knowledge API quota), switch to an
  ``asyncio.Lock``-per-key pattern that lets the second caller
  await the first's in-flight call. Out of scope for 17.B.2.

  Trace correlation note: :func:`current_trace_id_or_new` mints a
  fresh trace id when called outside a request scope (e.g. from a
  background task, or during a unit test that doesn't set the
  ContextVar). Correlating MCP calls made off the request hot path
  with their originating request requires the caller to explicitly
  propagate the trace id by setting the ContextVar before invoking
  the wrapper.
- Result truncation: at most 5 documents per response, at most 4000
  chars per document body. A truncated body gets a clear suffix
  ``... [truncated 4000/<original>]`` so the LLM knows the content was
  clipped.
- Structured log emitted every call with the fields ``{trace_id,
  workload, mcp_server, mcp_tool, query_or_names, doc_count,
  latency_ms}``. ``mcp_server`` carries the MCP target identity
  (``"developer_knowledge"`` here); ``workload`` carries the *caller*
  identity (``"drift"`` / ``"upgrade"`` / ``"unknown"``) read from
  the :func:`agent.workloads.current_workload` ContextVar bound by
  the request handlers in :mod:`agent.main`. ``"unknown"`` covers
  background calls and any test that exercises the wrapper outside
  a request scope. The trace id is read from the same
  ``driftscribe_lib.logging`` ContextVar that
  :mod:`agent.worker_client` uses to propagate the per-request trace
  id to workers — log lines on the agent and outbound MCP calls
  share a single correlation key.
- Fail-closed error translation: timeouts and other MCP errors become
  ``{"error": "...", "tool": ...}`` tool results, never propagated
  exceptions. The agent's LLM sees a structured failure it can reason
  about (e.g., decide not to cite a non-existent doc) rather than
  crashing the chat handler.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

# Import the workload ContextVar from the package-root
# :mod:`agent.workload_context` module, NOT from ``agent.workloads`` —
# the ``agent.workloads`` package ``__init__.py`` imports
# ``agent.workloads.registry``, which in turn imports the MCP wrappers
# from this module. Going through the package would create a circular
# import (the registry would re-enter this partially-initialized
# module). ``agent.workload_context`` is a sibling module to
# ``agent.workloads`` rather than a submodule, so importing it doesn't
# trigger the workloads package ``__init__.py`` and the cycle is
# avoided. See that module's docstring for the full rationale.
from agent.workload_context import current_workload
from driftscribe_lib.logging import current_trace_id_or_new


# --------------------------------------------------------------------------- #
# Constants — all knobs centralized so future tuning is one diff.
# --------------------------------------------------------------------------- #

_MCP_URL = "https://developerknowledge.googleapis.com/mcp"
_API_KEY_ENV = "DEVELOPER_KNOWLEDGE_API_KEY"
# SDK-level connection timeout (kwarg on StreamableHTTPConnectionParams).
# Default in ADK 1.33.0 is 5.0; we bump to 10.0 to accommodate the
# Developer Knowledge backend's typical first-byte latency.
_SDK_CONNECT_TIMEOUT_S = 10.0
# Wrapper-level wall-clock budget per tool call. Same value as the SDK
# connect timeout but enforced at a different layer — a stalled body
# read or slow tool execution hits this even if the connect was fast.
_MCP_CALL_TIMEOUT_S = 10.0
# Tools we expose to the agent. ``answer_query`` is deliberately
# excluded — see module docstring.
_TOOL_FILTER = ["search_documents", "get_documents"]

# Truncation guardrails.
_MAX_DOCS_PER_RESPONSE = 5
_MAX_DOC_CONTENT_CHARS = 4000

# Cache TTL. 60s is a deliberate compromise: long enough that the LLM
# searching the same term twice in one /chat turn hits the cache;
# short enough that a refreshed doc shows up on the operator's next
# request (the coordinator is long-lived under Cloud Run idle warm-up).
_CACHE_TTL_S = 60.0
# Hard upper bound on cache entries. Lazy TTL eviction only removes
# entries on lookup, so keys that miss the cache once and are never
# searched again stay forever. A misbehaving LLM that issues many
# distinct queries (or just steady-state operation over weeks of
# Cloud Run uptime) could exhaust memory without this cap. 1024 is
# generous for the expected query diversity per coordinator instance
# and small enough that the OrderedDict overhead is negligible
# (~100KB of metadata, plus the cached responses themselves which
# are already bounded by the 5-doc / 4000-char-per-doc truncation).
_CACHE_MAX_ENTRIES = 1024


# --------------------------------------------------------------------------- #
# Module-level state.
# --------------------------------------------------------------------------- #

_log = logging.getLogger(__name__)

# In-process cache. Key shape: ``(tool_name, normalized_key)`` →
# ``(result_dict, expires_at_monotonic)``. Module-level so a long-lived
# coordinator instance accumulates hits across requests; cleared by
# tests via the fixture in ``test_mcp_developer_knowledge.py``.
#
# ``OrderedDict`` (not ``dict``) so :func:`_cache_put` can evict the
# oldest-inserted entry on overflow in O(1) — see ``_CACHE_MAX_ENTRIES``
# and the bounding logic in ``_cache_put``. FIFO (not LRU) chosen for
# simplicity: a stale-enough entry will expire on its own via the TTL
# path, and FIFO eviction needs zero bookkeeping on the read path.
_RESPONSE_CACHE: OrderedDict[tuple[str, str], tuple[dict, float]] = OrderedDict()

# Shared toolset instance. Built lazily on first MCP call so module
# import is free of network dependencies and the missing-API-key check
# happens at the first request boundary rather than at coordinator boot
# (the coordinator may import this module before the env var resolves
# to its Secret-Manager-injected value, depending on init order). The
# toolset's ``MCPSessionManager`` pools sessions across calls, so reusing
# one toolset across calls is the right cost shape.
_TOOLSET: McpToolset | None = None


# --------------------------------------------------------------------------- #
# Exceptions.
# --------------------------------------------------------------------------- #


class MissingDeveloperKnowledgeApiKeyError(RuntimeError):
    """Raised when :envvar:`DEVELOPER_KNOWLEDGE_API_KEY` is unset or
    empty at toolset-build time.

    Mirrors :class:`agent.workloads.registry.MissingWorkerEnvError`'s
    fail-closed-at-boot pattern. The coordinator boot path either
    surfaces this directly (the Cloud Run revision fails its readiness
    check and never serves traffic) or it surfaces as a 500/503 on the
    first ``/chat`` request, depending on how the toolset is wired.
    Either way the operator sees the missing-config message in the
    Cloud Run logs immediately — no silent capability loss.
    """


# --------------------------------------------------------------------------- #
# Toolset construction.
# --------------------------------------------------------------------------- #


def build_developer_knowledge_toolset() -> McpToolset:
    """Build the :class:`McpToolset` bound to the Developer Knowledge MCP.

    Reads :envvar:`DEVELOPER_KNOWLEDGE_API_KEY` from env. Raises
    :class:`MissingDeveloperKnowledgeApiKeyError` if unset / empty —
    fail-closed at boot rather than silently lose the capability.

    Used by ``agent.adk_agent`` (Phase 17.B.3) to wire the MCP tools
    into the coordinator's ``LlmAgent``. The wrapper callables
    :func:`search_developer_docs` and :func:`retrieve_developer_doc`
    do NOT go through this toolset — they're standalone ADK function
    tools that talk to MCP via a separate code path. The toolset
    object is constructed for two reasons:

    1. It's the canonical place to keep the connection-params
       construction (URL, headers, timeouts) co-located with the
       wrapper functions so a future refactor that switches to the
       toolset's own tool dispatcher only has to change one file.
    2. It documents the ``tool_filter`` decision (exclude
       ``answer_query``) as code rather than as a comment.
    """
    api_key = os.environ.get(_API_KEY_ENV, "")
    if not api_key:
        raise MissingDeveloperKnowledgeApiKeyError(
            f"{_API_KEY_ENV} is unset or empty — the Developer Knowledge "
            f"MCP toolset cannot be built. For Cloud Run deploys this is "
            f"populated by the cloudbuild.yaml --set-secrets step from "
            f"Secret Manager (secret name: developer-knowledge-api-key). "
            f"For local dev, set the env var to a Google API key restricted "
            f"to developerknowledge.googleapis.com only."
        )
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=_MCP_URL,
            headers={"X-Goog-Api-Key": api_key},
            timeout=_SDK_CONNECT_TIMEOUT_S,
        ),
        tool_filter=list(_TOOL_FILTER),
    )


# --------------------------------------------------------------------------- #
# Raw MCP call adapter — single seam for tests to mock.
# --------------------------------------------------------------------------- #


def _get_toolset() -> McpToolset:
    """Return the cached toolset, building it on first call.

    Lazy so the missing-API-key check defers until first MCP call —
    matches the lazy worker URL resolution in
    :mod:`agent.worker_client` and lets the coordinator boot even if
    Secret Manager injection completes after Python import (it doesn't
    today, but cloud-init ordering is not contractual).
    """
    global _TOOLSET
    if _TOOLSET is None:
        _TOOLSET = build_developer_knowledge_toolset()
    return _TOOLSET


async def _call_mcp_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a single MCP tool against the Developer Knowledge server.

    Opens an MCP session via the toolset's ``MCPSessionManager``,
    calls ``tool_name`` with ``payload`` as arguments, and returns the
    parsed structured content. The session pool is owned by the
    toolset — we don't open or close it here.

    Kept as a thin, separate async function so unit tests can mock it
    at one seam (``patch.object(dk, "_call_mcp_tool", ...)``). The
    integration test in Phase 17.B.4 exercises this path against a
    mock MCP server.

    Returns whatever shape the server put in
    :attr:`mcp.types.CallToolResult.structuredContent` (for the
    Developer Knowledge MCP that's ``{"documents": [...]}``). Errors
    propagate as exceptions; :func:`_wrap_mcp_call` catches and
    translates them into structured tool results so the agent's LLM
    never sees a raw exception.
    """
    toolset = _get_toolset()
    # Session lifecycle is owned by :class:`MCPSessionManager` (the
    # toolset's pool); we don't open or close it here. ADK 1.33.0
    # contract: ``create_session()`` returns a session whose lifetime
    # is bound to the session manager — calling ``session.close()``
    # would yank it out of the pool and break the next caller. If
    # ``google-adk`` 1.34+ flips this contract (i.e. callers must
    # close), this comment needs to flip and we add a
    # ``try/finally: await session.close()`` around the call.
    session = await toolset._mcp_session_manager.create_session()  # noqa: SLF001
    result = await session.call_tool(tool_name, payload)
    if result.isError:
        # Surface the server's error content in the exception message —
        # ``_wrap_mcp_call`` converts this to a structured tool result.
        raise RuntimeError(
            f"MCP server returned isError=True for {tool_name!r}: {result.content!r}"
        )
    structured = result.structuredContent
    if structured is None:
        # Server didn't return structured JSON. Surface as an empty
        # response shape so the wrapper's truncation step is a no-op
        # and the agent sees ``{"documents": []}`` — semantically the
        # same as "no docs matched".
        return {"documents": []}
    return structured


# --------------------------------------------------------------------------- #
# Wrapper utilities — cache, truncation, logging.
# --------------------------------------------------------------------------- #


def _cache_get(tool_name: str, key: str) -> dict[str, Any] | None:
    """Return a cached result or ``None`` if absent / expired.

    Expired entries are removed lazily on lookup; this keeps the cache
    small without a background sweep thread and matches the coordinator's
    long-lived-but-low-traffic shape under Cloud Run.
    """
    entry = _RESPONSE_CACHE.get((tool_name, key))
    if entry is None:
        return None
    result, expires_at = entry
    if time.monotonic() >= expires_at:
        _RESPONSE_CACHE.pop((tool_name, key), None)
        return None
    return result


def _cache_put(tool_name: str, key: str, result: dict[str, Any]) -> None:
    """Store ``result`` in the response cache, evicting on overflow.

    When inserting would push the cache over ``_CACHE_MAX_ENTRIES``,
    we first drop any expired entries (cheap to identify, and they'd
    be evicted on their next lookup anyway). If we're still over the
    cap, drop the oldest-inserted entries until we fit. The first
    pass usually clears enough room on its own; the FIFO fallback
    only kicks in under steady-state load where the cap is reached
    before the TTL expires anything.
    """
    cache_key = (tool_name, key)
    # If the key already exists, ``__setitem__`` updates in place — no
    # net growth, no eviction needed. Handle that fast path first so
    # the eviction logic only runs on real inserts.
    if cache_key in _RESPONSE_CACHE:
        _RESPONSE_CACHE[cache_key] = (result, time.monotonic() + _CACHE_TTL_S)
        # Move to end so the entry's "insertion order" matches the
        # last write — keeps FIFO semantics intuitive on repeated
        # writes (re-cached entries don't get evicted before truly
        # older ones).
        _RESPONSE_CACHE.move_to_end(cache_key)
        return

    if len(_RESPONSE_CACHE) >= _CACHE_MAX_ENTRIES:
        # First pass: drop expired entries. Iterate over a snapshot of
        # keys since we mutate during iteration.
        now = time.monotonic()
        for k in list(_RESPONSE_CACHE.keys()):
            _, expires_at = _RESPONSE_CACHE[k]
            if now >= expires_at:
                del _RESPONSE_CACHE[k]
        # Second pass: still over cap? Evict oldest-inserted until we
        # have room for the new entry. Cap leaves room for one insert.
        while len(_RESPONSE_CACHE) >= _CACHE_MAX_ENTRIES:
            _RESPONSE_CACHE.popitem(last=False)

    _RESPONSE_CACHE[cache_key] = (result, time.monotonic() + _CACHE_TTL_S)


def _truncate_documents(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply the 5-doc / 4000-char-per-doc guardrails to a raw MCP
    response.

    The raw shape is ``{"documents": [{"parent": ..., "content": ...,
    "id": ...}]}`` (per the Developer Knowledge docs). A response with
    no ``documents`` key — or a non-list value — is returned untouched
    on the principle that the wrapper should not silently rewrite an
    unrecognized shape; the LLM can see whatever the server returned
    and reason about it. Tests pin the happy-path shape so a drift in
    server semantics surfaces there.
    """
    docs = raw.get("documents")
    if not isinstance(docs, list):
        return raw
    truncated_docs = []
    for doc in docs[:_MAX_DOCS_PER_RESPONSE]:
        content = doc.get("content", "")
        if isinstance(content, str) and len(content) > _MAX_DOC_CONTENT_CHARS:
            original_len = len(content)
            content = (
                content[:_MAX_DOC_CONTENT_CHARS]
                + f"... [truncated {_MAX_DOC_CONTENT_CHARS}/{original_len}]"
            )
        truncated_docs.append({**doc, "content": content})
    return {**raw, "documents": truncated_docs}


def _log_call(
    *,
    mcp_tool: str,
    query_or_names: str,
    doc_count: int,
    latency_ms: float,
    error: str | None = None,
) -> None:
    """Emit a single structured log record per call.

    Fields surface as top-level JSON keys via the project's
    :class:`driftscribe_lib.logging.JSONFormatter` (which walks
    ``record.__dict__`` for caller-supplied extras). ``trace_id`` is
    read from the ContextVar set by the request middleware — same
    source as :mod:`agent.worker_client`. ``workload`` is read from
    the :func:`agent.workloads.current_workload` ContextVar set by
    the request handler before invoking the agent
    (:func:`agent.main.chat`, :func:`agent.main._do_recheck`). It
    defaults to ``"unknown"`` when no binding is in scope (background
    tasks, unit tests not inside a request frame). Separating the two
    fields — ``mcp_server`` (which MCP we called) vs ``workload`` (who
    asked us to call it) — lets the observability dashboards slice
    latency / failures / quota by caller, which is the whole point of
    the per-workload routing in 17.A.3.
    """
    extras = {
        "trace_id": current_trace_id_or_new(),
        "workload": current_workload(),
        "mcp_server": "developer_knowledge",
        "mcp_tool": mcp_tool,
        "query_or_names": query_or_names,
        "doc_count": doc_count,
        "latency_ms": latency_ms,
    }
    if error is not None:
        extras["error"] = error
    _log.info("mcp_call", extra=extras)


# --------------------------------------------------------------------------- #
# Public wrappers — these are what land in TOOL_REGISTRY.
# --------------------------------------------------------------------------- #


async def search_developer_docs(query: str) -> dict[str, Any]:
    """Search the Developer Knowledge corpus for documents matching ``query``.

    Returns a dict with a ``documents`` key whose value is a list of
    at most 5 doc refs, each with ``parent`` (the document path),
    ``content`` (truncated to 4000 chars if longer), and ``id``. On
    timeout returns ``{"error": "mcp_timeout", "tool":
    "search_documents", ...}`` — the LLM sees a structured failure
    rather than a runtime crash.

    Args:
        query: free-text search string. Passed to the MCP server as-is.

    Returns:
        Either ``{"documents": [...]}`` on success or ``{"error":
        <code>, "tool": "search_documents", "query": <query>}`` on
        failure.
    """
    return await _wrap_mcp_call(
        mcp_tool="search_documents",
        payload={"query": query},
        cache_key=query,
        query_or_names=query,
    )


async def retrieve_developer_doc(name: str) -> dict[str, Any]:
    """Fetch the full body of a single Developer Knowledge document by name.

    The MCP ``get_documents`` tool takes a list of names; this wrapper
    accepts a single name and wraps it so the LLM doesn't need to
    learn the list-of-names shape. Use the ``parent`` field from a
    prior :func:`search_developer_docs` result as ``name``.

    Returns the same dict shape as :func:`search_developer_docs`, with
    a ``documents`` list of length 1 on success or an ``error`` key on
    failure.

    Args:
        name: document name (e.g.
            ``"documents/cloud.google.com/run/docs/configuring/environment-variables"``).
    """
    return await _wrap_mcp_call(
        mcp_tool="get_documents",
        payload={"names": [name]},
        cache_key=name,
        query_or_names=name,
    )


async def _wrap_mcp_call(
    *,
    mcp_tool: str,
    payload: dict[str, Any],
    cache_key: str,
    query_or_names: str,
) -> dict[str, Any]:
    """Shared call path for the two public wrappers.

    Implements the guardrails documented in the module docstring:
    cache lookup → wall-clock-bound MCP call → truncation → structured
    log → return. On timeout returns a fail-closed dict; the agent's
    LLM sees a tool result, not a raised exception.
    """
    cached = _cache_get(mcp_tool, cache_key)
    if cached is not None:
        # Hit: still emit a structured log so the timeline shows the
        # call happened. ``latency_ms`` is ~0 to reflect that.
        _log_call(
            mcp_tool=mcp_tool,
            query_or_names=query_or_names,
            doc_count=_doc_count(cached),
            latency_ms=0.0,
        )
        return cached

    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(
            _call_mcp_tool(mcp_tool, payload),
            timeout=_MCP_CALL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        latency_ms = (time.monotonic() - started) * 1000.0
        _log_call(
            mcp_tool=mcp_tool,
            query_or_names=query_or_names,
            doc_count=0,
            latency_ms=latency_ms,
            error="mcp_timeout",
        )
        return {
            "error": "mcp_timeout",
            "tool": mcp_tool,
            "query_or_names": query_or_names,
            "timeout_s": _MCP_CALL_TIMEOUT_S,
        }
    except MissingDeveloperKnowledgeApiKeyError:
        # Missing API key is a deploy / config failure, NOT a per-call
        # MCP failure. Let it propagate so the operator sees the
        # configuration error directly in the coordinator's logs (and
        # so the chat handler can map it to a 503 if/when this path is
        # reached at request time). Squashing it to ``mcp_error`` here
        # would mask a missing Secret Manager binding as transient
        # MCP-server flakiness — the opposite of fail-closed.
        raise
    except Exception as exc:
        # Catch-all for non-timeout MCP errors: auth failures, session
        # creation failures, server-side ``isError`` responses,
        # malformed payloads, etc. The agent's LLM should see a
        # structured failure it can reason about — never a raw
        # exception bubble out of a tool call and crash the chat
        # handler. Cache miss path only (cache hits are pre-validated).
        latency_ms = (time.monotonic() - started) * 1000.0
        error_repr = f"{type(exc).__name__}: {exc}"[:200]
        _log_call(
            mcp_tool=mcp_tool,
            query_or_names=query_or_names,
            doc_count=0,
            latency_ms=latency_ms,
            error="mcp_error",
        )
        return {
            "error": "mcp_error",
            "tool": mcp_tool,
            "query_or_names": query_or_names,
            "detail": error_repr,
        }

    result = _truncate_documents(raw) if isinstance(raw, dict) else {"documents": []}
    _cache_put(mcp_tool, cache_key, result)
    latency_ms = (time.monotonic() - started) * 1000.0
    _log_call(
        mcp_tool=mcp_tool,
        query_or_names=query_or_names,
        doc_count=_doc_count(result),
        latency_ms=latency_ms,
    )
    return result


def _doc_count(payload: dict[str, Any]) -> int:
    """Count documents in a response, defensively.

    The server normally returns ``{"documents": [...]}``, but a
    misconfigured or evolving backend could return ``None``, a dict,
    or omit the key. Logging callers want a clean integer, not a
    crash — match the wrapper's overall fail-closed posture.
    """
    docs = payload.get("documents")
    return len(docs) if isinstance(docs, list) else 0
