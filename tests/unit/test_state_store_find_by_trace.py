"""Unit tests for ``StateStore.find_decision_by_trace_id`` (Phase 19.A.6).

The /trace/{trace_id} endpoint enriches the reasoning timeline with the
persisted decision so the UI can show the final action alongside the
events. Both StateStore implementations need to support a trace_id
lookup — InMemory via linear scan, Firestore via ``.where("trace_id",
"==", trace_id).limit(1).stream()``.

These tests pin:

* The InMemory hit / miss / multi-decision matching shape (used in
  every integration test and DRY_RUN demos).
* The Firestore client interaction shape (mock-based so we don't
  need GCP creds): exactly one ``.where(...).limit(1).stream()`` call
  with the documented args, and the first match is returned as a
  dict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent.state_store import FirestoreStateStore, InMemoryStateStore


# --------------------------------------------------------------------------- #
# InMemoryStateStore
# --------------------------------------------------------------------------- #


def test_find_decision_by_trace_id_returns_match():
    s = InMemoryStateStore()
    trace = "a" * 32
    s.record_event("ev-1", {})
    s.record_decision(
        "dec-1", "ev-1", {"action": "drift_issue", "trace_id": trace}
    )

    out = s.find_decision_by_trace_id(trace)
    assert out is not None
    assert out["action"] == "drift_issue"
    assert out["trace_id"] == trace


def test_find_decision_by_trace_id_returns_none_when_no_match():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision(
        "dec-1", "ev-1", {"action": "drift_issue", "trace_id": "a" * 32}
    )

    assert s.find_decision_by_trace_id("b" * 32) is None


def test_find_decision_by_trace_id_empty_store_returns_none():
    s = InMemoryStateStore()
    assert s.find_decision_by_trace_id("a" * 32) is None


def test_find_decision_by_trace_id_picks_the_correct_one_amongst_many():
    """Multi-decision: only the decision whose ``trace_id`` matches is
    returned, regardless of insertion order."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_event("ev-2", {})
    s.record_event("ev-3", {})
    s.record_decision(
        "dec-1", "ev-1", {"action": "no_op", "trace_id": "a" * 32}
    )
    s.record_decision(
        "dec-2", "ev-2", {"action": "drift_issue", "trace_id": "b" * 32}
    )
    s.record_decision(
        "dec-3", "ev-3", {"action": "rollback", "trace_id": "c" * 32}
    )

    out = s.find_decision_by_trace_id("b" * 32)
    assert out is not None
    assert out["action"] == "drift_issue"
    assert out["trace_id"] == "b" * 32


def test_find_decision_by_trace_id_skips_decisions_without_trace_id():
    """Defensive: a pre-19.A.4 decision document with no ``trace_id``
    field must not match a lookup for an empty string. Belt-and-braces
    against a future caller passing ``""`` (e.g. from a missing header
    code path that didn't mint a fresh trace_id)."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "no_op"})  # no trace_id

    assert s.find_decision_by_trace_id("") is None
    assert s.find_decision_by_trace_id("a" * 32) is None


# --------------------------------------------------------------------------- #
# FirestoreStateStore — interaction shape via mock client
# --------------------------------------------------------------------------- #


def test_firestore_find_decision_by_trace_id_uses_where_limit_stream():
    """Pin the Firestore interaction: ``.where("trace_id", "==", trace_id)
    .limit(10).stream()``. Future refactors must not accidentally drop the
    page bound (would page across the whole decisions collection) or change
    the field name (would silently miss every document). The limit bounds
    the page fetched for the client-side newest-first pick; matching docs
    per trace_id are 1-2 in practice (create-class merge path can record a
    pending → merged pair)."""
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch

    mock_query = MagicMock()
    mock_decisions.where.return_value = mock_query
    mock_query.limit.return_value = mock_query

    snap = MagicMock()
    snap.create_time = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    snap.to_dict.return_value = {"action": "drift_issue", "trace_id": "a" * 32}
    mock_query.stream.return_value = iter([snap])

    store = FirestoreStateStore(project="p", client=mock_db)
    out = store.find_decision_by_trace_id("a" * 32)

    # created_at is backfilled from snapshot.create_time (setdefault), so
    # compare it separately from the rest of the to_dict() payload.
    assert out is not None
    assert out["created_at"] == snap.create_time
    out_without_created_at = {k: v for k, v in out.items() if k != "created_at"}
    assert out_without_created_at == {"action": "drift_issue", "trace_id": "a" * 32}
    mock_decisions.where.assert_called_once_with("trace_id", "==", "a" * 32)
    mock_query.limit.assert_called_once_with(10)
    mock_query.stream.assert_called_once_with()


def test_firestore_find_decision_by_trace_id_returns_none_when_no_match():
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch

    mock_query = MagicMock()
    mock_decisions.where.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.stream.return_value = iter([])  # zero hits

    store = FirestoreStateStore(project="p", client=mock_db)
    assert store.find_decision_by_trace_id("a" * 32) is None


def test_find_decision_by_trace_id_picks_newest_when_trace_has_two_rows():
    """The create-class merge path records waiting_for_rebake pending →
    merged under ONE trace_id; the lookup must return the newest row (the
    current lifecycle stage), not an arbitrary one."""
    s = InMemoryStateStore()
    trace = "d" * 32
    s.record_event("ev-1", {})
    s.record_event("ev-2", {})
    s.record_decision(
        "dec-old", "ev-1",
        {"action": "iac_apply", "trace_id": trace, "merge_state": "pending",
         "created_at": datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)},
    )
    s.record_decision(
        "dec-new", "ev-2",
        {"action": "iac_apply", "trace_id": trace, "merge_state": "merged",
         "created_at": datetime(2026, 6, 1, 12, 0, 5, tzinfo=timezone.utc)},
    )

    out = s.find_decision_by_trace_id(trace)
    assert out is not None
    assert out["merge_state"] == "merged"


def test_find_decision_by_trace_id_missing_created_at_loses_to_present_one():
    """Sentinel branch: a decision dict missing ``created_at`` (shouldn't
    normally happen after Phase 19.A.7 — ``record_decision`` setdefaults it
    — but the newest-first sort key must tolerate it via the ``datetime.min``
    sentinel rather than raising ``TypeError`` on the ``None`` vs ``datetime``
    compare). Bypass ``record_decision`` to plant a missing-field doc
    directly, mirroring
    ``test_state_store_list.py::test_inmemory_list_decisions_missing_created_at_sorts_last``.
    """
    s = InMemoryStateStore()
    trace = "e" * 32
    s._decisions["dec-missing"] = {
        "action": "iac_apply", "trace_id": trace, "tag": "missing",
    }
    s._decisions["dec-present"] = {
        "action": "iac_apply", "trace_id": trace, "tag": "present",
        "created_at": datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc),
    }

    out = s.find_decision_by_trace_id(trace)
    assert out is not None
    assert out["tag"] == "present"


def test_firestore_find_decision_by_trace_id_newest_and_backfills_created_at():
    """Firestore: (a) among multiple snapshots the newest by server-managed
    ``snapshot.create_time`` wins; (b) a pre-19.A.7 doc without created_at
    gets it backfilled from create_time (mirrors list_decisions) so the
    /trace fetch hint works for every decision."""
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch

    mock_query = MagicMock()
    mock_decisions.where.return_value = mock_query
    mock_query.limit.return_value = mock_query

    older = MagicMock()
    older.create_time = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    older.to_dict.return_value = {"action": "iac_apply", "trace_id": "a" * 32,
                                  "merge_state": "pending"}
    newer = MagicMock()
    newer.create_time = datetime(2026, 6, 1, 12, 0, 5, tzinfo=timezone.utc)
    newer.to_dict.return_value = {"action": "iac_apply", "trace_id": "a" * 32,
                                  "merge_state": "merged"}  # no created_at field
    mock_query.stream.return_value = iter([older, newer])  # unordered on purpose

    store = FirestoreStateStore(project="p", client=mock_db)
    out = store.find_decision_by_trace_id("a" * 32)

    assert out is not None
    assert out["merge_state"] == "merged"
    assert out["created_at"] == newer.create_time  # backfilled
