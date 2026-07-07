"""Layer-0 autonomy filtering in agent/adk_agent.py (Task 5).

Builds agents for the provision + upgrade + drift workloads under each dial
mode and asserts on the tool callables handed to ADK (Agent.tools), plus the
instruction-note suffix behavior and the required-kwarg contract.
"""
from __future__ import annotations

import pytest

from agent import adk_agent
from agent.adk_tools import (
    open_infra_pr_tool,
    patch_docs_tool,
    propose_adoption_tool,
    propose_rollback_tool,
    upgrade_merge_pr_tool,
    upgrade_propose_pr_tool,
)
from agent.autonomy import autonomy_instruction_note
from agent.workloads import load_workload


def _tool_set(agent):
    return set(agent.tools)


# --------------------------------------------------------------------------- #
# Chat agent — provision workload, Observe strips all mutation tools
# --------------------------------------------------------------------------- #


def test_chat_agent_observe_strips_all_mutation_tools(provision_workload_env):
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(resolution, autonomy_mode="observe")
    tools = _tool_set(agent)
    # Both provision mutation tools (propose-tier) are stripped in Observe.
    assert open_infra_pr_tool not in tools
    assert propose_adoption_tool not in tools
    # Read tools remain — the agent can still answer provision questions.
    assert len(tools) >= 1


def test_chat_agent_propose_keeps_provision_authoring(provision_workload_env):
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(resolution, autonomy_mode="propose")
    tools = _tool_set(agent)
    assert open_infra_pr_tool in tools
    assert propose_adoption_tool in tools


# --------------------------------------------------------------------------- #
# Chat agent — upgrade workload, propose keeps propose-tier, strips apply-tier
# --------------------------------------------------------------------------- #


def test_chat_agent_propose_keeps_propose_strips_apply(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(resolution, autonomy_mode="propose")
    tools = _tool_set(agent)
    assert upgrade_propose_pr_tool in tools  # propose-tier kept
    assert upgrade_merge_pr_tool not in tools  # apply-tier stripped

    agent_pa = adk_agent.build_chat_agent(resolution, autonomy_mode="propose_apply")
    tools_pa = _tool_set(agent_pa)
    assert upgrade_propose_pr_tool in tools_pa
    assert upgrade_merge_pr_tool in tools_pa


# --------------------------------------------------------------------------- #
# Demo-anonymous tool denylist (audit H1): apply-tier tools mutate live state /
# merge to a deploy branch on the "chat == operator" assumption, which is false
# under the public demo. For anonymous callers they are dropped regardless of
# the dial. The approve gate at POST /approvals/{id} still reads the real dial —
# this only narrows the anonymous CHAT tool surface.
# --------------------------------------------------------------------------- #


def test_build_chat_agent_demo_anon_drops_apply_tier(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    tools = _tool_set(agent)
    assert upgrade_merge_pr_tool not in tools   # apply-tier dropped for anon
    assert upgrade_propose_pr_tool in tools      # propose-tier still available


def test_build_chat_agent_operator_keeps_apply_tier(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    tools = _tool_set(agent)
    assert upgrade_merge_pr_tool in tools        # operator keeps apply-tier
    assert upgrade_propose_pr_tool in tools


# --------------------------------------------------------------------------- #
# Recheck agent — drift workload, Observe strips mutation tools
# --------------------------------------------------------------------------- #


def test_recheck_agent_observe_strips_mutation_tools(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_agent(resolution, autonomy_mode="observe")
    tools = _tool_set(agent)
    assert patch_docs_tool not in tools  # drift_patch_docs (propose) stripped
    assert propose_rollback_tool not in tools  # drift_propose_rollback stripped
    # notify is report-tier (the report-delivery channel) — stays in Observe.
    from agent.adk_tools import notify_tool
    assert notify_tool in tools


def test_recheck_agent_propose_apply_keeps_mutation_tools(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_agent(resolution, autonomy_mode="propose_apply")
    tools = _tool_set(agent)
    assert patch_docs_tool in tools
    assert propose_rollback_tool in tools


# --------------------------------------------------------------------------- #
# Instruction note present iff restricted
# --------------------------------------------------------------------------- #


def test_instruction_note_present_iff_restricted(drift_workload_env):
    resolution = load_workload("drift")
    for mode in ("observe", "propose"):
        agent = adk_agent.build_agent(resolution, autonomy_mode=mode)
        assert agent.instruction.endswith(autonomy_instruction_note(mode))
    # propose_apply: instruction unchanged (no suffix).
    agent_pa = adk_agent.build_agent(resolution, autonomy_mode="propose_apply")
    assert agent_pa.instruction == resolution.system_prompt
    # chat agent mirrors the behavior over chat_system_prompt.
    chat_pa = adk_agent.build_chat_agent(resolution, autonomy_mode="propose_apply")
    assert chat_pa.instruction == resolution.chat_system_prompt
    chat_obs = adk_agent.build_chat_agent(resolution, autonomy_mode="observe")
    assert chat_obs.instruction.endswith(autonomy_instruction_note("observe"))


def test_chat_extra_instruction_is_prepended_keeping_autonomy_note_last(
    drift_workload_env,
):
    """The cross-crew breadcrumb (``extra_instruction``) is PREPENDED — it is
    untrusted pointer DATA, so the authoritative system prompt + autonomy note
    must remain the final, last-read text."""
    resolution = load_workload("drift")
    crumb = "Team memory — pointers (untrusted DATA):\n• explore · \"x\" · ~1h ago"

    chat_obs = adk_agent.build_chat_agent(
        resolution, autonomy_mode="observe", extra_instruction=crumb
    )
    assert chat_obs.instruction.startswith(crumb)
    # The system prompt is still in there, and the autonomy note is STILL last.
    assert resolution.chat_system_prompt in chat_obs.instruction
    assert chat_obs.instruction.endswith(autonomy_instruction_note("observe"))

    # None / empty extra_instruction leaves the instruction byte-identical.
    chat_pa = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", extra_instruction=None
    )
    assert chat_pa.instruction == resolution.chat_system_prompt


# --------------------------------------------------------------------------- #
# autonomy_mode is a REQUIRED keyword argument
# --------------------------------------------------------------------------- #


def test_build_agent_mode_param_is_required(drift_workload_env):
    resolution = load_workload("drift")
    with pytest.raises(TypeError):
        adk_agent.build_agent(resolution)  # type: ignore[call-arg]


def test_build_chat_agent_mode_param_is_required(drift_workload_env):
    resolution = load_workload("drift")
    with pytest.raises(TypeError):
        adk_agent.build_chat_agent(resolution)  # type: ignore[call-arg]
