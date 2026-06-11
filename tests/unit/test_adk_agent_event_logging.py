"""Pin the structured log shape for thought / tool-call events, including
the partial-event dedup. ADK streams thoughts as a sequence of partial
events and then re-emits them merged in a non-partial event — naive
per-event logging would multiply each thought summary.

Field schema (consumed by Logs Explorer queries documented in the
deploy runbook):

  event=llm_thought   trace_id=<hex32>  workload=<name>  thought_text=<text>
  event=tool_call     trace_id=<hex32>  workload=<name>  tool_name=<name>
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


async def _stub_run(*args, **kwargs):
    # Two partial thought chunks — should NOT be logged.
    yield _Ev(
        [_P(text="checking ", thought=True)],
        partial=True,
    )
    yield _Ev(
        [_P(text="...contract", thought=True)],
        partial=True,
    )
    # Merged non-partial thought — SHOULD be logged (once).
    yield _Ev(
        [_P(text="checking ...contract", thought=True)],
        partial=False,
    )
    # Tool call — SHOULD be logged (function_calls never come as partials,
    # but we apply the same guard for uniformity).
    yield _Ev(
        [_P(function_call=SimpleNamespace(name="read_drift"))],
        partial=False,
    )
    # Final response.
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_dedups_partial_thoughts(caplog, drift_workload_env):
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    # Bind the workload ContextVar the same way the request handler in
    # ``agent.main.chat`` does — :func:`run_chat` itself does not call
    # :func:`set_workload`; that's the request frame's job. The log line
    # reads :func:`current_workload`, so without a binding it would
    # surface as ``"unknown"``.
    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_run
            result = await adk_agent.run_chat("hi", workload="drift", autonomy_mode="propose_apply")
    finally:
        reset_workload(token)

    thoughts = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_thought"
    ]
    assert len(thoughts) == 1, f"expected 1 thought log, got {len(thoughts)}"
    assert getattr(thoughts[0], "thought_text", "") == "checking ...contract"
    assert getattr(thoughts[0], "workload", None) == "drift"

    tool_calls = [
        r for r in caplog.records
        if getattr(r, "event", None) == "tool_call"
    ]
    assert len(tool_calls) == 1
    assert getattr(tool_calls[0], "tool_name", None) == "read_drift"

    # /chat response body still includes tool_calls for back-compat with
    # the operator UI (this is a public contract from Phase 11.7).
    assert result["tool_calls"] == ["read_drift"]


def test_emit_event_logs_returns_redacted_dicts():
    """_emit_event_logs returns the same redacted dicts it logs, in order.

    Phase 22 (streaming prep): the helper still logs byte-identically, but
    now returns the redacted payloads so run_chat_stream can yield the
    SAME dict it logged (single source of truth for redaction).
    """
    ev = _Ev(
        [
            _P(text="thinking about it", thought=True),
            _P(function_call=SimpleNamespace(
                name="read_drift", args={"PASSWORD": "hunter2", "ok": "v"})),
        ],
        partial=False,
    )
    out = adk_agent._emit_event_logs(ev, tool_calls=[])
    assert [d["event"] for d in out] == ["llm_thought", "tool_call"]
    assert out[0]["thought_text"] == "thinking about it"
    # redaction applied to tool_args (PASSWORD masked, plain value kept)
    assert out[1]["tool_args"]["PASSWORD"] != "hunter2"
    assert out[1]["tool_args"]["ok"] == "v"


def test_emit_llm_usage_returns_dict_or_none():
    no_usage = _Ev([], partial=False)
    assert adk_agent._emit_llm_usage(no_usage) is None

    with_usage = _Ev([], partial=False, usage=SimpleNamespace(
        prompt_token_count=10, candidates_token_count=5,
        thoughts_token_count=2, total_token_count=17))
    d = adk_agent._emit_llm_usage(with_usage)
    assert d["event"] == "llm_usage"
    assert d["total_token_count"] == 17
