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
    .limit(1).stream()``. Future refactors must not accidentally drop
    ``.limit(1)`` (would page across the whole decisions collection)
    or change the field name (would silently miss every document)."""
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
    snap.to_dict.return_value = {"action": "drift_issue", "trace_id": "a" * 32}
    mock_query.stream.return_value = iter([snap])

    store = FirestoreStateStore(project="p", client=mock_db)
    out = store.find_decision_by_trace_id("a" * 32)

    assert out == {"action": "drift_issue", "trace_id": "a" * 32}
    mock_decisions.where.assert_called_once_with("trace_id", "==", "a" * 32)
    mock_query.limit.assert_called_once_with(1)
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
