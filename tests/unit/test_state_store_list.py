"""Unit tests for ``StateStore.list_decisions`` (Phase 19.A.7).

The /decisions endpoint enumerates past decisions newest-first for the
operator transparency UI. Both StateStore implementations need to
support a bounded listing — InMemory sorts on the explicit
``created_at`` payload field; Firestore sorts CLIENT-SIDE on
``DocumentSnapshot.create_time``.

These tests pin two Codex-flagged IMPORTANT invariants:

1. **Server-side ``order_by(field)`` is wrong** — Firestore's
   ``order_by(field)`` EXCLUDES documents where the field is missing
   (not "sorts them last"). A pre-Phase-19 decision that doesn't carry
   ``created_at`` would silently disappear from the UI. The
   implementation MUST sort client-side on ``snapshot.create_time``
   so old + new decisions both appear.

2. **``.limit(N)`` before sort is wrong** — Firestore's default order
   without ``order_by`` is BY DOCUMENT ID, so ``.limit(N)`` picks an
   arbitrary subset that may exclude the newest decisions. The
   implementation MUST fetch all, then sort, then trim.

Plus the ``record_decision`` schema-change pin: every new decision
record gets a ``created_at`` field on write (``SERVER_TIMESTAMP`` for
Firestore, ``datetime.now(UTC)`` for InMemory).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from agent.state_store import FirestoreStateStore, InMemoryStateStore


# --------------------------------------------------------------------------- #
# InMemoryStateStore — record_decision sets created_at; list returns desc
# --------------------------------------------------------------------------- #


def test_inmemory_record_decision_sets_created_at_on_write():
    """A decision dict that doesn't carry ``created_at`` must get one
    backfilled by ``record_decision`` (mirrors the Firestore behavior
    of setting ``SERVER_TIMESTAMP`` so the listing has a sortable
    field on every record)."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    s.record_decision("dec-1", "ev-1", {"action": "no_op"})

    rec = s.get_decision("dec-1")
    assert rec is not None
    assert "created_at" in rec
    assert isinstance(rec["created_at"], datetime)
    # Timezone-aware; UTC.
    assert rec["created_at"].tzinfo is not None


def test_inmemory_record_decision_preserves_explicit_created_at():
    """If the caller already supplied ``created_at`` (e.g. tests that
    need a deterministic value), ``record_decision`` must NOT clobber
    it — ``setdefault`` semantics."""
    s = InMemoryStateStore()
    s.record_event("ev-1", {})
    fixed = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    s.record_decision(
        "dec-1", "ev-1", {"action": "no_op", "created_at": fixed}
    )

    rec = s.get_decision("dec-1")
    assert rec is not None
    assert rec["created_at"] == fixed


def test_inmemory_list_decisions_returns_newest_first():
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        s.record_event(f"ev-{i}", {})
        s.record_decision(
            f"dec-{i}",
            f"ev-{i}",
            {"action": "no_op", "n": i, "created_at": base + timedelta(seconds=i)},
        )

    out = s.list_decisions(limit=10)
    assert [d["n"] for d in out] == [2, 1, 0]


def test_inmemory_list_decisions_respects_limit():
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        s.record_event(f"ev-{i}", {})
        s.record_decision(
            f"dec-{i}",
            f"ev-{i}",
            {"action": "no_op", "n": i, "created_at": base + timedelta(seconds=i)},
        )

    out = s.list_decisions(limit=2)
    assert len(out) == 2
    # The 2 newest.
    assert [d["n"] for d in out] == [4, 3]


def test_inmemory_list_decisions_empty_store_returns_empty_list():
    s = InMemoryStateStore()
    assert s.list_decisions(limit=10) == []


def test_inmemory_list_decisions_missing_created_at_sorts_last():
    """A decision dict missing ``created_at`` (shouldn't normally
    happen after this task lands — ``record_decision`` backfills —
    but the sort key MUST tolerate it via a sentinel rather than
    raising ``TypeError`` on the ``None`` vs ``datetime`` compare)."""
    s = InMemoryStateStore()
    # Bypass record_decision so we can plant a missing-field doc
    # directly — this simulates a malformed write or a future migration.
    s._decisions["dec-old"] = {"action": "no_op", "tag": "old"}
    s._decisions["dec-new"] = {
        "action": "no_op",
        "tag": "new",
        "created_at": datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc),
    }

    out = s.list_decisions(limit=10)
    assert [d["tag"] for d in out] == ["new", "old"]


# --------------------------------------------------------------------------- #
# FirestoreStateStore — record_decision sets SERVER_TIMESTAMP
# --------------------------------------------------------------------------- #


def _build_firestore_mock_for_record_decision():
    """Wire up a Firestore client mock that captures ``.set()`` payloads
    on the decisions collection and ``.update()`` on the events one."""
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch

    mock_decision_doc = MagicMock()
    mock_decisions.document.return_value = mock_decision_doc
    mock_event_doc = MagicMock()
    mock_events.document.return_value = mock_event_doc

    return mock_db, mock_decision_doc, mock_event_doc


def test_firestore_record_decision_sets_created_at_server_timestamp():
    """Codex IMPORTANT: every new decision record must carry an
    explicit ``created_at = firestore.SERVER_TIMESTAMP`` field on
    write. The server-side authoritative time is immune to client
    clock skew, and (combined with client-side sort on
    ``snapshot.create_time`` in ``list_decisions``) gives the UI a
    sortable timestamp on every row.

    Capture the ``.set()`` payload and assert ``SERVER_TIMESTAMP`` is
    present. We can't assert the exact sentinel value cross-platform
    (it's an opaque object), but we CAN check the recorded payload
    has the key and that it isn't the bare dict the caller passed."""
    from google.cloud import firestore

    mock_db, mock_decision_doc, mock_event_doc = (
        _build_firestore_mock_for_record_decision()
    )
    store = FirestoreStateStore(project="p", client=mock_db)

    caller_payload = {"action": "drift_issue", "trace_id": "a" * 32}
    store.record_decision("dec-1", "ev-1", caller_payload)

    # The ``.set()`` payload includes the caller fields plus created_at.
    mock_decision_doc.set.assert_called_once()
    written = mock_decision_doc.set.call_args[0][0]
    assert written["action"] == "drift_issue"
    assert written["trace_id"] == "a" * 32
    assert "created_at" in written
    assert written["created_at"] is firestore.SERVER_TIMESTAMP

    # Caller's dict must NOT be mutated (defensive copy).
    assert "created_at" not in caller_payload

    # Event doc cross-reference still happens.
    mock_event_doc.update.assert_called_once_with({"decision_id": "dec-1"})


# --------------------------------------------------------------------------- #
# FirestoreStateStore.list_decisions — client-side sort on create_time
# --------------------------------------------------------------------------- #


def _build_firestore_mock_for_list_decisions(snaps: list[MagicMock]):
    """Wire up a Firestore client mock where ``decisions.stream()``
    yields the provided snapshot mocks."""
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch
    mock_decisions.stream.return_value = iter(snaps)
    return mock_db, mock_decisions


def _make_snap(data: dict, create_time: datetime):
    snap = MagicMock()
    snap.to_dict.return_value = data
    snap.create_time = create_time
    return snap


def test_firestore_list_decisions_orders_by_create_time_desc():
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    snaps = [
        # Insertion order is OLDEST first — listing must flip to NEWEST first.
        _make_snap({"action": "no_op", "n": 0}, base),
        _make_snap({"action": "no_op", "n": 1}, base + timedelta(seconds=1)),
        _make_snap({"action": "no_op", "n": 2}, base + timedelta(seconds=2)),
    ]
    mock_db, _ = _build_firestore_mock_for_list_decisions(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions(limit=10)
    assert [d["n"] for d in out] == [2, 1, 0]


def test_firestore_list_decisions_includes_pre_phase_19_decisions():
    """Codex IMPORTANT: server-side ``order_by("created_at")`` would
    EXCLUDE the pre-Phase-19 doc (Firestore filters out docs where
    the field is missing). Client-side sort on ``snapshot.create_time``
    keeps it visible.

    Pin: write one doc WITHOUT an explicit ``created_at`` payload
    field (mimicking a pre-19.A.7 doc); assert it appears in the
    listing at the position implied by its ``snapshot.create_time``."""
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    # The "old" decision has no created_at in its dict, but has a real
    # snapshot.create_time (Firestore always tracks this).
    snaps = [
        _make_snap(
            {"action": "no_op", "tag": "new", "created_at": base + timedelta(seconds=2)},
            base + timedelta(seconds=2),
        ),
        _make_snap(
            {"action": "no_op", "tag": "old"},  # no created_at field!
            base + timedelta(seconds=1),
        ),
        _make_snap(
            {"action": "no_op", "tag": "newest", "created_at": base + timedelta(seconds=3)},
            base + timedelta(seconds=3),
        ),
    ]
    mock_db, _ = _build_firestore_mock_for_list_decisions(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions(limit=10)
    tags = [d["tag"] for d in out]
    # The pre-Phase-19 doc is included AT THE POSITION implied by its
    # create_time (between "new" and the implied first).
    assert tags == ["newest", "new", "old"]
    # And the returned dict has ``created_at`` backfilled from
    # snapshot.create_time so the UI can show a timestamp uniformly.
    old_row = [d for d in out if d["tag"] == "old"][0]
    assert old_row["created_at"] == base + timedelta(seconds=1)


def test_firestore_list_decisions_doesnt_truncate_before_sort():
    """Codex IMPORTANT (v3.1): ``.limit(N)`` on a Firestore stream
    WITHOUT ``order_by`` uses the default doc-ID ordering, which
    picks an ARBITRARY subset that may exclude the newest. The
    implementation MUST fetch all, then sort, then trim.

    Pin: write ``limit * 4`` decisions where doc-id alphabetic order
    is INVERSE of create_time order. ``list_decisions(limit=10)``
    must return the 10 NEWEST by create_time, not the alphabetic
    head. If the impl ever regresses to ``stream(limit=N)``-then-sort,
    this test fails because it'd pick the alphabetic head, miss the
    newest ones, and the assertion on ``newest in returned`` breaks."""
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    # 40 decisions with non-monotonic doc ids. Use a setup where the
    # NEWEST decisions have the LARGEST integer suffix in their tag,
    # and the alphabetic order of the to_dict tags is INVERSE of
    # create_time — i.e. the alphabetically first doc is the OLDEST.
    snaps = []
    for i in range(40):
        # tag: zero-pad so alphabetic == numeric for the tag
        snaps.append(
            _make_snap(
                {"action": "no_op", "tag": f"d{i:03d}"},
                base + timedelta(seconds=i),
            )
        )
    # Shuffle insertion order so the stream isn't already sorted.
    import random

    random.Random(42).shuffle(snaps)

    mock_db, mock_decisions = _build_firestore_mock_for_list_decisions(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions(limit=10)
    assert len(out) == 10
    tags = [d["tag"] for d in out]
    # The 10 newest by create_time are d039 .. d030.
    assert tags == [f"d{i:03d}" for i in range(39, 29, -1)]
    # Defense-in-depth: pin the interaction shape too. A future
    # refactor that silently regressed to ``.limit(N).stream()`` or
    # added a server-side ``.order_by(...)`` would slip past the
    # behavior assertion above if the mock stream happened to be in
    # the right order — but it must NOT call those methods at all
    # because both regress the IMPORTANT invariants (limit-before-sort,
    # order_by-excludes-missing-field).
    mock_decisions.limit.assert_not_called()
    mock_decisions.order_by.assert_not_called()


def test_firestore_list_decisions_empty_collection_returns_empty():
    mock_db, _ = _build_firestore_mock_for_list_decisions([])
    store = FirestoreStateStore(project="p", client=mock_db)
    assert store.list_decisions(limit=10) == []


def test_firestore_list_decisions_backfills_created_at_from_create_time():
    """The backfill is what makes the UI uniform: every row in the
    response has a ``created_at`` field, regardless of whether it
    was written pre- or post-19.A.7. Even a doc that already has
    ``created_at`` keeps its explicit value (setdefault semantics)."""
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    explicit = base + timedelta(seconds=100)  # very different from create_time
    snaps = [
        _make_snap(
            {"action": "no_op", "tag": "with_explicit", "created_at": explicit},
            base + timedelta(seconds=1),
        ),
        _make_snap(
            {"action": "no_op", "tag": "without"},
            base + timedelta(seconds=2),
        ),
    ]
    mock_db, _ = _build_firestore_mock_for_list_decisions(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions(limit=10)
    by_tag = {d["tag"]: d for d in out}
    # The explicit value is preserved.
    assert by_tag["with_explicit"]["created_at"] == explicit
    # The missing value is backfilled from snapshot.create_time.
    assert by_tag["without"]["created_at"] == base + timedelta(seconds=2)
