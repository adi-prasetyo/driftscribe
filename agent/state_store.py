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
    def get_pause(self) -> dict[str, Any] | None: ...
    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]: ...
    def get_autonomy(self) -> dict[str, Any] | None: ...
    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]: ...


class InMemoryStateStore:
    """Process-local state. Used in tests and DRY_RUN mode."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}  # event_key -> {payload, decision_id}
        self._decisions: dict[str, dict[str, Any]] = {}  # decision_id -> full decision
        # Pause flag singleton. None = never written (system is running by default —
        # the pause doc not existing means the operator has never toggled it).
        self._pause: dict[str, Any] | None = None
        # Autonomy dial singleton. None = never written; agent.autonomy maps
        # absent → the permissive DEFAULT_MODE (system's pre-dial behavior).
        self._autonomy: dict[str, Any] | None = None

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
        # Phase C5e: store ``event_key`` on the decision itself so the Firestore
        # store's query-fallback recovery (find_decision_for_event) has a field to
        # query, and so callers / tests see the same shape across both stores.
        record["event_key"] = event_key
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

    def get_pause(self) -> dict[str, Any] | None:
        """Return a defensive copy of the pause document, or None if never set.

        Returns a copy so callers cannot alias or mutate the stored state.
        Absent doc = not paused: the system predates this feature; the default
        is always-running.
        """
        if self._pause is None:
            return None
        return dict(self._pause)

    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Overwrite the pause document and return a defensive copy.

        Stores a fresh ``updated_at`` timestamp (UTC ``datetime`` — the
        in-memory equivalent of Firestore's SERVER_TIMESTAMP). Defensive copy
        on both the stored dict and the returned dict so neither the caller
        nor a subsequent get_pause caller can alias internal state.
        """
        from datetime import datetime, timezone

        self._pause = {
            "paused": paused,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc),
        }
        return dict(self._pause)

    def get_autonomy(self) -> dict[str, Any] | None:
        """Return a defensive copy of the autonomy document, or None if never set.

        Mirrors get_pause: absent doc = dial never touched; the caller
        (agent.autonomy.read_autonomy_state) maps None to the default mode.
        """
        if self._autonomy is None:
            return None
        return dict(self._autonomy)

    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Overwrite the autonomy document and return a defensive copy."""
        from datetime import datetime, timezone

        self._autonomy = {
            "mode": mode,
            "reason": reason,
            "actor": actor,
            "updated_at": datetime.now(timezone.utc),
        }
        return dict(self._autonomy)


class FirestoreStateStore:
    """Cloud Firestore-backed state. Collections: ``events``, ``decisions``, ``config``."""

    def __init__(self, project: str, client: Any = None) -> None:
        # Lazy import so tests that don't use this don't need GCP creds installed
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project)
        self._db = client
        self._events = client.collection("events")
        self._decisions = client.collection("decisions")
        # ``config`` collection for singleton operator-configuration documents.
        # Currently only the ``pause`` document (id="pause") lives here.
        # Separate from ``events``/``decisions`` so IAM and query scopes stay clean.
        self._config = client.collection("config")

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
        decision_id = None
        if snap.exists:
            data = snap.to_dict() or {}
            decision_id = data.get("decision_id")
        if decision_id:
            return self.get_decision(decision_id)
        # Phase C5e recovery fallback (belt-and-suspenders): if the event doc is
        # missing or carries no decision_id — e.g. the pointer write was lost — fall
        # back to a query on the ``event_key`` field that ``record_decision`` now
        # stores INSIDE the decision doc. C5e uses the decision doc as the
        # apply-then-merge reconcile pointer, so a lost pointer must still be
        # recoverable rather than silently re-minting + re-applying.
        snaps = self._decisions.where("event_key", "==", event_key).limit(1).stream()
        for s in snaps:
            return s.to_dict()
        return None

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
        #
        # Phase C5e: the decision doc is the apply-then-merge reconcile pointer, so
        # the decision write + the event→decision pointer write MUST commit together
        # — previously two separate writes, where a crash between them orphaned the
        # pointer (a later /apply could then re-mint + re-apply over a possibly-
        # changed world). A WriteBatch makes both atomic. We ALSO store ``event_key``
        # inside the decision doc so ``find_decision_for_event`` can recover via a
        # query if the pointer write is ever lost. The event pointer uses
        # ``set(..., merge=True)`` rather than ``update`` so a (corner-case) missing
        # event doc upserts instead of raising NotFound — without clobbering the
        # existing claim payload.
        from google.cloud import firestore

        record = dict(decision)
        record["created_at"] = firestore.SERVER_TIMESTAMP
        record["event_key"] = event_key
        batch = self._db.batch()
        batch.set(self._decisions.document(decision_id), record)
        batch.set(
            self._events.document(event_key),
            {"decision_id": decision_id},
            merge=True,
        )
        batch.commit()

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

    def get_pause(self) -> dict[str, Any] | None:
        """Point-read the ``config/pause`` document; returns ``to_dict()`` or None.

        Returns None when the document has never been written (the feature was
        added after the system was deployed; absent = not paused by design).
        ``to_dict()`` already returns a plain dict copy so no extra defensive copy
        is needed here — Firestore's client always constructs a fresh object.
        """
        snap = self._config.document("pause").get()
        return snap.to_dict() if snap.exists else None

    def set_pause(
        self, *, paused: bool, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Full-overwrite the ``config/pause`` document and return the as-written dict.

        Uses ``firestore.SERVER_TIMESTAMP`` for ``updated_at`` so the caller
        receives the real server-authoritative time (not client-clock time that
        drifts across Cloud Run instances). One extra point-read after the write
        is intentional: toggles are rare operator actions, and returning a
        client-side guess at the server timestamp would silently lie about what
        Firestore actually stored. The read-after-write is the cheapest way to
        give the caller — and the audit log — the truthful value.
        """
        from google.cloud import firestore

        doc_ref = self._config.document("pause")
        doc_ref.set(
            {
                "paused": paused,
                "reason": reason,
                "actor": actor,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        # Read back the written document so the returned dict carries the real
        # server timestamp rather than the sentinel value.
        snap = doc_ref.get()
        return snap.to_dict()

    def get_autonomy(self) -> dict[str, Any] | None:
        """Point-read the ``config/autonomy`` document; ``to_dict()`` or None.

        Mirrors get_pause: None when the document has never been written
        (the dial was never touched; agent.autonomy maps that to the
        permissive default mode). ``to_dict()`` already returns a fresh
        plain dict so no extra defensive copy is needed.
        """
        snap = self._config.document("autonomy").get()
        return snap.to_dict() if snap.exists else None

    def set_autonomy(
        self, *, mode: str, reason: str | None, actor: str
    ) -> dict[str, Any]:
        """Full-overwrite the ``config/autonomy`` document; return as-written.

        Mirrors set_pause: ``firestore.SERVER_TIMESTAMP`` for ``updated_at``
        plus a read-after-write so the caller and audit log see the real
        server-authoritative time rather than the sentinel.
        """
        from google.cloud import firestore

        doc_ref = self._config.document("autonomy")
        doc_ref.set(
            {
                "mode": mode,
                "reason": reason,
                "actor": actor,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        snap = doc_ref.get()
        return snap.to_dict()
