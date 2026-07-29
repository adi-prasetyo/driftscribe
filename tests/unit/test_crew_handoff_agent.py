"""Agent-layer effects of a crew handoff (slice 1).

Two things change inside ``agent.adk_agent`` once a conversation can hold turns
from more than one crew:

1. Replay attribution. ``_seed_event_from_turn`` authors EVERY crew turn under
   the current agent's name, deliberately, to suppress ADK's "For context: X
   said" rewrite. Same-crew that is correct. Cross-crew it is exactly backwards:
   Explore's words would read to Anchor as its own prior output.
2. The first-turn denylist, which must be a real filter on the built agent —
   not a claim in a prompt.
"""
import pytest

from agent.adk_agent import _seed_event_from_turn
from agent.handoff import HANDOFF_FIRST_TURN_DENIED_TOOLS


def _text(event):
    return "".join(p.text or "" for p in event.content.parts)


# --- replay attribution ----------------------------------------------------

def test_user_turns_stay_authored_by_the_user():
    ev = _seed_event_from_turn(
        {"role": "user", "text": "the timeout looks wrong", "workload": "explore"},
        agent_name="driftscribe_chat_drift", current_workload="drift",
    )
    assert ev.author == "user"
    assert ev.content.role == "user"


def test_same_crew_turns_replay_as_this_agents_own_model_output():
    """Unchanged behavior — the suppression exists so a resumed conversation
    keeps role fidelity, and that is still right when nobody handed off."""
    ev = _seed_event_from_turn(
        {"role": "crew", "text": "it is drifted", "workload": "drift"},
        agent_name="driftscribe_chat_drift", current_workload="drift",
    )
    assert ev.author == "driftscribe_chat_drift"
    assert ev.content.role == "model"


def test_another_crews_turns_replay_attributed_to_that_crew():
    """The joining crew must not read Explore's findings as its own. Authoring
    under the other crew's name lets ADK apply the rewrite this code suppresses
    everywhere else — and that rewrite is precisely the attributed quotation a
    handoff wants."""
    ev = _seed_event_from_turn(
        {"role": "crew", "text": "it is drifted", "workload": "explore"},
        agent_name="driftscribe_chat_drift", current_workload="drift",
    )
    assert ev.author != "driftscribe_chat_drift"
    assert "Explore" in ev.author


def test_a_crew_turn_with_no_workload_is_treated_as_this_crews_own():
    """Conversations written before handoffs existed carry turns whose workload
    field predates any cross-crew possibility. Absent == same crew keeps every
    old thread replaying exactly as it did."""
    ev = _seed_event_from_turn(
        {"role": "crew", "text": "older turn"},
        agent_name="driftscribe_chat_drift", current_workload="drift",
    )
    assert ev.author == "driftscribe_chat_drift"


def test_transition_rows_replay_as_server_events_not_as_crew_speech():
    """``crew_change`` is written by the server, not produced by a model. It
    must not enter the transcript as anything the joining crew "said"."""
    ev = _seed_event_from_turn(
        {"role": "crew_change", "text": "fixing this needs a rollback",
         "workload": "drift", "handoff": {"from": "explore", "to": "drift"}},
        agent_name="driftscribe_chat_drift", current_workload="drift",
    )
    assert ev.author not in ("user", "driftscribe_chat_drift")
    assert "Anchor" in _text(ev)


def test_declined_transition_rows_tell_the_crew_it_was_declined():
    """Without this the crew re-proposes the same handoff every turn, and no
    prompt-level restraint can see the refusal to respect it."""
    ev = _seed_event_from_turn(
        {"role": "handoff_declined", "text": "needs a rollback",
         "workload": "explore", "handoff": {"from": "explore", "to": "drift"}},
        agent_name="driftscribe_chat_explore", current_workload="explore",
    )
    text = _text(ev).lower()
    assert "declin" in text
    assert "anchor" in text


# --- first-turn denylist ---------------------------------------------------

def test_build_chat_agent_drops_denied_tools(upgrade_workload_env):
    """Patch is the crew that has them, so Patch is where this must bite."""
    from agent.adk_agent import build_chat_agent
    from agent.workloads import load_workload

    resolution = load_workload("upgrade")
    baseline = {t.__name__ for t in build_chat_agent(
        resolution, autonomy_mode="propose_apply",
    ).tools}
    assert "upgrade_merge_pr_tool" in baseline, "precondition: normally present"

    joined = {t.__name__ for t in build_chat_agent(
        resolution, autonomy_mode="propose_apply",
        denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
    ).tools}
    assert "upgrade_merge_pr_tool" not in joined
    assert "upgrade_close_pr_tool" not in joined
    # Everything else survives — the crew still arrives able to do its job.
    assert "upgrade_propose_pr_tool" in joined


def test_denial_is_independent_of_the_demo_anon_carve_out(upgrade_workload_env):
    """The demo-anon denylist deliberately PRESERVES ``upgrade_merge_pr`` as a
    risk-accepted carve-out. That carve-out must not leak the tool back into a
    handoff turn served to an anonymous visitor."""
    from agent.adk_agent import build_chat_agent
    from agent.workloads import load_workload

    tools = {t.__name__ for t in build_chat_agent(
        load_workload("upgrade"), autonomy_mode="propose_apply",
        demo_anon=True, denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
    ).tools}
    assert "upgrade_merge_pr_tool" not in tools


@pytest.mark.parametrize("mode", ["observe", "propose", "propose_apply"])
def test_denial_holds_in_every_autonomy_mode(upgrade_workload_env, mode):
    """``propose_apply`` is the default and has no denylist of its own, so the
    handoff denial cannot be a side effect of some other filter happening to
    run first."""
    from agent.adk_agent import build_chat_agent
    from agent.workloads import load_workload

    tools = {t.__name__ for t in build_chat_agent(
        load_workload("upgrade"), autonomy_mode=mode,
        denied_tools=HANDOFF_FIRST_TURN_DENIED_TOOLS,
    ).tools}
    assert "upgrade_merge_pr_tool" not in tools
    assert "upgrade_close_pr_tool" not in tools
