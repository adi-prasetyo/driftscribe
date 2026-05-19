"""Persistent state for DriftScribe.

Two implementations:
- InMemoryStateStore: tests + DRY_RUN mode. Resets per-process.
- FirestoreStateStore: production. Uses Cloud Firestore native mode.

Both implement the StateStore Protocol so callers can substitute freely.

Idempotency model:
- ``record_event`` claims an event key. If the key already exists, returns
  False (claim refused). Used to gate side-effect work.
- ``record_decision`` writes the decision JSON keyed by decision_id, and
  cross-references it back to event_key so ``find_decision_for_event`` can
  return it on subsequent identical recheck calls.
- ``get_decision(decision_id)`` returns the recorded response or None.
"""

from typing import Any, Protocol


class StateStore(Protocol):
    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool: ...
    def release_event(self, event_key: str) -> None: ...
    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None: ...
    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None: ...
    def get_decision(self, decision_id: str) -> dict[str, Any] | None: ...
    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool: ...


class InMemoryStateStore:
    """Process-local state. Used in tests and DRY_RUN mode."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}  # event_key -> {payload, decision_id}
        self._decisions: dict[str, dict[str, Any]] = {}  # decision_id -> full decision

    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool:
        if event_key in self._events:
            return False
        self._events[event_key] = {"payload": payload, "decision_id": None}
        return True

    def release_event(self, event_key: str) -> None:
        """Drop a claim. Used by ``_do_recheck`` when side effects fail so
        retries can proceed. No-op if the event isn't claimed."""
        self._events.pop(event_key, None)

    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None:
        record = self._events.get(event_key)
        if not record or not record["decision_id"]:
            return None
        return self._decisions.get(record["decision_id"])

    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None:
        self._decisions[decision_id] = decision
        if event_key in self._events:
            self._events[event_key]["decision_id"] = decision_id

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        return self._decisions.get(decision_id)

    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool:
        """Compare-and-delete the event doc; True iff decision_id matched."""
        record = self._events.get(event_key)
        if not record or record.get("decision_id") != decision_id:
            return False
        self._events.pop(event_key, None)
        return True


class FirestoreStateStore:
    """Cloud Firestore-backed state. Collections: ``events``, ``decisions``."""

    def __init__(self, project: str, client: Any = None) -> None:
        # Lazy import so tests that don't use this don't need GCP creds installed
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project)
        self._db = client
        self._events = client.collection("events")
        self._decisions = client.collection("decisions")

    def record_event(self, event_key: str, payload: dict[str, Any]) -> bool:
        # Create-if-absent: succeed only when the doc didn't already exist.
        # We narrow to AlreadyExists so genuine infra failures (permissions,
        # network) propagate as exceptions rather than being misread as "claim
        # refused" — Codex review #4 of Phase 4.
        from google.api_core.exceptions import AlreadyExists

        doc = self._events.document(event_key)
        try:
            doc.create({"payload": payload, "decision_id": None})
            return True
        except AlreadyExists:
            return False

    def release_event(self, event_key: str) -> None:
        """Drop a claim so retries can proceed after a side-effect failure.
        No-op if the document doesn't exist."""
        from google.api_core.exceptions import NotFound

        try:
            self._events.document(event_key).delete()
        except NotFound:
            pass

    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None:
        snap = self._events.document(event_key).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        decision_id = data.get("decision_id")
        if not decision_id:
            return None
        return self.get_decision(decision_id)

    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None:
        self._decisions.document(decision_id).set(decision)
        self._events.document(event_key).update({"decision_id": decision_id})

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        snap = self._decisions.document(decision_id).get()
        return snap.to_dict() if snap.exists else None

    def evict_cached_decision(self, event_key: str, decision_id: str) -> bool:
        """Compare-and-delete the event doc transactionally; True iff
        decision_id matched. Closes Phase 13 Codex W2 carry-over — two
        concurrent /recheck retries observing the same expired cached
        rollback would both call release_event under the prior code,
        letting one re-claim and the other delete that fresh claim. The
        CAS keeps the loser from clobbering the winner."""
        from google.cloud import firestore

        doc_ref = self._events.document(event_key)

        @firestore.transactional
        def _txn(transaction, expected_decision_id):
            snap = doc_ref.get(transaction=transaction)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("decision_id") != expected_decision_id:
                return False
            transaction.delete(doc_ref)
            return True

        transaction = self._db.transaction()
        return _txn(transaction, decision_id)
