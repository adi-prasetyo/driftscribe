from unittest.mock import MagicMock

from agent.state_store import FirestoreStateStore, InMemoryStateStore


def test_record_event_first_call_returns_true():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {"trigger": "manual"}) is True


def test_record_event_duplicate_returns_false():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {"trigger": "manual"}) is True
    assert s.record_event("ev-1", {"trigger": "manual"}) is False


def test_find_decision_for_event_before_decision_recorded_returns_none():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    assert s.find_decision_for_event("ev-1") is None


def test_record_decision_cross_references_event():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    assert s.find_decision_for_event("ev-1") == {"action": "drift_issue"}


def test_get_decision_returns_recorded_decision():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    assert s.get_decision("dec-1") == {"action": "drift_issue"}


def test_get_decision_for_unknown_id_returns_none():
    s = InMemoryStateStore()
    assert s.get_decision("missing") is None


def test_find_decision_for_unknown_event_returns_none():
    s = InMemoryStateStore()
    assert s.find_decision_for_event("missing") is None


def test_release_event_allows_re_claim():
    s = InMemoryStateStore()
    assert s.record_event("ev-1", {}) is True
    s.release_event("ev-1")
    assert s.record_event("ev-1", {}) is True


def test_release_event_is_noop_for_unknown_key():
    s = InMemoryStateStore()
    s.release_event("never-claimed")  # must not raise


# --------------------------------------------------------------------------- #
# evict_cached_decision — Phase 14 compare-and-delete (CAS) on event doc
# --------------------------------------------------------------------------- #
#
# Closes Phase 13 Codex W2 carry-over: replace the unconditional
# release_event in the /recheck expired-cache branch with a CAS so two
# concurrent retries can't double-mint approvals. The CAS deletes the event
# doc ONLY when the doc's current decision_id matches what the caller saw
# as expired; if a racing retry already evicted+re-recorded, the loser sees
# a different decision_id and refuses to delete.


def test_evict_cached_decision_removes_when_decision_id_matches():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "rollback"})

    assert s.evict_cached_decision("ev-1", "dec-1") is True
    # Event slot is gone — a fresh record_event must succeed.
    assert s.record_event("ev-1", {}) is True


def test_evict_cached_decision_refuses_when_decision_id_differs():
    """CAS fail path: another retry already evicted+re-recorded; the loser
    sees a fresh decision_id and must NOT delete the winner's claim."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-fresh", "ev-1", {"action": "rollback"})

    # Caller observed "dec-stale" as expired but the doc now holds "dec-fresh".
    assert s.evict_cached_decision("ev-1", "dec-stale") is False
    # The fresh claim is preserved — record_event still refuses.
    assert s.record_event("ev-1", {}) is False
    # And find_decision_for_event still returns the fresh decision.
    assert s.find_decision_for_event("ev-1") == {"action": "rollback"}


def test_evict_cached_decision_returns_false_when_event_missing():
    s = InMemoryStateStore()
    assert s.evict_cached_decision("never-claimed", "dec-1") is False


def _build_firestore_mock(snap_exists: bool, snap_data: dict | None):
    """Wire up a Firestore client mock that returns a configured snapshot for
    ``self._events.document(...).get(transaction=...)`` and exposes the
    transaction's delete method for assertion."""
    mock_db = MagicMock()
    mock_doc_ref = MagicMock()
    mock_events_collection = MagicMock()
    mock_events_collection.document.return_value = mock_doc_ref
    mock_decisions_collection = MagicMock()

    def collection_dispatch(name):
        if name == "events":
            return mock_events_collection
        return mock_decisions_collection

    mock_db.collection.side_effect = collection_dispatch

    snap = MagicMock()
    snap.exists = snap_exists
    snap.to_dict.return_value = snap_data
    mock_doc_ref.get.return_value = snap

    mock_transaction = MagicMock()
    mock_db.transaction.return_value = mock_transaction

    return mock_db, mock_doc_ref, mock_transaction


def test_firestore_evict_cached_decision_deletes_on_match():
    """Pin the FirestoreStateStore CAS contract: transactional read of the
    event doc, compare decision_id, delete on match. Uses a mock Firestore
    client so we don't need GCP creds."""
    mock_db, mock_doc_ref, mock_transaction = _build_firestore_mock(
        snap_exists=True, snap_data={"payload": {}, "decision_id": "dec-1"}
    )
    store = FirestoreStateStore(project="p", client=mock_db)

    result = store.evict_cached_decision("ev-1", "dec-1")
    assert result is True
    # Firestore transactional writes go through the transaction, not the
    # doc ref directly — assert delete was issued against the transaction
    # with our doc ref as the target.
    mock_transaction.delete.assert_called_once_with(mock_doc_ref)


def test_firestore_evict_cached_decision_skips_on_mismatch():
    mock_db, mock_doc_ref, mock_transaction = _build_firestore_mock(
        snap_exists=True, snap_data={"payload": {}, "decision_id": "dec-fresh"}
    )
    store = FirestoreStateStore(project="p", client=mock_db)

    result = store.evict_cached_decision("ev-1", "dec-stale")
    assert result is False
    mock_transaction.delete.assert_not_called()


def test_firestore_evict_cached_decision_returns_false_when_doc_missing():
    mock_db, mock_doc_ref, mock_transaction = _build_firestore_mock(
        snap_exists=False, snap_data=None
    )
    store = FirestoreStateStore(project="p", client=mock_db)

    result = store.evict_cached_decision("ev-1", "dec-1")
    assert result is False
    mock_transaction.delete.assert_not_called()
