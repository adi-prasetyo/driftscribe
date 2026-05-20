"""End-to-end load test for the upgrade workload (Phase 17.C.4).

Mirrors :mod:`tests.unit.test_drift_workload_loads` for the upgrade
workload. Pins:

1. ``load_workload("upgrade")`` succeeds — i.e. the registry's
   ``_TOOL_REGISTRY`` entries for ``upgrade_read_dependencies`` and
   ``upgrade_propose_pr`` are now real callables (no longer ``None``),
   so the resolver does NOT raise
   :class:`agent.workloads.ReservedToolNotImplementedError`.
2. ``WorkloadResolution.system_prompt`` is the /recheck prompt
   (non-empty, references the four-action decision space).
3. ``WorkloadResolution.chat_system_prompt`` is the /chat prompt
   (non-empty, distinct from system_prompt) — the Phase 17.C.4
   Option A schema split is materialised.
4. The two upgrade-only tools are resolved as the same callables the
   inventory test pins.

Note: the upgrade workload's contract.yaml is parsed elsewhere
(:mod:`tests.unit.test_upgrade_contract`); this file's scope is the
manifest-level resolution.
"""
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_load_workload_upgrade_succeeds(upgrade_workload_env):
    """Phase 17.C.4 step 4: ``load_workload("upgrade")`` no longer
    raises ``ReservedToolNotImplementedError``.

    Pre-17.C.4 the registry had ``upgrade_read_dependencies`` and
    ``upgrade_propose_pr`` pinned as ``None``, so this call failed at
    tool-resolution time. 17.C.4 flipped both to real callables; the
    resolver now produces a complete :class:`WorkloadResolution`.
    """
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    assert resolution.spec.name == "upgrade"


def test_load_workload_upgrade_resolves_real_upgrade_tools(upgrade_workload_env):
    """The two upgrade-only tools resolve to the real callables in
    :mod:`agent.adk_tools`. Pins the registry wiring landed by 17.C.4.

    Compares by ``__name__`` (not ``is`` identity) because
    :func:`tests.unit.test_coordinator_tool_inventory.test_adk_agent_imports_cleanly_without_pulling_dangerous_sdks`
    pops the ``agent.adk_tools`` module from ``sys.modules`` and
    re-imports it — a subsequent ``from agent.adk_tools import …`` in
    a later test yields fresh function objects that don't match the
    callables the registry captured at first import. The names are
    stable across the re-import; identity is not.
    """
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    tools = resolution.tools
    assert callable(tools["upgrade_read_dependencies"])
    assert callable(tools["upgrade_propose_pr"])
    assert tools["upgrade_read_dependencies"].__name__ == "upgrade_read_dependencies_tool"
    assert tools["upgrade_propose_pr"].__name__ == "upgrade_propose_pr_tool"


def test_load_workload_upgrade_system_prompt_non_empty(upgrade_workload_env):
    """The upgrade /recheck system prompt is non-empty and references
    the four-action decision space. Smoke-pin that the placeholder
    content (which used to mention session_state tools) has been
    replaced.
    """
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    text = resolution.system_prompt
    assert text.strip(), "upgrade system_prompt must be non-empty"
    # Phase 17.C.4 cleanup: stale references must be gone.
    assert "get_session_state" not in text
    assert "set_session_state" not in text
    # The four upgrade actions must be referenced (case-insensitive
    # substring — the prompt formats them with backticks).
    lower = text.lower()
    for action in ("no_op", "docs_pr", "upgrade_pr", "escalation"):
        assert action in lower, (
            f"upgrade system_prompt should mention action {action!r}"
        )


def test_load_workload_upgrade_chat_system_prompt_non_empty(
    upgrade_workload_env,
):
    """Phase 17.C.4 Option A: the upgrade workload ships a distinct
    /chat-flavored prompt that references the LLM-facing tool surface
    (``upgrade_read_dependencies_tool`` / ``upgrade_propose_pr_tool``).

    Pin both:
    - the prompt is non-empty (smoke-check the file was wired and
      loaded);
    - it differs from the /recheck system_prompt (so a future schema
      regression that silently fell back to system_prompt would fail
      this test).
    """
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    chat = resolution.chat_system_prompt
    assert chat.strip(), "upgrade chat_system_prompt must be non-empty"
    # The chat prompt is distinct from the /recheck prompt.
    assert chat != resolution.system_prompt
    # Both LLM-facing tool names must appear so the operator-facing
    # prompt names the surface accurately.
    assert "upgrade_read_dependencies_tool" in chat
    assert "upgrade_propose_pr_tool" in chat


def test_load_workload_upgrade_exposes_contract_path(upgrade_workload_env):
    """``WorkloadResolution.contract_path`` resolves to the workload-
    local ``contract.yaml``. The upgrade workload pins
    ``contract_file: contract.yaml`` in its manifest — this test
    catches a future YAML refactor that drops the field (the eager
    upgrade-contract resolve in :func:`agent.main._eager_resolve_upgrade_contract`
    depends on the path being present).
    """
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    assert resolution.contract_path is not None
    assert resolution.contract_path.is_absolute()
    assert resolution.contract_path.name == "contract.yaml"
    assert resolution.contract_path.parent.name == "upgrade"
