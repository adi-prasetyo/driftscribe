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


def test_chat_agent_demo_anon_drops_provision_authoring_keeps_adoption(provision_workload_env):
    """M4/H2: for anonymous demo callers, free-form infra AUTHORING
    (provision_open_infra_pr) is dropped even though it is propose-tier — it
    opens unbounded LLM-authored PRs on the public judged repo + a Cloud Build
    per call. The bounded, template-generated Adopt flow (propose_adoption) stays
    so the flagship infra-panel Adopt CTA still works for judges."""
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    tools = _tool_set(agent)
    assert open_infra_pr_tool not in tools       # free-form authoring denied
    assert propose_adoption_tool in tools         # Adopt CTA preserved


def test_chat_agent_operator_keeps_provision_authoring(provision_workload_env):
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    tools = _tool_set(agent)
    assert open_infra_pr_tool in tools            # operator keeps authoring
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
# Demo-anonymous tool denylist (audit H1, PARTIALLY REVERSED 2026-07-09):
# apply-tier tools stay denied by default for anonymous callers, EXCEPT the
# risk-accepted upgrade_merge_pr carve-out (docs/plans/2026-07-09-operator-seat-
# demo-window.md) — a visitor holds the operator seat and may merge the upgrade
# PR. The approve gate at POST /approvals/{id} still reads the real dial.
# --------------------------------------------------------------------------- #


def test_build_chat_agent_demo_anon_keeps_upgrade_merge_pr(upgrade_workload_env):
    """Operator-seat reversal: the upgrade merge tool is the carve-out, so an
    anonymous Patch chat KEEPS it (a visitor can merge the upgrade PR)."""
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    tools = _tool_set(agent)
    assert upgrade_merge_pr_tool in tools        # carve-out: anon keeps it
    assert upgrade_propose_pr_tool in tools


def test_build_chat_agent_operator_keeps_apply_tier(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    tools = _tool_set(agent)
    assert upgrade_merge_pr_tool in tools        # operator keeps apply-tier
    assert upgrade_propose_pr_tool in tools


def test_chat_agent_demo_anon_upgrade_tool_set_matches_operator(upgrade_workload_env):
    """After the carve-out, upgrade's only apply-tier tool (upgrade_merge_pr) is
    allowed for anon and it drops nothing else, so the anon Patch tool set equals
    the operator's at the same dial."""
    resolution = load_workload("upgrade")
    anon = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    operator = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    assert _tool_set(anon) == _tool_set(operator)


def test_demo_anon_denied_tools_carve_out_is_exact(monkeypatch):
    """The carve-out is exactly {upgrade_merge_pr}: a NEW apply-tier tool is
    still denied for anon (fail-closed default), the carve-out is allowed, and
    the propose-tier free-form authoring denial is unchanged."""
    fake_tiers = {**adk_agent.TOOL_TIERS, "future_apply_tool": "apply"}
    monkeypatch.setattr(adk_agent, "TOOL_TIERS", fake_tiers)
    denied = adk_agent._demo_anon_denied_tools()
    assert "future_apply_tool" in denied            # unlisted apply-tier denied
    assert "upgrade_merge_pr" not in denied           # carve-out allowed
    assert "provision_open_infra_pr" in denied        # extra-deny unchanged


# --------------------------------------------------------------------------- #
# Demo-environment crew notes: all four crews get a tailored, demo_anon-gated,
# runtime-composed note (keyed by workload spec name), each appended LAST and
# with no cross-note leakage. An authenticated operator gets none of them. The
# generic _DEMO_ANON_NOTE survives only as the drop-gated fallback for a future
# crew/tool with no tailored note.
# --------------------------------------------------------------------------- #


def _assert_only_demo_note(instruction: str, own: str) -> None:
    all_notes = {
        adk_agent._EXPLORE_DEMO_ANON_NOTE,
        adk_agent._ANCHOR_DEMO_ANON_NOTE,
        adk_agent._PATCH_DEMO_ANON_NOTE,
        adk_agent._PROVISION_DEMO_ANON_NOTE,
        adk_agent._DEMO_ANON_NOTE,
    }
    assert own in instruction
    assert instruction.endswith(own)                  # note is the last-read text
    for other in all_notes - {own}:
        assert other not in instruction               # no cross-note leakage


def _assert_no_demo_notes(instruction: str) -> None:
    for note in (
        adk_agent._EXPLORE_DEMO_ANON_NOTE,
        adk_agent._ANCHOR_DEMO_ANON_NOTE,
        adk_agent._PATCH_DEMO_ANON_NOTE,
        adk_agent._PROVISION_DEMO_ANON_NOTE,
        adk_agent._DEMO_ANON_NOTE,
    ):
        assert note not in instruction


def test_demo_anon_explore_note(explore_workload_env):
    resolution = load_workload("explore")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    _assert_only_demo_note(agent.instruction, adk_agent._EXPLORE_DEMO_ANON_NOTE)


def test_demo_anon_drift_note(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    _assert_only_demo_note(agent.instruction, adk_agent._ANCHOR_DEMO_ANON_NOTE)
    # Proposing + patch stay available to anon — the note never coincides with a
    # capability loss.
    tools = _tool_set(agent)
    assert propose_rollback_tool in tools
    assert patch_docs_tool in tools


def test_demo_anon_upgrade_note(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    _assert_only_demo_note(agent.instruction, adk_agent._PATCH_DEMO_ANON_NOTE)


def test_demo_anon_provision_note(provision_workload_env):
    """Provision DROPS a tool (provision_open_infra_pr), yet its tailored note
    supersedes the generic drop-gated _DEMO_ANON_NOTE (dict lookup runs first)."""
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    _assert_only_demo_note(agent.instruction, adk_agent._PROVISION_DEMO_ANON_NOTE)


def test_operator_explore_has_no_demo_notes(explore_workload_env):
    resolution = load_workload("explore")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    _assert_no_demo_notes(agent.instruction)


def test_operator_drift_has_no_demo_notes(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    _assert_no_demo_notes(agent.instruction)


def test_operator_upgrade_has_no_demo_notes(upgrade_workload_env):
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    _assert_no_demo_notes(agent.instruction)


def test_operator_provision_has_no_demo_notes(provision_workload_env):
    resolution = load_workload("provision")
    agent = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    _assert_no_demo_notes(agent.instruction)


def test_chat_agent_demo_anon_drift_tool_set_matches_operator(drift_workload_env):
    """Drift drops nothing for anon, which is what keeps the note-dispatch dict
    lookup safe (if drift ever gains a denied tool this fails and the dispatch
    must be revisited)."""
    resolution = load_workload("drift")
    anon = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=True
    )
    operator = adk_agent.build_chat_agent(
        resolution, autonomy_mode="propose_apply", demo_anon=False
    )
    assert _tool_set(anon) == _tool_set(operator)


def test_demo_anon_note_is_last_with_breadcrumb_drift(drift_workload_env):
    resolution = load_workload("drift")
    agent = adk_agent.build_chat_agent(
        resolution,
        autonomy_mode="propose_apply",
        demo_anon=True,
        extra_instruction="BREADCRUMB-SENTINEL",
    )
    assert agent.instruction.startswith("BREADCRUMB-SENTINEL")
    assert agent.instruction.endswith(adk_agent._ANCHOR_DEMO_ANON_NOTE)


def test_demo_anon_note_is_last_with_breadcrumb_upgrade(upgrade_workload_env):
    """The new dict-dispatch path keeps the crew note last even with a prepended
    breadcrumb (Patch/upgrade covered too)."""
    resolution = load_workload("upgrade")
    agent = adk_agent.build_chat_agent(
        resolution,
        autonomy_mode="propose_apply",
        demo_anon=True,
        extra_instruction="BREADCRUMB-SENTINEL",
    )
    assert agent.instruction.startswith("BREADCRUMB-SENTINEL")
    assert agent.instruction.endswith(adk_agent._PATCH_DEMO_ANON_NOTE)


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
