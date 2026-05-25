"""Pin the contract of :func:`agent.adk_agent.run_chat_stream` — the core
streaming generator that the SSE `/chat` path drains.

It must yield, in current-log order, ``{"type":"event","event":<dict>}``
items for each timeline event, then a terminal
``{"type":"result", ...}``. Streamed event dicts carry synthetic
``seq``/``insert_id``/``timestamp`` (Cloud Logging supplies these for the
polling path; SSE has to synthesize them). Redaction is identical to the
durable log copy. Empty replies raise ``RuntimeError`` just like
:func:`run_chat`.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


async def _stub_run(*args, **kwargs):
    yield _Ev([_P(text="checking", thought=True)], partial=False)
    yield _Ev([_P(function_call=SimpleNamespace(
        name="read_drift", args={"PASSWORD": "s3cret"}))], partial=False)
    yield _Ev([_P(function_response=SimpleNamespace(
        name="read_drift", response={"ok": True}))], partial=False)
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"ok","confidence":0.9}')],
        partial=False, final=True,
        usage=SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                              thoughts_token_count=0, total_token_count=2))


@pytest.mark.asyncio
async def test_run_chat_stream_order_terminal_and_redaction(drift_workload_env):
    token = set_workload("drift")
    items = []
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_run
            async for it in adk_agent.run_chat_stream("hi", workload="drift"):
                items.append(it)
    finally:
        reset_workload(token)

    events = [it["event"] for it in items if it["type"] == "event"]
    kinds = [e["event"] for e in events]
    # order: thought, tool_call, tool_result, final_response, llm_usage
    assert kinds == ["llm_thought", "tool_call", "tool_result",
                     "final_response", "llm_usage"]
    # tool_call args redacted
    tc = next(e for e in events if e["event"] == "tool_call")
    assert tc["tool_args"]["PASSWORD"] != "s3cret"
    # streamed events carry synthetic ordering fields
    assert all("seq" in e and "insert_id" in e and "timestamp" in e
               for e in events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    # terminal result is last
    assert items[-1]["type"] == "result"
    assert items[-1]["tool_calls"] == ["read_drift"]
    assert items[-1]["reply"]
    assert items[-1]["session_id"]


@pytest.mark.asyncio
async def test_run_chat_stream_empty_reply_raises(drift_workload_env):
    async def _empty(*a, **k):
        yield _Ev([_P(function_call=SimpleNamespace(name="read_drift"))],
                  partial=False)
        # final event with no usable text
        yield _Ev([_P(text="")], partial=False, final=True)

    token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _empty
            with pytest.raises(RuntimeError):
                async for _ in adk_agent.run_chat_stream("hi", workload="drift"):
                    pass
    finally:
        reset_workload(token)
