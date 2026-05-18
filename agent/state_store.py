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
    def find_decision_for_event(self, event_key: str) -> dict[str, Any] | None: ...
    def record_decision(
        self, decision_id: str, event_key: str, decision: dict[str, Any]
    ) -> None: ...
    def get_decision(self, decision_id: str) -> dict[str, Any] | None: ...


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
        doc = self._events.document(event_key)
        try:
            doc.create({"payload": payload, "decision_id": None})
            return True
        except Exception:
            # google.api_core.exceptions.AlreadyExists — treat as claim refused.
            # Broad catch is intentional: any failure to claim must NOT proceed
            # to side effects.
            return False

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
