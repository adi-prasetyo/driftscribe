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

import pytest

# Read the canonical list from the source of truth.
from agent.adk_agent import COORDINATOR_TOOLS


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


# Phase 17.A.2: the drift workload's symbolic-name tool list — the
# subset of TOOL_REGISTRY entries the drift workload is allowed to wire
# through to the LLM. Mirrors ``workloads/drift/workload.yaml``'s
# ``enabled_tool_names`` field. Adding a tool to the drift YAML without
# updating this set (or vice versa) fails CI.
EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES = frozenset({
    "drift_read_live_env",
    "drift_patch_docs",
    "drift_propose_rollback",
    "notify",
    "load_contract",
    "search_recent_prs",
})


# Negative-name pattern: catch names suggesting dangerous capabilities.
# This is intentionally broad — false positives are fine (rename the tool)
# but false negatives are a security gap.
_DANGEROUS_NAME_RE = re.compile(
    r"shell|exec|subprocess|os[_-]?command|delete|drop|destroy|sudo|raw[_-]?http|arbitrary|run[_-]?command|eval",
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
    """Phase 17.A.2: the drift workload's enabled_tool_names set must
    equal the expected set, exactly.

    Failure modes match :func:`test_coordinator_tools_match_expected_set`
    one layer up: the symbolic-name list in ``workloads/drift/workload.yaml``
    is the per-workload capability filter; ``COORDINATOR_TOOLS`` is the
    Python callable registry. Both must change in lockstep when
    capability genuinely widens.

    The ``drift_workload_env`` fixture (defined in ``tests/conftest.py``)
    sets the four worker URL env vars so ``load_workload`` succeeds
    without depending on the deploy env, and clears the workload cache
    on both sides of the test.
    """
    from agent.workloads import load_workload
    resolution = load_workload("drift")
    actual = frozenset(resolution.spec.enabled_tool_names)

    assert actual == EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES, (
        f"Drift workload tool inventory drifted.\n"
        f"  Expected: {sorted(EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES)}\n"
        f"  Actual:   {sorted(actual)}\n"
        f"  Added:    {sorted(actual - EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES)}\n"
        f"  Removed:  {sorted(EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES - actual)}\n"
        f"If this change is intentional, update both the YAML at "
        f"workloads/drift/workload.yaml AND EXPECTED_DRIFT_WORKLOAD_TOOL_NAMES "
        f"in this test."
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
