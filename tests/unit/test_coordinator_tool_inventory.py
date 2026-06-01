"""Layer 0: capability-bounded tool registry enforcement.

The coordinator's ADK agent is allowed to call ONLY the tools in
``agent.adk_agent.COORDINATOR_TOOLS``. This test pins the exact set so
that adding a new tool requires an intentional edit here — preventing
a careless "let me add a quick helper" PR from silently widening the
LLM's authority.

Phase 17.A.2: a second assertion pins the *drift workload's* symbolic
tool list (``workloads/drift/workload.yaml::enabled_tool_names``) so
that adding a tool to the drift workload's YAML — or removing one —
also requires an intentional edit here. The two assertions are paired:
``COORDINATOR_TOOLS`` is the registry of Python callables the LLM may
invoke; the drift workload's ``enabled_tool_names`` is the symbolic
filter applied per workload (17.A.3 will hand the LLM only the tools
intersecting both). Both must change in lockstep when capability
genuinely widens.

Phase 17.A.4: extends the workload-symbolic pinning to cover *every*
workload (drift, upgrade) and adds three new invariants:

1. Each workload's ``enabled_tool_names`` matches a hardcoded tuple
   (order included — see the tool-order pin below).
2. A narrower negative regex (the one named in the Phase 17 plan)
   applies to every workload's symbolic tool names *and* the Python
   callable names in ``COORDINATOR_TOOLS``.
3. A cross-workload disjointness pin asserts no drift-only tool name
   appears in the upgrade workload, and vice versa. Shared tools
   (``notify``, ``search_recent_prs``) are allowed in both — the
   disjointness only applies to the workload-prefixed names.
4. A tool-order pin asserts the ``Agent.tools`` order at build time
   matches the YAML's ``enabled_tool_names`` order, so a silent YAML
   reorder doesn't degrade the LLM's tool-selection prompt construction.

The test also enforces a negative-name pattern: no tool may have a name
suggesting a dangerous capability (shell, exec, subprocess, delete, etc.),
even if it would otherwise pass the positive list (defense-in-depth
against a future PR that simultaneously updates the expected set AND
adds something it shouldn't).

A third test (Phase 13 carry-over from Codex 11.7 review) extends the
same logic to *parameter names*: a safely-named tool that accepts a
``cmd`` / ``url`` / ``payload`` / ``raw_request`` argument could let the
LLM widen capability through the argument rather than the tool name.

See ``docs/architecture/multi-agent-design.md`` §"Layer 0".
"""
import inspect
import re
from pathlib import Path

import pytest
import yaml

# Read the canonical lists from the source of truth.
from agent.adk_agent import (
    CHAT_ONLY_TOOL_NAMES,
    COORDINATOR_TOOLS,
    DRIFT_WORKLOAD_TOOL_NAMES,
    EXPLORE_WORKLOAD_TOOL_NAMES,
    PROVISION_WORKLOAD_TOOL_NAMES,
    UPGRADE_WORKLOAD_TOOL_NAMES,
    build_agent,
    build_chat_agent,
)
from agent.fanout import MUTATION_TOOL_NAMES
from agent.workloads.spec import WorkloadSpec


# Hardcode the expected set. Updates to this set MUST be intentional —
# anyone bumping the list must also justify the new tool in the PR.
EXPECTED_TOOL_NAMES = frozenset({
    "read_live_env_tool",
    "propose_rollback_tool",
    "patch_docs_tool",
    "notify_tool",
    "search_recent_prs_tool",
    "load_contract_tool",
    # Phase 17.B.3 — Developer Knowledge MCP wrappers (async callables
    # in ``agent.mcp.developer_knowledge``). Their symbolic names in
    # the workload YAML match the callable names 1:1 — no underscore_tool
    # suffix because they're not drift-specific function-tool wrappers.
    "search_developer_docs",
    "retrieve_developer_doc",
    # Phase 17.C.4 — Upgrade workload tools. Authority-clean LLM-facing
    # surface: ``upgrade_read_dependencies_tool`` takes no args, and
    # ``upgrade_propose_pr_tool`` derives ``target_repo`` /
    # ``lockfile_path`` / ``branch`` / ``base`` / ``title`` server-side
    # from ``UPGRADE_TARGET_REGISTRY``. See
    # ``agent.adk_tools.upgrade_read_dependencies_tool`` /
    # ``agent.adk_tools.upgrade_propose_pr_tool`` for the
    # routing-fields-server-side rationale.
    "upgrade_read_dependencies_tool",
    "upgrade_propose_pr_tool",
    # Upgrade PR close — withdraw an upgrade PR this workload opened.
    # Authority-clean (pr_number + reason only); the worker gates on
    # driftscribe-label + upgrade/ branch + main base.
    "upgrade_close_pr_tool",
    # Upgrade PR merge — merge an upgrade PR this workload opened.
    # Authority-clean (pr_number only); the worker gates on the same
    # provenance triple PLUS fail-closed CI (required check green + no
    # conflict) and merges with a deploy-pinned squash.
    "upgrade_merge_pr_tool",
    # Infra-IaC read-only inventory — whole-project resource describe via
    # the infra_reader worker (cloudasset.viewer only). Authority-clean
    # (no args). Strictly read-only: exposed by the chat-only ``explore``
    # workload and intentionally absent from ``_MUTATION_TOOL_NAMES``.
    "read_project_inventory_tool",
    # Phase D2 — Provision workload: author OpenTofu (IaC) edits and open
    # ONE iac/-only PR via the tofu-editor worker. Authority-clean (the LLM
    # supplies only the file writes + PR title/body; routing fields derived
    # server-side). UNLIKE the read tools, this is a MUTATION tool — its
    # symbolic name ``provision_open_infra_pr`` IS in ``_MUTATION_TOOL_NAMES``
    # below. Callable ``__name__`` is ``open_infra_pr_tool``.
    "open_infra_pr_tool",
})


# Phase 17.A.4: workload-prefixed name sets used by the cross-workload
# disjointness test. "Drift-only" means tools whose authority is bound
# to drift's domain (Cloud Run env reading + drift-shaped mutations);
# "upgrade-only" means tools bound to upgrade's domain (lockfile reading +
# upgrade-shaped mutations). Shared tools (``notify``, ``search_recent_prs``,
# the MCP doc-search tools, the session-state tools) are NOT in either
# set — they're allowed in both workloads by design.
#
# Hardcoded here (not derived) for the same reason ``EXPECTED_TOOL_NAMES``
# is hardcoded: the test is the audit point. A future PR that adds a
# tool to either set MUST update this constant, which means a reviewer
# sees the change as part of capability widening.
#
# Note: the session-state tools (``get_session_state`` /
# ``set_session_state``) were briefly listed in upgrade's enabled set
# during the 17.A.1/17.B.3 prep but were never bound to a workload's
# authority domain. The 17.B.4 follow-up removed them from upgrade's
# YAML; they remain reserved in ``_TOOL_REGISTRY`` for the future
# session-memory feature but are absent from both ``*_ONLY`` sets here.
_DRIFT_ONLY_TOOL_NAMES = frozenset({
    "drift_read_live_env",
    "drift_patch_docs",
    "drift_propose_rollback",
})
_UPGRADE_ONLY_TOOL_NAMES = frozenset({
    "upgrade_read_dependencies",
    "upgrade_propose_pr",
    "upgrade_close_pr",
    "upgrade_merge_pr",
})

# The read-only invariant for the explore workload. Every symbolic tool
# name that EITHER mutates a system OR rides a write-capable credential.
# ``explore`` (chat-only, strictly read-only) MUST be disjoint from this
# set — that is the load-bearing guarantee behind the "Explore (read-only)"
# label. Note ``search_recent_prs`` is here NOT because it writes (its code
# only reads) but because it rides the coordinator's write-capable GitHub
# PAT; credential containment is part of "strictly read-only" (Codex review
# 2026-05-25). ``notify`` is a side-effect (posts a webhook), so it counts
# as a mutation for this purpose too.
#
# Phase D5-3: the canonical set was promoted to ``agent.fanout`` (as
# ``MUTATION_TOOL_NAMES``) so the fan-out slice-author resolution and THIS
# audit pin share ONE source of truth — the runtime trust filter
# (``resolve_provision_read_tools`` strips these from a slice sub-agent's
# tool set) and the read-only/mutation disjointness assertions below cannot
# drift apart. This module is still the audit point: it imports the set and
# asserts the invariants on it; a name added to the set is reviewed here.
_MUTATION_TOOL_NAMES = MUTATION_TOOL_NAMES

# Mutation WORKERS no read-only workload may wire in. This is a secondary,
# manifest-level guard (the primary read-only guarantee is the tool-set
# disjointness above, since tools call workers by hardcoded name through
# worker_client, not via the manifest's worker_names). Still worth pinning:
# a manifest that listed a mutation worker would at minimum be misleading,
# and would fail-load loudly if its URL env var were unset.
_MUTATION_WORKER_NAMES = frozenset({
    "drift_docs",
    "drift_rollback",
    "upgrade_docs",
    # Phase D2 — the tofu-editor worker commits validated iac/-only file
    # writes and opens ONE PR (the provision workload's write surface). It
    # holds a write-capable GitHub editor PAT, so it is a mutation worker.
    "tofu_editor",
})


# Negative-name pattern: catch names suggesting dangerous capabilities.
# This is intentionally broad — false positives are fine (rename the tool)
# but false negatives are a security gap.
_DANGEROUS_NAME_RE = re.compile(
    r"shell|exec|subprocess|os[_-]?command|delete|drop|destroy|sudo|raw[_-]?http|arbitrary|run[_-]?command|eval",
    re.IGNORECASE,
)


# Phase 17.A.4: a narrower negative regex specifically targeting the
# patterns named in the Phase 17 plan (§17.A.4 step 2). Distinct from
# the broader ``_DANGEROUS_NAME_RE`` above on purpose:
#
# - ``_DANGEROUS_NAME_RE`` is the historical strict guard over the
#   Python *callable* names in ``COORDINATOR_TOOLS``. Adding patterns
#   to it (drop/destroy/eval/...) widens the guard for callable names.
# - ``_WORKLOAD_DANGEROUS_NAME_RE`` is the Phase 17 guard over the
#   *symbolic* names every workload YAML references. Kept identical to
#   the plan's specified pattern so reviewers can grep for it.
#
# Both regexes are applied to every name they cover — they're additive,
# not alternatives. A tool name that matches either fails inventory.
_WORKLOAD_DANGEROUS_NAME_RE = re.compile(
    r"shell|exec|subprocess|os_command|delete|sudo|raw_http|arbitrary",
    re.IGNORECASE,
)


def test_coordinator_tools_match_expected_set():
    """The set of registered tools must equal the expected set, exactly.

    Failure mode 1: a tool was added without updating EXPECTED_TOOL_NAMES.
    → Discuss the addition; if intentional, add it here.

    Failure mode 2: a tool was renamed or removed.
    → Update EXPECTED_TOOL_NAMES; verify the rename is intentional.
    """
    actual = {t.__name__ for t in COORDINATOR_TOOLS}
    assert actual == EXPECTED_TOOL_NAMES, (
        f"Coordinator tool inventory drifted.\n"
        f"  Expected: {sorted(EXPECTED_TOOL_NAMES)}\n"
        f"  Actual:   {sorted(actual)}\n"
        f"  Added:    {sorted(actual - EXPECTED_TOOL_NAMES)}\n"
        f"  Removed:  {sorted(EXPECTED_TOOL_NAMES - actual)}\n"
        f"If this change is intentional, update EXPECTED_TOOL_NAMES in "
        f"this test AND update the Layer 0 section in "
        f"docs/architecture/multi-agent-design.md."
    )


def test_drift_workload_enabled_tools_match_expected_set(drift_workload_env):
    """Phase 17.A.2 / 17.A.4: the drift workload's enabled_tool_names must
    equal :data:`DRIFT_WORKLOAD_TOOL_NAMES` exactly — same names, same order.

    Failure modes match :func:`test_coordinator_tools_match_expected_set`
    one layer up: the symbolic-name list in ``workloads/drift/workload.yaml``
    is the per-workload capability filter; ``COORDINATOR_TOOLS`` is the
    Python callable registry. Both must change in lockstep when
    capability genuinely widens.

    Phase 17.A.4 tightened this from a set-equality to an ordered list
    equality so a silent YAML reorder is caught — see
    :func:`test_drift_workload_tool_order_pin` for why order matters.

    The ``drift_workload_env`` fixture (defined in ``tests/conftest.py``)
    sets the four worker URL env vars so ``load_workload`` succeeds
    without depending on the deploy env, and clears the workload cache
    on both sides of the test.
    """
    from agent.workloads import load_workload
    resolution = load_workload("drift")
    actual = tuple(resolution.spec.enabled_tool_names)

    assert actual == DRIFT_WORKLOAD_TOOL_NAMES, (
        f"Drift workload tool inventory drifted.\n"
        f"  Expected (ordered): {list(DRIFT_WORKLOAD_TOOL_NAMES)}\n"
        f"  Actual   (ordered): {list(actual)}\n"
        f"  Added:    {sorted(set(actual) - set(DRIFT_WORKLOAD_TOOL_NAMES))}\n"
        f"  Removed:  {sorted(set(DRIFT_WORKLOAD_TOOL_NAMES) - set(actual))}\n"
        f"If this change is intentional, update both the YAML at "
        f"workloads/drift/workload.yaml AND DRIFT_WORKLOAD_TOOL_NAMES in "
        f"agent/adk_agent.py."
    )


def test_upgrade_workload_enabled_tools_match_expected_set():
    """Phase 17.A.4: the upgrade workload's enabled_tool_names must equal
    :data:`UPGRADE_WORKLOAD_TOOL_NAMES` exactly — same names, same order.

    Phase 17.C.4 follow-up: the previous version of this test parsed
    the YAML directly because upgrade's tools were reserved
    (``None`` in TOOL_REGISTRY). 17.C.4 flipped both
    ``upgrade_read_dependencies`` and ``upgrade_propose_pr`` to real
    callables, so ``load_workload("upgrade")`` now succeeds — but we
    still parse the YAML directly here because this test's audit point
    is the YAML ⇄ constant equality, and that doesn't depend on the
    UPGRADE_READER_URL / UPGRADE_DOCS_URL env vars being set. The
    full-resolution path is covered by
    ``tests/unit/test_upgrade_workload_loads.py``.
    """
    yaml_path = (
        Path(__file__).resolve().parents[2]
        / "workloads"
        / "upgrade"
        / "workload.yaml"
    )
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkloadSpec.model_validate(raw)
    actual = tuple(spec.enabled_tool_names)

    assert actual == UPGRADE_WORKLOAD_TOOL_NAMES, (
        f"Upgrade workload tool inventory drifted.\n"
        f"  Expected (ordered): {list(UPGRADE_WORKLOAD_TOOL_NAMES)}\n"
        f"  Actual   (ordered): {list(actual)}\n"
        f"  Added:    {sorted(set(actual) - set(UPGRADE_WORKLOAD_TOOL_NAMES))}\n"
        f"  Removed:  {sorted(set(UPGRADE_WORKLOAD_TOOL_NAMES) - set(actual))}\n"
        f"If this change is intentional, update both the YAML at "
        f"workloads/upgrade/workload.yaml AND UPGRADE_WORKLOAD_TOOL_NAMES "
        f"in agent/adk_agent.py."
    )


def _load_explore_spec() -> WorkloadSpec:
    """Parse ``workloads/explore/workload.yaml`` directly (no env needed).

    Like the upgrade enabled-tools test, the audit point here is the
    YAML ⇄ constant equality, which doesn't depend on the read-worker
    URL env vars being set.
    """
    yaml_path = (
        Path(__file__).resolve().parents[2]
        / "workloads"
        / "explore"
        / "workload.yaml"
    )
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return WorkloadSpec.model_validate(raw)


def test_explore_workload_enabled_tools_match_expected_set():
    """The explore workload's enabled_tool_names must equal
    :data:`EXPLORE_WORKLOAD_TOOL_NAMES` exactly — same names, same order.
    Mirrors the drift/upgrade inventory pins (three-way YAML ⇄ code
    constant ⇄ runtime equality)."""
    actual = tuple(_load_explore_spec().enabled_tool_names)

    assert actual == EXPLORE_WORKLOAD_TOOL_NAMES, (
        f"Explore workload tool inventory drifted.\n"
        f"  Expected (ordered): {list(EXPLORE_WORKLOAD_TOOL_NAMES)}\n"
        f"  Actual   (ordered): {list(actual)}\n"
        f"  Added:    {sorted(set(actual) - set(EXPLORE_WORKLOAD_TOOL_NAMES))}\n"
        f"  Removed:  {sorted(set(EXPLORE_WORKLOAD_TOOL_NAMES) - set(actual))}\n"
        f"If this change is intentional, update both the YAML at "
        f"workloads/explore/workload.yaml AND EXPLORE_WORKLOAD_TOOL_NAMES "
        f"in agent/adk_agent.py."
    )


def test_explore_workload_is_strictly_read_only():
    """THE read-only guarantee behind the "Explore (read-only)" label:
    the explore workload exposes ZERO mutation tools.

    ``set(EXPLORE_WORKLOAD_TOOL_NAMES)`` must be disjoint from
    :data:`_MUTATION_TOOL_NAMES` (every tool that writes OR rides a
    write-capable credential). A future PR that slipped, say,
    ``upgrade_merge_pr`` or ``notify`` or ``search_recent_prs`` into
    explore's YAML — and dutifully updated EXPLORE_WORKLOAD_TOOL_NAMES to
    match — would still fail HERE, which is the point: the inventory pin
    catches the rename, this test catches the capability widening.
    """
    leaked = set(EXPLORE_WORKLOAD_TOOL_NAMES) & _MUTATION_TOOL_NAMES
    assert not leaked, (
        f"The explore workload (labelled read-only) enables mutation "
        f"tool(s) {sorted(leaked)}. Explore must expose ONLY read tools. "
        f"Remove them from workloads/explore/workload.yaml and "
        f"EXPLORE_WORKLOAD_TOOL_NAMES, or reconsider whether 'explore' is "
        f"still read-only."
    )


def _load_provision_spec() -> WorkloadSpec:
    """Parse ``workloads/provision/workload.yaml`` directly (no env needed).

    Like the explore/upgrade enabled-tools tests, the audit point here is
    the YAML ⇄ constant equality, which doesn't depend on the worker URL
    env vars being set.
    """
    yaml_path = (
        Path(__file__).resolve().parents[2]
        / "workloads"
        / "provision"
        / "workload.yaml"
    )
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return WorkloadSpec.model_validate(raw)


def test_provision_workload_enabled_tools_match_expected_set():
    """Phase D2: the provision workload's enabled_tool_names must equal
    :data:`PROVISION_WORKLOAD_TOOL_NAMES` exactly — same names, same order.
    Mirrors the drift/upgrade/explore inventory pins (three-way YAML ⇄ code
    constant ⇄ runtime equality)."""
    actual = tuple(_load_provision_spec().enabled_tool_names)

    assert actual == PROVISION_WORKLOAD_TOOL_NAMES, (
        f"Provision workload tool inventory drifted.\n"
        f"  Expected (ordered): {list(PROVISION_WORKLOAD_TOOL_NAMES)}\n"
        f"  Actual   (ordered): {list(actual)}\n"
        f"  Added:    {sorted(set(actual) - set(PROVISION_WORKLOAD_TOOL_NAMES))}\n"
        f"  Removed:  {sorted(set(PROVISION_WORKLOAD_TOOL_NAMES) - set(actual))}\n"
        f"If this change is intentional, update both the YAML at "
        f"workloads/provision/workload.yaml AND PROVISION_WORKLOAD_TOOL_NAMES "
        f"in agent/adk_agent.py."
    )


def test_provision_workload_carries_mutation_tool_explore_stays_read_only():
    """Phase D2: the two chat-only workloads diverge on read-only-ness.

    ``explore`` is strictly read-only — disjoint from
    :data:`_MUTATION_TOOL_NAMES` (pinned in
    :func:`test_explore_workload_is_strictly_read_only`). ``provision``
    intentionally carries ONE mutation tool (``provision_open_infra_pr``),
    so it is deliberately NOT asserted read-only and is NOT added to any
    read-only-disjointness check.

    Three pins:

    1. ``provision_open_infra_pr`` is NOT in ``EXPLORE_WORKLOAD_TOOL_NAMES``
       (explore must never gain the IaC-authoring tool).
    2. ``provision_open_infra_pr`` IS in ``PROVISION_WORKLOAD_TOOL_NAMES``
       (provision intentionally carries it).
    3. ``provision_open_infra_pr`` IS in ``_MUTATION_TOOL_NAMES`` — i.e. it
       is recognized as a mutation tool, which is why provision is exempt
       from the read-only disjointness assertion explore must satisfy.
    """
    assert "provision_open_infra_pr" not in EXPLORE_WORKLOAD_TOOL_NAMES, (
        "explore (strictly read-only) must NOT carry the IaC-authoring "
        "mutation tool provision_open_infra_pr."
    )
    assert "provision_open_infra_pr" in PROVISION_WORKLOAD_TOOL_NAMES, (
        "provision must carry its IaC-authoring tool provision_open_infra_pr."
    )
    assert "provision_open_infra_pr" in _MUTATION_TOOL_NAMES, (
        "provision_open_infra_pr opens a PR via a write-capable editor PAT "
        "and must be classified as a mutation tool — which is precisely why "
        "provision (unlike explore) is NOT asserted read-only."
    )


def test_explore_workload_wires_no_mutation_worker():
    """Secondary manifest-level guard: explore lists no mutation worker.

    The primary read-only guarantee is the tool-set disjointness above
    (tools call workers by hardcoded name through worker_client, not via
    the manifest's worker_names). But a manifest that listed
    ``drift_docs`` / ``drift_rollback`` / ``upgrade_docs`` would be
    misleading at best, so pin its absence too.
    """
    worker_names = set(_load_explore_spec().worker_names)
    leaked = worker_names & _MUTATION_WORKER_NAMES
    assert not leaked, (
        f"The explore workload wires mutation worker(s) {sorted(leaked)} "
        f"in its worker_names. A strictly read-only workload must list "
        f"only read workers (drift_reader / upgrade_reader). Remove them "
        f"from workloads/explore/workload.yaml."
    )


# Union of every symbolic tool name any workload references. Parametrize-id
# uses the name itself so a failure points directly at the offender.
_ALL_WORKLOAD_TOOL_NAMES = sorted(
    set(DRIFT_WORKLOAD_TOOL_NAMES)
    | set(UPGRADE_WORKLOAD_TOOL_NAMES)
    | set(EXPLORE_WORKLOAD_TOOL_NAMES)
    | set(PROVISION_WORKLOAD_TOOL_NAMES)
)


@pytest.mark.parametrize("symbolic_name", _ALL_WORKLOAD_TOOL_NAMES)
def test_no_workload_symbolic_tool_has_dangerous_name(symbolic_name):
    """Phase 17.A.4 step 2 (symbolic side): every workload-referenced
    symbolic tool name must NOT match the plan's dangerous-capability
    regex.

    Distinct from :func:`test_no_tool_has_dangerous_name` above, which
    guards the Python *callable* names in ``COORDINATOR_TOOLS``. This
    one guards the *symbolic* names exposed by the workload YAMLs —
    the layer the LLM's prompt actually sees post-17.A.3.

    The regex is intentionally the narrower one named in the Phase 17
    plan (``shell|exec|subprocess|os_command|delete|sudo|raw_http|arbitrary``)
    so a grep across the codebase finds the same pattern in both the
    plan and the test.
    """
    assert not _WORKLOAD_DANGEROUS_NAME_RE.search(symbolic_name), (
        f"Workload symbolic tool name {symbolic_name!r} matches the "
        f"Phase 17 dangerous-capability pattern. Rename, or reconsider "
        f"whether any workload should reference this capability."
    )


@pytest.mark.parametrize("tool", COORDINATOR_TOOLS, ids=lambda t: t.__name__)
def test_no_coordinator_tool_callable_matches_workload_dangerous_regex(tool):
    """Phase 17.A.4 step 2 (callable side): apply the narrower Phase 17
    dangerous-capability regex to every Python callable in
    ``COORDINATOR_TOOLS`` as well.

    Additive to :func:`test_no_tool_has_dangerous_name` (which uses the
    broader historical regex). The two are intentionally redundant — the
    Phase 17 plan calls out this exact regex, and pinning it separately
    means a future loosening of the broader regex can't silently disable
    the Phase 17 guard.
    """
    name = tool.__name__
    assert not _WORKLOAD_DANGEROUS_NAME_RE.search(name), (
        f"Coordinator tool callable {name!r} matches the Phase 17 "
        f"dangerous-capability pattern. Rename, or reconsider whether "
        f"the coordinator should have this capability."
    )


def test_drift_and_upgrade_only_tool_names_are_disjoint():
    """Phase 17.A.4 step 3: cross-workload disjointness — no tool name
    bound to drift's authority domain may appear in the upgrade workload,
    and vice versa.

    Two assertions, both intentional:

    1. The two "*_ONLY" subsets are internally disjoint (sanity check
       on the constants themselves — they can't share a name).
    2. Each "*_ONLY" subset is absent from the *other* workload's full
       enabled-tool list. This is the load-bearing invariant: it catches
       a future PR that, say, adds ``drift_patch_docs`` to upgrade's
       YAML (which would mean upgrade could mutate drift's docs — a
       cross-workload authority leak).

    Shared tools (``notify``, ``search_recent_prs``, the MCP doc-search
    tools) are intentionally NOT in either "*_ONLY" set — they're
    allowed in both workloads by design, so the disjointness pin
    doesn't penalize them. The session-state tools (``get_session_state``
    / ``set_session_state``) remain reserved in ``_TOOL_REGISTRY`` but
    no workload currently enables them, so they don't appear in
    either ``*_WORKLOAD_TOOL_NAMES`` tuple either.
    """
    # Internal sanity: the two subsets don't overlap by construction.
    overlap = _DRIFT_ONLY_TOOL_NAMES & _UPGRADE_ONLY_TOOL_NAMES
    assert not overlap, (
        f"_DRIFT_ONLY_TOOL_NAMES and _UPGRADE_ONLY_TOOL_NAMES share "
        f"{sorted(overlap)}. Move shared names out of both subsets — "
        f"they belong in neither (the disjointness invariant is about "
        f"workload-bound authority, not shared infra)."
    )

    # The load-bearing invariant: each "*_ONLY" subset is absent from
    # the OTHER workload's full enabled-tool list.
    drift_set = set(DRIFT_WORKLOAD_TOOL_NAMES)
    upgrade_set = set(UPGRADE_WORKLOAD_TOOL_NAMES)

    drift_leak = _DRIFT_ONLY_TOOL_NAMES & upgrade_set
    assert not drift_leak, (
        f"Drift-only tool(s) {sorted(drift_leak)} appear in the upgrade "
        f"workload's enabled_tool_names. This is a cross-workload "
        f"authority leak: the upgrade workload would be able to invoke "
        f"a drift-domain tool. Remove it from "
        f"workloads/upgrade/workload.yaml."
    )

    upgrade_leak = _UPGRADE_ONLY_TOOL_NAMES & drift_set
    assert not upgrade_leak, (
        f"Upgrade-only tool(s) {sorted(upgrade_leak)} appear in the "
        f"drift workload's enabled_tool_names. This is a cross-workload "
        f"authority leak: the drift workload would be able to invoke "
        f"an upgrade-domain tool. Remove it from "
        f"workloads/drift/workload.yaml."
    )


def test_drift_workload_tool_order_pin(drift_workload_env):
    """Phase 17.A.4 step 4 (M-6 from the 17.A.3 Codex review): the order
    of tools handed to :class:`Agent` at build time must equal the order
    in :data:`DRIFT_WORKLOAD_TOOL_NAMES` (which mirrors the YAML).

    Why order matters: ADK passes the tool list to the LLM as part of
    its function-calling prompt construction. A silent YAML reorder
    could shift the LLM's tool-selection heuristics in ways that don't
    show up as an inventory diff (set equality ignores order). Pinning
    the order here makes every reorder an explicit, reviewed change.

    The assertion compares Python *callable* names in ``Agent.tools``
    against the expected callable names derived from the symbolic-name
    tuple via :data:`TOOL_REGISTRY`. That covers two failure modes in
    one go: the dict order from :func:`load_workload` getting scrambled,
    and ``build_agent`` somehow reordering the values.
    """
    from agent.workloads import load_workload
    from agent.workloads.registry import TOOL_REGISTRY

    resolution = load_workload("drift")
    agent = build_agent(resolution)
    actual_callable_order = [t.__name__ for t in agent.tools]

    expected_callable_order = [
        TOOL_REGISTRY[symbolic].__name__
        for symbolic in DRIFT_WORKLOAD_TOOL_NAMES
    ]

    assert actual_callable_order == expected_callable_order, (
        f"Agent.tools order does not match DRIFT_WORKLOAD_TOOL_NAMES.\n"
        f"  Expected: {expected_callable_order}\n"
        f"  Actual:   {actual_callable_order}\n"
        f"Either the YAML order, the TOOL_REGISTRY resolution, or "
        f"build_agent's tool-list construction has reordered the tools."
    )


def test_explore_workload_tool_order_pin(explore_workload_env):
    """The order of tools the explore chat agent receives must equal the
    order in :data:`EXPLORE_WORKLOAD_TOOL_NAMES` (which mirrors the YAML).

    Explore is chat-only, so it is served via :func:`build_chat_agent`
    (not :func:`build_agent`) — this also confirms the chat builder
    surfaces exactly the five read tools, in order, for a workload that
    happens to list no CHAT_ONLY_TOOL_NAMES tools to filter.
    """
    from agent.workloads import load_workload
    from agent.workloads.registry import TOOL_REGISTRY

    resolution = load_workload("explore")
    agent = build_chat_agent(resolution)
    actual_callable_order = [t.__name__ for t in agent.tools]

    expected_callable_order = [
        TOOL_REGISTRY[symbolic].__name__
        for symbolic in EXPLORE_WORKLOAD_TOOL_NAMES
    ]

    assert actual_callable_order == expected_callable_order, (
        f"Explore chat agent tool order does not match "
        f"EXPLORE_WORKLOAD_TOOL_NAMES.\n"
        f"  Expected: {expected_callable_order}\n"
        f"  Actual:   {actual_callable_order}"
    )


def test_close_and_merge_pr_are_chat_only_not_exposed_to_autonomous_recheck(
    upgrade_workload_env,
):
    """``upgrade_close_pr`` and ``upgrade_merge_pr`` must reach the
    interactive /chat agent but NOT the autonomous /recheck agent.

    Closing AND merging a PR are operator-driven, availability-affecting
    mutations — merge especially, since it writes to ``main``. The
    /recheck path runs without a human in the loop, so handing it either
    tool would make a destructive PR mutation gated only by prompt
    discipline (Codex review 2026-05-25). ``build_agent`` (/recheck)
    filters :data:`CHAT_ONLY_TOOL_NAMES` out by symbolic name;
    ``build_chat_agent`` keeps them. The worker-side gate (provenance +
    fail-closed CI for merge) is the other half of the defense — this
    test pins the routing half so a future refactor can't silently widen
    the autonomous surface.
    """
    from agent.workloads import load_workload

    assert "upgrade_close_pr" in CHAT_ONLY_TOOL_NAMES
    assert "upgrade_merge_pr" in CHAT_ONLY_TOOL_NAMES

    resolution = load_workload("upgrade")
    chat_tools = {t.__name__ for t in build_chat_agent(resolution).tools}
    recheck_tools = {t.__name__ for t in build_agent(resolution).tools}

    for chat_only in ("upgrade_close_pr_tool", "upgrade_merge_pr_tool"):
        assert chat_only in chat_tools, (
            f"the interactive /chat agent must carry {chat_only}"
        )
        assert chat_only not in recheck_tools, (
            f"the autonomous /recheck agent must NOT carry {chat_only} — "
            "it's chat-only (CHAT_ONLY_TOOL_NAMES)."
        )
    # The non-chat-only upgrade tools are still present on /recheck.
    assert "upgrade_propose_pr_tool" in recheck_tools


@pytest.mark.parametrize("tool", COORDINATOR_TOOLS, ids=lambda t: t.__name__)
def test_no_tool_has_dangerous_name(tool):
    """No tool name may match a dangerous-capability pattern.

    Defense-in-depth: the positive list (above) requires updating to
    add a tool; this test additionally enforces that even an intentional
    addition can't slip a dangerous-sounding name through. If you need
    to add a tool whose name matches this pattern, push back hard on
    the design first — chances are the tool itself is misconceived.
    """
    name = tool.__name__
    assert not _DANGEROUS_NAME_RE.search(name), (
        f"Tool name {name!r} matches dangerous-capability pattern. "
        f"Even if intentional, rename to something safer-sounding (or "
        f"reconsider whether the coordinator should have this capability)."
    )


# Negative-parameter-name pattern: catch parameter names that hint at a
# capability wider than the tool's stated purpose. Phase 13 carry-over
# from the Phase 11.9 Codex review — a tool named ``read_live_env_tool``
# is fine, but if it accepted a ``url`` argument the LLM could in
# principle widen the capability through that argument.
#
# Boundary choice: we use ``(?<![a-z])token(?![a-z])`` (with IGNORECASE)
# rather than ``\btoken\b`` because Python's ``\b`` doesn't fire on the
# underscore-to-letter transition (``_`` is a word character). We DO want
# ``shell_cmd``, ``target_url``, and ``raw_request`` to match — but we
# do NOT want ``expression`` to match ``expr``, or ``formula`` to match
# anything. The lookarounds-on-letters approach gives exactly that:
# ``_`` is a non-letter so it acts as a separator, but adjacent letters
# don't. See ``test_dangerous_param_regex_smoke_test`` below for the
# positive/negative anchors.
_DANGEROUS_PARAM_RE = re.compile(
    r"(?<![a-z])(?:cmd|command|shell[_-]?cmd|url|endpoint|raw[_-]?url|"
    r"payload|raw[_-]?request|script|eval|expr)(?![a-z])",
    re.IGNORECASE,
)


# Phase 17.C.4: ``upgrade_propose_pr_tool.advisory_url`` is a deliberate
# exception. The LLM picks the URL (it's the decision content — which
# GHSA advisory the bump addresses); the Upgrade Docs Worker's
# :data:`workers.upgrade_docs.validator._GHSA_ADVISORY_RE` validator
# enforces the URL shape (``^https://github.com/advisories/GHSA-...$``)
# at request time, so the LLM cannot use this parameter to redirect the
# coordinator at an arbitrary URL. The exemption is narrow on purpose —
# any other URL-shaped param must rename to avoid the regex.
_DANGEROUS_PARAM_NAME_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset({
    ("upgrade_propose_pr_tool", "advisory_url"),
})


@pytest.mark.parametrize("tool", COORDINATOR_TOOLS, ids=lambda t: t.__name__)
def test_no_tool_has_dangerous_parameter_name(tool):
    """No tool's parameter list may contain names that hint at a
    capability wider than the tool's stated purpose.

    Defense-in-depth: complements ``test_no_tool_has_dangerous_name``.
    A tool named ``read_live_env_tool`` is fine, but if it accepted a
    ``url`` parameter the LLM could in principle widen the capability
    through the argument. This test catches that at registration time.

    Phase 17.C.4 exemption: ``upgrade_propose_pr_tool.advisory_url`` is
    the only URL-shaped param in the registry. It's allowed because the
    Upgrade Docs Worker enforces a strict GHSA URL regex on the value
    (see :data:`workers.upgrade_docs.validator._GHSA_ADVISORY_RE`) — the
    LLM cannot widen capability through that argument because the
    worker rejects anything that isn't a github.com/advisories/GHSA-…
    URL. Any new URL-shaped param MUST come with a similar worker-side
    validator AND a new entry in
    :data:`_DANGEROUS_PARAM_NAME_EXCEPTIONS` above.
    """
    sig = inspect.signature(tool)
    for param_name in sig.parameters:
        if (tool.__name__, param_name) in _DANGEROUS_PARAM_NAME_EXCEPTIONS:
            continue
        assert not _DANGEROUS_PARAM_RE.search(param_name), (
            f"Tool {tool.__name__!r} has parameter {param_name!r} matching "
            f"a dangerous-parameter pattern. Even if the tool is safe today, "
            f"a parameter named this way invites future widening of "
            f"capability through the argument. Rename, or reconsider "
            f"whether this argument needs to exist. (If the worker enforces "
            f"a strict regex on the value, add an entry to "
            f"_DANGEROUS_PARAM_NAME_EXCEPTIONS with that justification.)"
        )


def test_dangerous_param_regex_smoke_test():
    """Verify the regex actually catches the patterns it's designed for.

    Without this, the parametrized test above could silently pass against
    a regex bug (e.g., a missing alternation, an escaped metachar, or a
    boundary that's too loose/too strict). The positive cases lock in
    the patterns we mean to catch; the negative cases lock in the
    safe parameter names the current registry actually uses, so a
    future tightening of the regex can't silently break them.
    """
    # Dangerous patterns MUST match (bare and with prefix/suffix).
    assert _DANGEROUS_PARAM_RE.search("cmd")
    assert _DANGEROUS_PARAM_RE.search("command")
    assert _DANGEROUS_PARAM_RE.search("shell_cmd")
    assert _DANGEROUS_PARAM_RE.search("shell-cmd")
    assert _DANGEROUS_PARAM_RE.search("url")
    assert _DANGEROUS_PARAM_RE.search("target_url")
    assert _DANGEROUS_PARAM_RE.search("raw_url")
    assert _DANGEROUS_PARAM_RE.search("endpoint")
    assert _DANGEROUS_PARAM_RE.search("payload")
    assert _DANGEROUS_PARAM_RE.search("raw_request")
    assert _DANGEROUS_PARAM_RE.search("script")
    assert _DANGEROUS_PARAM_RE.search("eval")
    assert _DANGEROUS_PARAM_RE.search("expr")
    # Case-insensitivity.
    assert _DANGEROUS_PARAM_RE.search("CMD")
    assert _DANGEROUS_PARAM_RE.search("Target_URL")

    # Safe params (every parameter name currently used by COORDINATOR_TOOLS
    # MUST NOT match — except ``advisory_url``, which is the Phase 17.C.4
    # GHSA-validated exception covered by
    # :data:`_DANGEROUS_PARAM_NAME_EXCEPTIONS`). If this list grows, add
    # the new name here.
    for safe in (
        "target_revision",
        "reason",
        "file_path",
        "new_content",
        "title",
        "body",
        "channel",
        "severity",
        "keywords",
        "days",
        # Phase 17.B.3 — Developer Knowledge MCP wrapper params.
        "query",
        "name",
        # Phase 17.C.4 — Upgrade workload tool params (safe-named ones).
        "package_name",
        "target_version",
        # Upgrade PR close tool param.
        "pr_number",
    ):
        assert not _DANGEROUS_PARAM_RE.search(safe), (
            f"Regex unexpectedly matched safe parameter name {safe!r}. "
            f"Loosen the regex or rename the offending pattern."
        )

    # Adjacency check: don't false-match on words that merely contain
    # a banned token as a letter-run substring.
    for adjacent in ("expression", "evaluator", "scripted", "formula"):
        assert not _DANGEROUS_PARAM_RE.search(adjacent), (
            f"Regex false-matched on adjacent word {adjacent!r}; "
            f"tighten the boundary."
        )


def test_adk_agent_imports_cleanly_without_pulling_dangerous_sdks():
    """Smoke test: importing the coordinator's brain doesn't yank in
    subprocess, os.system, or similar via a top-level side effect.

    This is a coarse check — Python's stdlib often loads ``subprocess``
    transitively for unrelated reasons, so we whitelist that one and
    just verify the LLM-relevant tool surface stays clean.
    """
    import sys
    # Save original module objects so we can restore them after the
    # re-import probe. Other tests (e.g. integration tests that do
    # `from agent import adk_agent` at collection time and then
    # `patch.object(adk_agent, "Runner")`) hold references to the
    # ORIGINAL module object — and `agent/main.py:chat` lazy-imports
    # `from agent.adk_agent import run_chat` at request time, which
    # would resolve to the NEW module's globals (unpatched Runner) and
    # try to spin up a real Gemini client without an API key. Restoring
    # sys.modules at the end of this test keeps those references valid.
    target_mods = ("agent.adk_agent", "agent.adk_tools", "agent.worker_client")
    saved = {m: sys.modules[m] for m in target_mods if m in sys.modules}
    try:
        for mod_name in target_mods:
            sys.modules.pop(mod_name, None)
        import agent.adk_agent  # noqa: F401  - reimport for the side-effect check

        # subprocess is everywhere in the stdlib; just check we didn't yank
        # in something more obviously dangerous (paramiko, fabric, etc.).
        for forbidden in ("paramiko", "fabric", "pexpect"):
            assert forbidden not in sys.modules, (
                f"Importing agent.adk_agent pulled in {forbidden!r}, which "
                f"suggests a tool was added that shells out to remote hosts."
            )
    finally:
        for mod_name, mod_obj in saved.items():
            sys.modules[mod_name] = mod_obj
