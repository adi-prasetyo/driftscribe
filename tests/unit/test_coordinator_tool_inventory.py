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
    COORDINATOR_TOOLS,
    DRIFT_WORKLOAD_TOOL_NAMES,
    UPGRADE_WORKLOAD_TOOL_NAMES,
    build_agent,
)


# Hardcode the expected set. Updates to this set MUST be intentional —
# anyone bumping the list must also justify the new tool in the PR.
EXPECTED_TOOL_NAMES = frozenset({
    "read_live_env_tool",
    "propose_rollback_tool",
    "patch_docs_tool",
    "notify_tool",
    "search_recent_prs_tool",
    "load_contract_tool",
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
_DRIFT_ONLY_TOOL_NAMES = frozenset({
    "drift_read_live_env",
    "drift_patch_docs",
    "drift_propose_rollback",
})
_UPGRADE_ONLY_TOOL_NAMES = frozenset({
    "upgrade_read_dependencies",
    "upgrade_propose_pr",
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

    Why parse the YAML directly here instead of calling
    :func:`agent.workloads.load_workload`: upgrade's tools are still
    reserved-but-not-implemented (``None`` in ``TOOL_REGISTRY`` until
    17.B/17.C). A ``load_workload("upgrade")`` call today raises
    :class:`ReservedToolNotImplementedError` before producing a spec.
    Reading and validating the YAML through
    :class:`WorkloadSpec.model_validate` exercises the same schema the
    loader uses, without the tool-resolution step that fails on
    reserved-None entries.

    When 17.C flips the reserved entries to real callables, this test
    keeps working unchanged — :data:`UPGRADE_WORKLOAD_TOOL_NAMES` is the
    audit point either way.
    """
    from agent.workloads.spec import WorkloadSpec

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


# Union of every symbolic tool name any workload references. Parametrize-id
# uses the name itself so a failure points directly at the offender.
_ALL_WORKLOAD_TOOL_NAMES = sorted(
    set(DRIFT_WORKLOAD_TOOL_NAMES) | set(UPGRADE_WORKLOAD_TOOL_NAMES)
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
    tools, the session-state tools) are intentionally NOT in either
    "*_ONLY" set — they're allowed in both workloads by design, so the
    disjointness pin doesn't penalize them.
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


@pytest.mark.parametrize("tool", COORDINATOR_TOOLS, ids=lambda t: t.__name__)
def test_no_tool_has_dangerous_parameter_name(tool):
    """No tool's parameter list may contain names that hint at a
    capability wider than the tool's stated purpose.

    Defense-in-depth: complements ``test_no_tool_has_dangerous_name``.
    A tool named ``read_live_env_tool`` is fine, but if it accepted a
    ``url`` parameter the LLM could in principle widen the capability
    through the argument. This test catches that at registration time.

    Current 6-tool registry has no such parameters — the test pins the
    property as the registry grows.
    """
    sig = inspect.signature(tool)
    for param_name in sig.parameters:
        assert not _DANGEROUS_PARAM_RE.search(param_name), (
            f"Tool {tool.__name__!r} has parameter {param_name!r} matching "
            f"a dangerous-parameter pattern. Even if the tool is safe today, "
            f"a parameter named this way invites future widening of "
            f"capability through the argument. Rename, or reconsider "
            f"whether this argument needs to exist."
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

    # Safe params (every parameter name currently used by COORDINATOR_TOOLS)
    # MUST NOT match. If this list grows, add the new name here.
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
    # Force re-import in a clean module cache subset
    for mod_name in [
        "agent.adk_agent",
        "agent.adk_tools",
        "agent.worker_client",
    ]:
        sys.modules.pop(mod_name, None)
    import agent.adk_agent  # noqa: F401  - reimport for the side-effect check

    # subprocess is everywhere in the stdlib; just check we didn't yank
    # in something more obviously dangerous (paramiko, fabric, etc.).
    for forbidden in ("paramiko", "fabric", "pexpect"):
        assert forbidden not in sys.modules, (
            f"Importing agent.adk_agent pulled in {forbidden!r}, which "
            f"suggests a tool was added that shells out to remote hosts."
        )
