"""Layer 0: capability-bounded tool registry enforcement.

The coordinator's ADK agent is allowed to call ONLY the tools in
``agent.adk_agent.COORDINATOR_TOOLS``. This test pins the exact set so
that adding a new tool requires an intentional edit here — preventing
a careless "let me add a quick helper" PR from silently widening the
LLM's authority.

The test also enforces a negative-name pattern: no tool may have a name
suggesting a dangerous capability (shell, exec, subprocess, delete, etc.),
even if it would otherwise pass the positive list (defense-in-depth
against a future PR that simultaneously updates the expected set AND
adds something it shouldn't).

See ``docs/architecture/multi-agent-design.md`` §"Layer 0".
"""
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
