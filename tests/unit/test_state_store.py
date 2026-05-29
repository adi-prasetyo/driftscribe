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
    # Phase 19.A.7: record_decision now backfills ``created_at`` for
    # the /decisions listing — assert the caller-supplied fields and
    # the presence of the timestamp without pinning its exact value.
    out = s.find_decision_for_event("ev-1")
    assert out is not None
    assert out["action"] == "drift_issue"
    assert "created_at" in out


def test_get_decision_returns_recorded_decision():
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "drift_issue"})
    out = s.get_decision("dec-1")
    assert out is not None
    assert out["action"] == "drift_issue"
    assert "created_at" in out


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
    # Phase 19.A.7: record_decision backfills ``created_at`` for the
    # /decisions listing — assert the action without pinning the
    # auto-generated timestamp.
    fresh = s.find_decision_for_event("ev-1")
    assert fresh is not None
    assert fresh["action"] == "rollback"


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


# --------------------------------------------------------------------------- #
# Phase C5e: record_decision stores event_key + is atomic; find_decision_for_event
# recovers via a query when the pointer is lost.
# --------------------------------------------------------------------------- #


def test_inmemory_record_decision_stores_event_key():
    """The decision doc carries its ``event_key`` so both stores expose the same
    shape (and the Firestore query-fallback has a field to query)."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "rollback"})
    out = s.get_decision("dec-1")
    assert out is not None
    assert out["event_key"] == "ev-1"


def test_inmemory_find_decision_for_event_returns_by_pointer():
    """Normal path: the event pointer resolves the decision (event_key field is
    present but the pointer is what's used)."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "rollback"})
    out = s.find_decision_for_event("ev-1")
    assert out is not None
    assert out["action"] == "rollback"
    assert out["event_key"] == "ev-1"


class _FakeDocRef:
    """Records ``set`` / ``update`` calls so the test can assert what a batch wrote."""

    def __init__(self, name: str, recorder: list):
        self._name = name
        self._recorder = recorder

    def set(self, data, merge=False):
        self._recorder.append(("set", self._name, data, merge))

    def update(self, data):
        self._recorder.append(("update", self._name, data))

    def get(self, transaction=None):  # unused here
        raise NotImplementedError


class _FakeBatch:
    def __init__(self, recorder: list):
        self._recorder = recorder
        self.committed = False

    def set(self, doc_ref, data, merge=False):
        doc_ref.set(data, merge=merge)

    def update(self, doc_ref, data):
        doc_ref.update(data)

    def commit(self):
        self.committed = True
        self._recorder.append(("commit",))


def _build_record_decision_mock():
    """Firestore client mock for ``record_decision``: a ``.batch()`` whose writes
    land in a shared recorder; ``.collection("decisions"|"events").document(id)``
    returns recording doc refs."""
    recorder: list = []
    decision_refs: dict[str, _FakeDocRef] = {}
    event_refs: dict[str, _FakeDocRef] = {}

    mock_db = MagicMock()

    def collection_dispatch(name):
        col = MagicMock()
        registry = decision_refs if name == "decisions" else event_refs

        def document(doc_id):
            if doc_id not in registry:
                registry[doc_id] = _FakeDocRef(f"{name}/{doc_id}", recorder)
            return registry[doc_id]

        col.document.side_effect = document
        return col

    mock_db.collection.side_effect = collection_dispatch
    batch = _FakeBatch(recorder)
    mock_db.batch.return_value = batch
    return mock_db, recorder, batch


def test_firestore_record_decision_writes_both_atomically():
    """``record_decision`` commits BOTH the decision doc and the event→decision
    pointer through a single batch — guarding against the orphaned-pointer crash
    window the C5e reconcile contract depends on."""
    mock_db, recorder, batch = _build_record_decision_mock()
    store = FirestoreStateStore(project="p", client=mock_db)

    store.record_decision("dec-1", "ev-1", {"action": "applied"})

    # Exactly one batch.commit().
    assert batch.committed is True
    assert ("commit",) in recorder

    # The decision doc was set with the action + event_key + created_at.
    decision_sets = [r for r in recorder if r[0] == "set" and r[1] == "decisions/dec-1"]
    assert len(decision_sets) == 1
    _, _, data, merge = decision_sets[0]
    assert data["action"] == "applied"
    assert data["event_key"] == "ev-1"
    assert "created_at" in data
    assert merge is False

    # The event pointer was set with merge=True (upsert; no NotFound on a missing
    # event doc, no clobber of an existing claim payload).
    event_sets = [r for r in recorder if r[0] == "set" and r[1] == "events/ev-1"]
    assert len(event_sets) == 1
    _, _, pdata, pmerge = event_sets[0]
    assert pdata == {"decision_id": "dec-1"}
    assert pmerge is True


def test_firestore_find_decision_for_event_query_fallback():
    """If the event pointer is missing (lost write), find_decision_for_event falls
    back to a query on the decision's ``event_key`` field."""
    mock_db = MagicMock()

    # events.document("ev-1").get() → snapshot that does not exist.
    mock_events = MagicMock()
    missing_snap = MagicMock()
    missing_snap.exists = False
    mock_events.document.return_value.get.return_value = missing_snap

    # decisions.where("event_key","==","ev-1").limit(1).stream() → one match.
    mock_decisions = MagicMock()
    recovered = MagicMock()
    recovered.to_dict.return_value = {"action": "applied", "event_key": "ev-1"}
    where_q = MagicMock()
    where_q.limit.return_value.stream.return_value = iter([recovered])
    mock_decisions.where.return_value = where_q

    def collection_dispatch(name):
        return mock_events if name == "events" else mock_decisions

    mock_db.collection.side_effect = collection_dispatch
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.find_decision_for_event("ev-1")
    assert out == {"action": "applied", "event_key": "ev-1"}
    mock_decisions.where.assert_called_once_with("event_key", "==", "ev-1")


def test_firestore_find_decision_for_event_query_fallback_no_match_returns_none():
    """Event pointer missing AND no decision carries the event_key → None."""
    mock_db = MagicMock()

    mock_events = MagicMock()
    missing_snap = MagicMock()
    missing_snap.exists = False
    mock_events.document.return_value.get.return_value = missing_snap

    mock_decisions = MagicMock()
    where_q = MagicMock()
    where_q.limit.return_value.stream.return_value = iter([])
    mock_decisions.where.return_value = where_q

    def collection_dispatch(name):
        return mock_events if name == "events" else mock_decisions

    mock_db.collection.side_effect = collection_dispatch
    store = FirestoreStateStore(project="p", client=mock_db)

    assert store.find_decision_for_event("ev-1") is None


def test_firestore_find_decision_for_event_pointer_present_skips_fallback():
    """When the event pointer IS present, the normal get_decision path is used and
    the query fallback is NOT consulted."""
    mock_db = MagicMock()

    mock_events = MagicMock()
    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {"payload": {}, "decision_id": "dec-1"}
    mock_events.document.return_value.get.return_value = snap

    mock_decisions = MagicMock()
    dec_snap = MagicMock()
    dec_snap.exists = True
    dec_snap.to_dict.return_value = {"action": "applied", "event_key": "ev-1"}
    mock_decisions.document.return_value.get.return_value = dec_snap

    def collection_dispatch(name):
        return mock_events if name == "events" else mock_decisions

    mock_db.collection.side_effect = collection_dispatch
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.find_decision_for_event("ev-1")
    assert out == {"action": "applied", "event_key": "ev-1"}
    mock_decisions.where.assert_not_called()
