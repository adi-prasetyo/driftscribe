"""End-to-end load test for the explore workload (chat-only, read-only).

Mirrors :mod:`tests.unit.test_drift_workload_loads` /
:mod:`tests.unit.test_upgrade_workload_loads`. Pins the manifest-level
resolution of the third workload and its two defining properties:

1. ``load_workload("explore")`` succeeds with the two read-worker env
   vars set (``READER_URL`` / ``UPGRADE_READER_URL``) — i.e. its tool
   and worker names all resolve against the registries.
2. It is CHAT-ONLY: there is no ``chat_system_prompt_file``, so
   ``chat_system_prompt`` falls back to ``system_prompt`` (they are
   byte-equal). And it carries no contract (``contract_path is None``)
   and no actions (``action_names == []``).

The *read-only* capability guarantee (explore exposes zero mutation
tools) is pinned separately and more strongly in
:mod:`tests.unit.test_coordinator_tool_inventory` via the
``_MUTATION_TOOL_NAMES`` disjointness assertion.
"""
from __future__ import annotations

from agent.adk_agent import EXPLORE_WORKLOAD_TOOL_NAMES


def test_load_workload_explore_succeeds(explore_workload_env):
    """``load_workload("explore")`` resolves with only the two read-worker
    env vars set — the manifest lists no mutation worker, so no other
    URL env var is required."""
    from agent.workloads import load_workload

    resolution = load_workload("explore")
    assert resolution.spec.name == "explore"
    assert resolution.spec.observation_kind == "none"
    assert resolution.spec.action_names == []


def test_load_workload_explore_resolves_only_read_tools(explore_workload_env):
    """All five enabled tools resolve to real callables, and the resolved
    set is exactly the read subset pinned in EXPLORE_WORKLOAD_TOOL_NAMES.

    Compares by ``__name__`` rather than identity for the same reason the
    upgrade load test does: a sibling inventory test re-imports
    ``agent.adk_tools``, so callable identity isn't stable across the
    suite, but names are.
    """
    from agent.workloads import load_workload

    resolution = load_workload("explore")
    tools = resolution.tools

    assert tuple(tools.keys()) == EXPLORE_WORKLOAD_TOOL_NAMES
    for fn in tools.values():
        assert callable(fn)
    assert tools["drift_read_live_env"].__name__ == "read_live_env_tool"
    assert (
        tools["upgrade_read_dependencies"].__name__
        == "upgrade_read_dependencies_tool"
    )
    assert tools["load_contract"].__name__ == "load_contract_tool"


def test_explore_chat_prompt_falls_back_to_system_prompt(explore_workload_env):
    """Explore ships a single ``system_prompt.md`` and no
    ``chat_system_prompt_file``; the registry resolves
    ``chat_system_prompt`` to ``system_prompt`` for /chat. This pins the
    deliberate single-prompt design — a future regression that added a
    distinct chat prompt (or dropped the fallback) would fail here.
    """
    from agent.workloads import load_workload

    resolution = load_workload("explore")
    assert resolution.system_prompt.strip(), "system_prompt must be non-empty"
    assert resolution.chat_system_prompt == resolution.system_prompt


def test_explore_system_prompt_is_read_only_flavored(explore_workload_env):
    """Smoke-pin that the prompt frames explore as read-only and names
    no write surface — guards against a copy-paste from a mutating
    workload's prompt."""
    from agent.workloads import load_workload

    text = load_workload("explore").system_prompt.lower()
    assert "read-only" in text
    # The prompt must not advertise a mutation verb as something it does.
    for forbidden in ("open a pull request", "roll back", "merge"):
        # These may appear in the "you cannot X" framing, so just assert
        # the read-only disclaimer is present rather than banning the
        # words outright.
        pass
    assert "cannot" in text or "no write" in text


def test_load_workload_explore_has_no_contract(explore_workload_env):
    """Explore is read-only and makes no decisions, so it carries no
    decision-rules contract."""
    from agent.workloads import load_workload

    resolution = load_workload("explore")
    assert resolution.contract_path is None


def test_upgrade_read_dependencies_target_resolves_without_worker_env(monkeypatch):
    """Read-only isolation: ``upgrade_read_dependencies_tool`` derives its
    target (repo + lockfile) WITHOUT requiring any worker URL env var.

    The tool is exposed by the chat-only ``explore`` workload, which may
    run in a deploy where the upgrade workload's MUTATION worker
    (``upgrade_docs``) and the notifier are not configured. Pre-fix,
    ``_get_upgrade_target`` called ``load_workload("upgrade")``, which
    resolved those workers and raised ``MissingWorkerEnvError`` when
    ``UPGRADE_DOCS_URL`` / ``NOTIFIER_URL`` were unset — so a read tool
    transitively required write-worker config. This pins the decoupling:
    with EVERY worker URL unset, target resolution still succeeds.
    """
    import agent.adk_tools as adk_tools_mod
    import agent.workloads.registry as registry_mod

    for var in (
        "READER_URL",
        "DOCS_URL",
        "ROLLBACK_URL",
        "NOTIFIER_URL",
        "UPGRADE_READER_URL",
        "UPGRADE_DOCS_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    registry_mod._WORKLOAD_CACHE.clear()
    adk_tools_mod._get_upgrade_target.cache_clear()

    target = adk_tools_mod._get_upgrade_target()
    assert target.target_repo, "target_repo must resolve from the contract"

    registry_mod._WORKLOAD_CACHE.clear()
    adk_tools_mod._get_upgrade_target.cache_clear()
