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
    def find_decision_by_trace_id(
        self, trace_id: str
    ) -> dict[str, Any] | None: ...
    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]: ...


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
        # Phase 19.A.7: every new decision carries a ``created_at`` so
        # ``list_decisions`` has a sortable field on every row. Use a
        # real ``datetime`` here (no ``SERVER_TIMESTAMP`` equivalent for
        # in-memory state). ``setdefault`` lets tests that need a
        # deterministic value pass it in explicitly without being
        # clobbered. Defensive copy so the caller's dict isn't mutated.
        from datetime import datetime, timezone

        record = dict(decision)
        record.setdefault("created_at", datetime.now(timezone.utc))
        self._decisions[decision_id] = record
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

    def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """Linear scan over decisions for the matching ``trace_id``.

        Phase 19.A.6: the ``/trace/{trace_id}`` endpoint enriches the
        reasoning timeline with the persisted decision document so the
        UI can show the final action alongside the events. Linear scan
        is fine for InMemoryStateStore — used only in tests / DRY_RUN —
        where the decision dict is at most a few entries deep.
        """
        for d in self._decisions.values():
            if d.get("trace_id") == trace_id:
                return d
        return None

    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions, newest first.

        Phase 19.A.7: powers the operator-facing ``/decisions``
        listing. Sort key tolerates a missing ``created_at`` via a
        UTC ``datetime.min`` sentinel — a missing field would
        otherwise raise ``TypeError`` on the ``None`` vs ``datetime``
        compare, and a malformed write shouldn't crash the UI.
        """
        from datetime import datetime, timezone

        sentinel = datetime.min.replace(tzinfo=timezone.utc)
        by_time = sorted(
            self._decisions.values(),
            key=lambda d: d.get("created_at") or sentinel,
            reverse=True,
        )
        return by_time[:limit]


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
        # Phase 19.A.7: every new decision carries a ``created_at``
        # field set to ``firestore.SERVER_TIMESTAMP`` so the listing
        # has a server-authoritative sortable column on every row,
        # immune to client clock skew. Defensive copy so the caller's
        # dict isn't mutated. NOTE: ``list_decisions`` does NOT rely
        # on this field for ordering (it sorts client-side on
        # ``snapshot.create_time`` so pre-Phase-19 docs without
        # ``created_at`` still appear) — but the UI surfaces it as
        # the displayed timestamp, so it's worth recording explicitly.
        from google.cloud import firestore

        record = dict(decision)
        record["created_at"] = firestore.SERVER_TIMESTAMP
        self._decisions.document(decision_id).set(record)
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

    def find_decision_by_trace_id(self, trace_id: str) -> dict[str, Any] | None:
        """Index lookup on ``trace_id`` over the decisions collection.

        Phase 19.A.6: the ``/trace/{trace_id}`` endpoint enriches the
        reasoning timeline with the persisted decision so the UI can
        show the final action alongside the events. ``.limit(1)``
        bounds the read; the field is set on every decision since
        19.A.4 (``record_decision`` persists the request's trace_id).

        Returns ``None`` if no decision matches (e.g. /trace was called
        before /recheck finished, or the trace_id was for a /chat call
        that doesn't write a decision document at all).
        """
        snaps = (
            self._decisions.where("trace_id", "==", trace_id).limit(1).stream()
        )
        for s in snaps:
            return s.to_dict()
        return None

    def list_decisions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return up to ``limit`` decisions, newest first.

        Phase 19.A.7 — Codex review IMPORTANT (two distinct invariants):

        1. **Do NOT use server-side ``order_by("created_at")``.**
           Firestore's ``order_by(field)`` EXCLUDES documents where
           the field is missing — it does not sort them last. A
           pre-Phase-19 decision (written before this task added the
           ``created_at`` schema column) would silently disappear
           from the listing. Sorting CLIENT-SIDE on
           ``DocumentSnapshot.create_time`` — which is always present
           and server-managed — gives us a stable union of old and
           new docs without backfilling.

        2. **Do NOT call ``.limit(N)`` on the unordered stream.**
           Firestore's default ordering without ``order_by`` is by
           document ID, so ``.limit(N)`` picks an arbitrary subset
           that may exclude the newest decisions entirely. We have to
           fetch ALL snapshots, sort, then trim.

        Documented assumption: hackathon decision volume is in the
        hundreds, not millions. If this scales past that, swap to a
        server-side ordered query — but that needs a one-time
        backfill of ``created_at`` on every old doc first to
        preserve invariant (1).

        Polish: ``snapshot.create_time`` isn't in ``to_dict()``, but
        pre-Phase-19 docs don't have an explicit ``created_at``
        either. Backfill from ``create_time`` so the UI can show a
        timestamp uniformly across every row.
        """
        snaps = list(self._decisions.stream())
        snaps.sort(
            key=lambda s: s.create_time,
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for s in snaps[:limit]:
            d = s.to_dict() or {}
            d.setdefault("created_at", s.create_time)
            out.append(d)
        return out
