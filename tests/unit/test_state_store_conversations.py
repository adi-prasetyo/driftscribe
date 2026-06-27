"""Conversation persistence on the StateStore (P1 multi-turn chat)."""
from datetime import datetime, timezone

import pytest

from agent.state_store import InMemoryStateStore


def _store():
    return InMemoryStateStore()


def test_create_then_get_conversation_round_trips():
    s = _store()
    s.create_conversation("c1", workload="drift", title="why is svc drifting")
    conv = s.get_conversation("c1")
    assert conv is not None
    assert conv["conversation_id"] == "c1"
    assert conv["workload"] == "drift"
    assert conv["title"] == "why is svc drifting"
    assert conv["turn_count"] == 0
    assert conv["turns"] == []
    assert isinstance(conv["created_at"], datetime)


def test_get_unknown_conversation_returns_none():
    assert _store().get_conversation("nope") is None


def test_append_turn_allocates_monotonic_seq_and_orders_turns():
    s = _store()
    s.create_conversation("c1", workload="drift", title="t")
    seq0 = s.append_turn("c1", role="user", text="hello", workload="drift",
                         trace_id="tr-1")
    seq1 = s.append_turn("c1", role="crew", text="hi there", workload="drift",
                         trace_id="tr-1", tool_calls=["read_live_env_tool"])
    assert seq0 == 0 and seq1 == 1
    conv = s.get_conversation("c1")
    assert conv["turn_count"] == 2
    assert [t["seq"] for t in conv["turns"]] == [0, 1]
    assert [t["role"] for t in conv["turns"]] == ["user", "crew"]
    assert conv["turns"][1]["tool_calls"] == ["read_live_env_tool"]
    assert conv["last_trace_id"] == "tr-1"


def test_append_turn_records_iac_pr_on_crew_turn_only():
    s = _store()
    s.create_conversation("c1", workload="provision", title="t")
    s.append_turn("c1", role="user", text="adopt bucket", workload="provision")
    s.append_turn("c1", role="crew", text="opened PR", workload="provision",
                  trace_id="tr", iac_pr={"pr_number": 5, "pr_url": "https://x/5"})
    turns = s.get_conversation("c1")["turns"]
    assert "iac_pr" not in turns[0]
    assert turns[1]["iac_pr"] == {"pr_number": 5, "pr_url": "https://x/5"}


def test_append_turn_unknown_conversation_raises():
    with pytest.raises(KeyError):
        _store().append_turn("ghost", role="user", text="x", workload="drift")


def test_append_turns_pair_is_atomic_and_creates_on_demand():
    s = _store()
    seqs = s.append_turns(
        "c1",
        [
            {"role": "user", "text": "q", "workload": "drift", "trace_id": "tr"},
            {"role": "crew", "text": "a", "workload": "drift", "trace_id": "tr",
             "tool_calls": ["x"]},
        ],
        create_with={"workload": "drift", "title": "q"},
    )
    assert seqs == [0, 1]
    conv = s.get_conversation("c1")
    assert conv["title"] == "q"
    assert conv["turn_count"] == 2
    assert conv["last_trace_id"] == "tr"
    assert [t["role"] for t in conv["turns"]] == ["user", "crew"]


def test_append_turns_without_create_with_on_missing_raises():
    with pytest.raises(KeyError):
        _store().append_turns("ghost", [{"role": "user", "text": "x",
                                         "workload": "drift"}])


def test_list_conversations_newest_first_and_limited():
    s = _store()
    for i in range(3):
        s.create_conversation(f"c{i}", workload="drift", title=f"t{i}")
        s.append_turn(f"c{i}", role="user", text="x", workload="drift")
    rows = s.list_conversations(limit=2)
    assert len(rows) == 2
    # most-recently-updated first; c2 was created/updated last
    assert rows[0]["conversation_id"] == "c2"
    # list rows are metadata only — no embedded turns
    assert "turns" not in rows[0]


def test_list_conversations_filters_by_workload():
    s = _store()
    s.create_conversation("d", workload="drift", title="t")
    s.create_conversation("p", workload="provision", title="t")
    rows = s.list_conversations(workload="provision")
    assert [r["conversation_id"] for r in rows] == ["p"]
