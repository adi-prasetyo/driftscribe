"""Pin the llm_usage log line shape. One record per LLM event that
carries usage metadata. Required fields: prompt_token_count,
candidates_token_count, thoughts_token_count, total_token_count.
thoughts_token_count is the whole point — it's the only way to prove
post-deploy that include_thoughts=True did or did not move the cost
needle relative to the pre-Phase-18 baseline.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import adk_agent
from agent.workload_context import reset_workload, set_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


def _usage(prompt=120, candidates=80, thoughts=64, total=264):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        thoughts_token_count=thoughts,
        total_token_count=total,
    )


async def _stub_run(*args, **kwargs):
    yield _Ev([_P(text="reasoning", thought=True)], partial=False)
    yield _Ev(
        [_P(text='{"action":"no_op","env_diffs":[],"rationale":"matches","confidence":0.9}')],
        partial=False,
        final=True,
        usage=_usage(),
    )


@pytest.mark.asyncio
async def test_run_chat_emits_llm_usage_log(caplog, drift_workload_env):
    """A workload binding is required because `current_workload()`
    returns ``"unknown"`` without one — mirror what `agent.main.chat`
    does in production before invoking `run_chat`."""
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    workload_token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_run
            await adk_agent.run_chat("hi", workload="drift", autonomy_mode="propose_apply")
    finally:
        reset_workload(workload_token)

    usage = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_usage"
    ]
    # In RunConfig(streaming_mode=NONE), usage_metadata is yielded exactly
    # once per call — tighten the assertion to catch accidental duplication.
    assert len(usage) == 1, f"expected exactly 1 llm_usage record, got {len(usage)}"
    r = usage[0]
    assert getattr(r, "prompt_token_count") == 120
    assert getattr(r, "candidates_token_count") == 80
    assert getattr(r, "thoughts_token_count") == 64
    assert getattr(r, "total_token_count") == 264
    assert getattr(r, "workload") == "drift"


@pytest.mark.asyncio
async def test_run_agent_emits_llm_usage_log(caplog, drift_workload_env):
    """Parallel coverage for run_agent (the /recheck path). The
    llm_usage block in run_agent is identical to run_chat's today; this
    test pins it so a future edit can't drop one without breaking
    the suite. Reuses the same _stub_run because run_agent and
    run_chat consume the event stream identically up to the
    final-text branch."""
    caplog.set_level(logging.INFO, logger="driftscribe.agent.adk_agent")
    workload_token = set_workload("drift")
    try:
        with patch.object(adk_agent, "Runner") as runner_cls:
            runner_cls.return_value.run_async = _stub_run
            proposal = await adk_agent.run_agent("hi", workload="drift", autonomy_mode="propose_apply")
    finally:
        reset_workload(workload_token)

    # Sanity: run_agent's parse path didn't break despite the thought
    # part being interleaved with the JSON.
    assert proposal is not None
    assert getattr(proposal, "action", None) is not None

    usage = [
        r for r in caplog.records
        if getattr(r, "event", None) == "llm_usage"
    ]
    assert len(usage) == 1, f"expected exactly 1 llm_usage record, got {len(usage)}"
    r = usage[0]
    assert getattr(r, "prompt_token_count") == 120
    assert getattr(r, "thoughts_token_count") == 64
    assert getattr(r, "workload") == "drift"
