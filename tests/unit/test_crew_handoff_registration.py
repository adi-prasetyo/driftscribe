"""Registration pins for ``request_crew_handoff`` (slice 1).

Adding one tool touches nine surfaces in lockstep. Two of them are load-bearing
in a way that is easy to miss, and each gets its own named test below:

- ``CHAT_ONLY_TOOL_NAMES`` — ``build_agent`` strips ONLY names in that set, so
  omitting it hands the autonomous ``/recheck`` Anchor a handoff tool with no
  operator on the other end to confirm anything.
- the fan-out exclusion — anything not filtered out is handed to Provision's
  slice sub-agents, which run without the operator-facing guards.
"""
import pathlib

import yaml

from agent.adk_agent import (
    CHAT_ONLY_TOOL_NAMES,
    COORDINATOR_TOOLS,
    DRIFT_WORKLOAD_TOOL_NAMES,
    EXPLORE_WORKLOAD_TOOL_NAMES,
    PROVISION_WORKLOAD_TOOL_NAMES,
    UPGRADE_WORKLOAD_TOOL_NAMES,
)
from agent.fanout import (
    CONTROL_PLANE_PROPOSAL_TOOL_NAMES,
    MUTATION_TOOL_NAMES,
)
from agent.workloads.registry import TOOL_REGISTRY, TOOL_TIERS

SYMBOLIC = "request_crew_handoff"
CALLABLE = "request_crew_handoff_tool"

_REPO = pathlib.Path(__file__).resolve().parents[2]


def _manifest(name: str) -> dict:
    return yaml.safe_load((_REPO / "workloads" / name / "workload.yaml").read_text())


def test_registered_as_a_callable_under_the_symbolic_name():
    assert TOOL_REGISTRY[SYMBOLIC] is not None
    assert TOOL_REGISTRY[SYMBOLIC].__name__ == CALLABLE


def test_present_in_the_coordinator_registration_manifest():
    assert CALLABLE in {t.__name__ for t in COORDINATOR_TOOLS}


def test_tiered_report_so_the_door_still_opens_in_observe_mode():
    """Observe means "look, don't act" — and the handoff itself acts on nothing
    external. The crew it hands off TO has its own propose/apply tools stripped
    by the same dial, so nothing escalates. Tiering this ``propose`` would
    instead make the single door a dead end in Observe, with no picker left to
    fall back to once slice 3 removes it."""
    assert TOOL_TIERS[SYMBOLIC] == "report"


def test_enabled_on_all_four_crews_in_code_and_yaml():
    """Every crew can hand off, including back to Explore — a dead end that
    only moves one hop later is still a dead end."""
    for name, tuple_ in (
        ("drift", DRIFT_WORKLOAD_TOOL_NAMES),
        ("upgrade", UPGRADE_WORKLOAD_TOOL_NAMES),
        ("explore", EXPLORE_WORKLOAD_TOOL_NAMES),
        ("provision", PROVISION_WORKLOAD_TOOL_NAMES),
    ):
        assert SYMBOLIC in tuple_, f"{name} code tuple missing {SYMBOLIC}"
        assert SYMBOLIC in _manifest(name)["enabled_tool_names"], (
            f"{name}/workload.yaml missing {SYMBOLIC}"
        )


def test_chat_only_so_autonomous_recheck_never_gets_it():
    """``/recheck`` is Eventarc-triggered Anchor. There is no operator in that
    loop to confirm a transition, and no conversation to transition."""
    assert SYMBOLIC in CHAT_ONLY_TOOL_NAMES


def test_build_agent_strips_it_from_the_recheck_surface(drift_workload_env):
    """The pin above states intent; this proves the runtime honors it."""
    from agent.adk_agent import build_agent
    from agent.workloads import load_workload

    agent = build_agent(load_workload("drift"), autonomy_mode="propose_apply")
    assert CALLABLE not in {t.__name__ for t in agent.tools}


def test_stripped_from_provision_fanout_slice_agents(provision_workload_env):
    """Slice sub-agents author HCL text only. Their prompts carry none of the
    operator-facing guards, and there is no chip surface in a sub-agent run —
    a proposal made there could never be confirmed, only leaked into the
    parent's tool log."""
    from agent.fanout import resolve_provision_read_tools

    assert SYMBOLIC not in resolve_provision_read_tools()


# --- the split taxonomy ----------------------------------------------------

def test_control_plane_set_is_exactly_the_handoff_tool():
    assert CONTROL_PLANE_PROPOSAL_TOOL_NAMES == frozenset({SYMBOLIC})


def test_not_an_external_mutation_tool():
    """No PR, no rollback, no notification, no write-capable credential — so it
    does not belong in the set that keeps Explore honest."""
    assert SYMBOLIC not in MUTATION_TOOL_NAMES


def test_explore_control_plane_surface_is_exactly_the_handoff_tool():
    """Explore's manifest used to promise it "cannot change anything". It now
    writes ONE piece of coordinator state and mints a transition credential, so
    the honest pin is not "zero" — it is "exactly this one, and nothing else".
    A second control-plane tool appearing on Explore must break this test."""
    assert (
        set(EXPLORE_WORKLOAD_TOOL_NAMES) & CONTROL_PLANE_PROPOSAL_TOOL_NAMES
        == {SYMBOLIC}
    )


def test_explore_stays_disjoint_from_external_mutation():
    """Unchanged by this slice, restated here because it is the invariant the
    new tool comes closest to weakening."""
    assert not set(EXPLORE_WORKLOAD_TOOL_NAMES) & MUTATION_TOOL_NAMES


def test_no_redemption_callable_is_reachable_by_any_model():
    """The model may PROPOSE a handoff; only a separate authenticated HTTP
    request may redeem one. If a redeem/confirm callable ever lands in the
    registry, the nonce stops being a second factor and becomes decoration."""
    registered = {t.__name__ for t in COORDINATOR_TOOLS} | {
        t.__name__ for t in TOOL_REGISTRY.values() if t is not None
    }
    forbidden = {"redeem_crew_handoff", "confirm_crew_handoff", "accept_crew_handoff"}
    assert not (registered | set(TOOL_REGISTRY)) & forbidden
