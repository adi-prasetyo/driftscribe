"""StateStore transactions behind the crew handoff (slice 1).

Run against ``InMemoryStateStore``. The Firestore implementation mirrors the
same shape inside one ``@firestore.transactional`` — the parity test at the
bottom pins that both stores expose the identical surface, because a method
that exists on only one of them fails in production and nowhere else.
"""
import datetime as dt

import pytest

from agent.handoff import (
    build_pending_handoff,
    handoff_nonce_digest,
    mint_handoff_nonce,
    validate_handoff_proposal,
)
from agent.state_store import InMemoryStateStore

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def store():
    return InMemoryStateStore()


def _proposal(to="drift", frm="explore"):
    return validate_handoff_proposal(
        target=to, reason="fixing this needs a rollback",
        brief="ORDER_TIMEOUT is 90s; the contract pins 30s.",
        current_workload=frm,
    )


def _turns(workload="explore"):
    return [
        {"role": "user", "text": "the timeout looks wrong", "workload": workload},
        {"role": "crew", "text": "it is drifted", "workload": workload},
    ]


def _propose(store, cid, *, to="drift", frm="explore", now=NOW, create=True):
    """Commit a turn pair + a fresh proposal; return the nonce."""
    nonce, digest = mint_handoff_nonce()
    store.append_turns(
        cid, _turns(frm),
        create_with={"workload": frm, "title": "t"} if create else None,
        pending_handoff=build_pending_handoff(
            _proposal(to=to, frm=frm), digest=digest, now=now,
        ),
    )
    return nonce


# --- proposal commits with the turn pair -----------------------------------

def test_proposal_and_first_turns_commit_together_on_a_new_conversation():
    """The case the obvious design cannot handle: on turn 1 the conversation
    does not exist yet (creation is lazy), so a tool that wrote the proposal
    itself would have nothing to write to."""
    store = InMemoryStateStore()
    _propose(store, "c1")
    conv = store.get_conversation("c1")
    assert conv is not None
    assert len(conv["turns"]) == 2
    assert conv["pending_handoff"]["to"] == "drift"


def test_a_conversation_without_a_proposal_has_no_pending_field(store):
    store.append_turns(
        "c1", _turns(), create_with={"workload": "explore", "title": "t"},
    )
    assert not store.get_conversation("c1").get("pending_handoff")


def test_a_new_proposal_supersedes_and_burns_the_previous_one(store):
    """Both proposals come from Explore, so ``pending.from == workload`` does
    NOT catch this — superseding is the only thing that closes the replay."""
    first = _propose(store, "c1")
    second = _propose(store, "c1", create=False)

    assert store.redeem_handoff(
        "c1", nonce=first, accept=True, now=NOW,
    )["error"] == "invalid_nonce"
    assert store.redeem_handoff("c1", nonce=second, accept=True, now=NOW)["ok"]


def test_an_ordinary_turn_leaves_an_open_proposal_alone(store):
    """Typing instead of clicking must not silently destroy the chip — the
    operator can still confirm until the 15-minute window closes."""
    nonce = _propose(store, "c1")
    store.append_turns("c1", _turns())
    assert store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)["ok"]


# --- redemption ------------------------------------------------------------

def test_accepting_flips_the_crew_and_appends_a_transition_turn(store):
    nonce = _propose(store, "c1")
    out = store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)

    assert out["ok"] is True
    assert out["pending"]["to"] == "drift"
    conv = store.get_conversation("c1")
    assert conv["workload"] == "drift"
    assert not conv.get("pending_handoff")
    last = conv["turns"][-1]
    assert last["role"] == "crew_change"
    assert last["workload"] == "drift"
    assert last["handoff"] == {"from": "explore", "to": "drift"}


def test_a_nonce_is_single_use(store):
    nonce = _propose(store, "c1")
    assert store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)["ok"]
    again = store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)
    assert again["ok"] is False
    assert again["error"] == "no_pending"


def test_declining_burns_the_nonce_without_moving_the_crew(store):
    """Decline has to POST too. Without it the crew re-proposes every turn and
    no amount of prompt restraint can stop it."""
    nonce = _propose(store, "c1")
    out = store.redeem_handoff("c1", nonce=nonce, accept=False, now=NOW)

    assert out["ok"] is True
    conv = store.get_conversation("c1")
    assert conv["workload"] == "explore"
    assert not conv.get("pending_handoff")
    assert conv["turns"][-1]["role"] == "handoff_declined"
    assert store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=NOW,
    )["error"] == "no_pending"


def test_a_wrong_nonce_is_refused_and_leaves_the_proposal_intact(store):
    """A failed guess must not burn the operator's real chip."""
    nonce = _propose(store, "c1")
    other, _ = mint_handoff_nonce()

    assert store.redeem_handoff(
        "c1", nonce=other, accept=True, now=NOW,
    )["error"] == "invalid_nonce"
    assert store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)["ok"]


def test_an_expired_proposal_is_refused(store):
    nonce = _propose(store, "c1")
    out = store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=NOW + dt.timedelta(minutes=15),
    )
    assert out["error"] == "expired"
    assert store.get_conversation("c1")["workload"] == "explore"


def test_a_proposal_whose_origin_no_longer_matches_the_crew_is_refused(store):
    """The conversation moved on by some other path; a proposal made FROM a
    crew that is no longer driving describes a route that no longer exists."""
    nonce = _propose(store, "c1")
    conv_workload_moved = _propose(store, "c1", create=False, to="upgrade")
    store.redeem_handoff("c1", nonce=conv_workload_moved, accept=True, now=NOW)
    # Thread is now with upgrade; the original explore->drift nonce is dead
    # twice over (superseded AND stale), and must not resurrect.
    assert store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=NOW,
    )["ok"] is False


def test_redeeming_an_unknown_conversation_is_refused(store):
    nonce, _ = mint_handoff_nonce()
    assert store.redeem_handoff(
        "nope", nonce=nonce, accept=True, now=NOW,
    )["error"] == "not_found"


def test_redeeming_a_conversation_with_no_proposal_is_refused(store):
    store.append_turns(
        "c1", _turns(), create_with={"workload": "explore", "title": "t"},
    )
    nonce, _ = mint_handoff_nonce()
    assert store.redeem_handoff(
        "c1", nonce=nonce, accept=True, now=NOW,
    )["error"] == "no_pending"


# --- run lease -------------------------------------------------------------

def test_a_run_lease_blocks_a_second_run_until_released(store):
    store.append_turns(
        "c1", _turns(), create_with={"workload": "explore", "title": "t"},
    )
    assert store.begin_chat_run("c1", run_id="a", now=NOW) is True
    assert store.begin_chat_run("c1", run_id="b", now=NOW) is False
    store.finish_chat_run("c1", run_id="a")
    assert store.begin_chat_run("c1", run_id="b", now=NOW) is True


def test_a_stale_lease_expires_so_a_crashed_run_cannot_wedge_a_thread(store):
    store.append_turns(
        "c1", _turns(), create_with={"workload": "explore", "title": "t"},
    )
    assert store.begin_chat_run("c1", run_id="a", now=NOW) is True
    later = NOW + dt.timedelta(hours=1)
    assert store.begin_chat_run("c1", run_id="b", now=later) is True


def test_finishing_someone_elses_lease_is_a_no_op(store):
    store.append_turns(
        "c1", _turns(), create_with={"workload": "explore", "title": "t"},
    )
    store.begin_chat_run("c1", run_id="a", now=NOW)
    store.finish_chat_run("c1", run_id="b")
    assert store.begin_chat_run("c1", run_id="c", now=NOW) is False


def test_a_conversation_that_does_not_exist_yet_can_always_start_a_run(store):
    """Turn 1 has no document to lease — and nothing to interleave with, since
    a conversation with no doc has no proposal either."""
    assert store.begin_chat_run("brand-new", run_id="a", now=NOW) is True


def test_redemption_is_refused_while_a_run_holds_the_lease(store):
    """The 409 crew-lock is a point-in-time check, not a lock: /chat validates
    the workload, returns a StreamingResponse, and only then runs the agent —
    persisting with the CAPTURED workload. Without this the transcript can
    attribute a turn to a crew that was not driving. That is audit integrity,
    not capability escalation, but a system whose pitch is a trustworthy
    decision record cannot ship a transcript that misattributes."""
    nonce = _propose(store, "c1")
    store.begin_chat_run("c1", run_id="a", now=NOW)

    out = store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)
    assert out["error"] == "busy"
    assert store.get_conversation("c1")["workload"] == "explore"

    store.finish_chat_run("c1", run_id="a")
    assert store.redeem_handoff("c1", nonce=nonce, accept=True, now=NOW)["ok"]


# --- store parity ----------------------------------------------------------

def test_both_stores_expose_the_same_handoff_surface():
    from agent.state_store import FirestoreStateStore, StateStore

    for name in (
        "begin_chat_run", "finish_chat_run", "redeem_handoff",
    ):
        assert hasattr(InMemoryStateStore, name), f"InMemory missing {name}"
        assert hasattr(FirestoreStateStore, name), f"Firestore missing {name}"
        assert hasattr(StateStore, name), f"protocol missing {name}"


def test_the_stored_digest_is_never_the_nonce(store):
    nonce = _propose(store, "c1")
    pending = store.get_conversation("c1")["pending_handoff"]
    assert nonce not in str(pending)
    assert pending["nonce_digest"] == handoff_nonce_digest(nonce)
