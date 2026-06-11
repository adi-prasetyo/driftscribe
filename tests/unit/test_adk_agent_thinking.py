"""Pin the BuiltInPlanner wiring and the final-text-skips-thoughts fix.

These two invariants are tested together because they MUST land in the
same commit (Phase 18.B.1). If the planner is enabled without the
final-text filter, run_agent's JSON parse will swallow thought text
and produce a runtime error mid-/recheck. The tests assert both halves
of the invariant.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from google.adk.planners.built_in_planner import BuiltInPlanner
from google.genai.types import ThinkingConfig

from agent import adk_agent
from agent.workloads import load_workload
from tests.unit._adk_stubs import StubEvent as _Ev, StubPart as _P


# --- half 1: planner is wired -----------------------------------------------


def test_build_agent_has_builtin_planner_with_thoughts_enabled(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_agent(resolution, autonomy_mode="propose_apply")
    assert isinstance(agent.planner, BuiltInPlanner)
    assert isinstance(agent.planner.thinking_config, ThinkingConfig)
    assert agent.planner.thinking_config.include_thoughts is True


def test_build_chat_agent_has_builtin_planner_with_thoughts_enabled(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_chat_agent(resolution, autonomy_mode="propose_apply")
    assert isinstance(agent.planner, BuiltInPlanner)
    assert agent.planner.thinking_config.include_thoughts is True


def test_upgrade_workload_agents_also_have_thoughts_enabled(upgrade_workload_env):
    resolution = load_workload("upgrade")
    for builder in (adk_agent.build_agent, adk_agent.build_chat_agent):
        agent = builder(resolution, autonomy_mode="propose_apply")
        assert agent.planner.thinking_config.include_thoughts is True


# --- half 2: final-text collection skips thought parts ----------------------


async def _stub_run(*args, **kwargs):
    # One non-partial thought summary, then the merged final JSON.
    yield _Ev(
        [_P(text="reasoning about contract", thought=True)],
        partial=False,
    )
    yield _Ev(
        [
            _P(text="ignored-thought-text", thought=True),
            _P(
                text=(
                    '{"action":"no_op","env_diffs":[],'
                    '"rationale":"matches","confidence":0.9}'
                ),
                thought=False,
            ),
        ],
        partial=False,
        final=True,
    )


@pytest.mark.asyncio
async def test_run_chat_final_text_excludes_thought_parts(drift_workload_env):
    """Thought text MUST NOT contaminate run_chat's reply field."""
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        result = await adk_agent.run_chat("hi", workload="drift", autonomy_mode="propose_apply")
    assert "ignored-thought-text" not in result["reply"]
    assert "no_op" in result["reply"]


@pytest.mark.asyncio
async def test_run_agent_parses_final_response_when_thought_part_present(drift_workload_env):
    """The primary parse-breaking landmine is `run_agent` / `/recheck`,
    not `run_chat`. If `include_thoughts=True` ships without the
    final-text thought-skip in `run_agent`, the thought summary gets
    concatenated into the JSON blob fed to `_parse_response` and the
    parse raises, breaking the entire `/recheck` flow. This test pins
    that path: stub a final event with both a `thought=True` part and
    a valid decision-JSON part, and assert `run_agent` returns a parsed
    `DecisionProposal` instead of raising.
    """
    with patch.object(adk_agent, "Runner") as runner_cls:
        runner_cls.return_value.run_async = _stub_run
        proposal = await adk_agent.run_agent("hi", workload="drift", autonomy_mode="propose_apply")
    # Don't pin the exact class — just confirm parsing didn't blow up
    # and the rationale survived. The strong invariant is "no exception".
    assert proposal is not None
    assert getattr(proposal, "action", None) is not None
