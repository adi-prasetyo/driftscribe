"""Unit tests for agent/pause.py and the StateStore pause methods.

TDD Step 1 — these tests must fail before implementation, then pass after.
Tests cover the four spec-mandated cases plus defensive-copy semantics.
"""

from agent.pause import FAIL_CLOSED_REASON, PauseState, read_pause_state
from agent.state_store import InMemoryStateStore


# ---------------------------------------------------------------------------
# InMemoryStateStore pause round-trip (spec §Task-1 Step-1 verbatim)
# ---------------------------------------------------------------------------


def test_inmemory_pause_round_trip():
    s = InMemoryStateStore()
    assert s.get_pause() is None
    doc = s.set_pause(paused=True, reason="drill", actor="operator-token")
    assert doc["paused"] is True and doc["reason"] == "drill"
    assert doc["actor"] == "operator-token" and doc["updated_at"] is not None
    assert s.get_pause() == doc
    doc2 = s.set_pause(paused=False, reason=None, actor="operator-token")
    assert doc2["paused"] is False and s.get_pause()["paused"] is False


# ---------------------------------------------------------------------------
# read_pause_state — absent doc means system is running
# ---------------------------------------------------------------------------


def test_read_pause_state_absent_doc_means_running():
    st = read_pause_state(InMemoryStateStore())
    assert st == PauseState(paused=False)


# ---------------------------------------------------------------------------
# read_pause_state — paused doc is faithfully mapped
# ---------------------------------------------------------------------------


def test_read_pause_state_paused_doc():
    s = InMemoryStateStore()
    s.set_pause(paused=True, reason="drill", actor="a@b.c")
    st = read_pause_state(s)
    assert st.paused is True and st.reason == "drill" and st.actor == "a@b.c"
    assert st.read_error is False


# ---------------------------------------------------------------------------
# read_pause_state — any storage error triggers fail-closed (spec verbatim)
# ---------------------------------------------------------------------------


def test_read_pause_state_fail_closed_on_store_error():
    class Boom:
        def get_pause(self):
            raise RuntimeError("firestore down")

    st = read_pause_state(Boom())
    assert st.paused is True and st.read_error is True
    # Pin the constant, not mere truthiness — operator-facing surfaces show
    # this string verbatim, so a silent rewording must fail a test.
    assert st.reason == FAIL_CLOSED_REASON


# ---------------------------------------------------------------------------
# Defensive-copy semantics: mutations to returned/passed dicts don't alias
# ---------------------------------------------------------------------------


def test_get_pause_returns_defensive_copy():
    """Mutating the returned doc must not corrupt the stored state."""
    s = InMemoryStateStore()
    s.set_pause(paused=True, reason="original", actor="op")
    copy1 = s.get_pause()
    copy1["reason"] = "mutated"
    # The next get_pause must still return the original reason.
    assert s.get_pause()["reason"] == "original"


def test_set_pause_stores_defensive_copy():
    """Mutating the dict returned from set_pause must not corrupt the store."""
    s = InMemoryStateStore()
    doc = s.set_pause(paused=True, reason="initial", actor="op")
    doc["reason"] = "tampered"
    assert s.get_pause()["reason"] == "initial"


# ---------------------------------------------------------------------------
# read_pause_state passes through reason/actor/updated_at from stored doc
# ---------------------------------------------------------------------------


def test_read_pause_state_passes_through_fields():
    s = InMemoryStateStore()
    s.set_pause(paused=False, reason="test reason", actor="ops@example.com")
    st = read_pause_state(s)
    assert st.paused is False
    assert st.reason == "test reason"
    assert st.actor == "ops@example.com"
    assert st.updated_at is not None
    assert st.read_error is False


# ---------------------------------------------------------------------------
# FAIL_CLOSED_REASON constant must be non-empty (satisfies spec assertion)
# ---------------------------------------------------------------------------


def test_fail_closed_reason_is_nonempty():
    assert FAIL_CLOSED_REASON and isinstance(FAIL_CLOSED_REASON, str)
