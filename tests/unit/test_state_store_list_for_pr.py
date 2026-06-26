"""Unit tests for ``StateStore.list_decisions_for_pr`` (read_team_log support).

``read_team_log_tool(pr_number=N)`` needs an EXACT per-PR lookup that is
independent of global recency. The naive alternative — ``list_decisions(limit)``
then client-filter on ``pr_number`` — is buggy: ``list_decisions`` trims to
``limit`` BEFORE the caller filters, so a PR whose rows aren't among the latest
``limit`` global decisions returns empty even though its rows exist (Codex
finding #4 on the design). ``list_decisions_for_pr`` filters first, so the per-PR
view is correct regardless of how many newer unrelated decisions exist.

The two invariants these tests pin mirror ``list_decisions``:

1. **Firestore filters server-side with ``where('pr_number','==',n)``** — a single
   equality filter uses the automatic single-field index (no composite index, no
   ``order_by`` which would exclude docs missing the field). It must NOT call
   ``order_by`` or ``.limit(N)`` on the stream (both regress the
   limit-before-sort / order_by-excludes-missing invariants); it sorts
   CLIENT-SIDE on ``snapshot.create_time`` and trims after.

2. **Exactness regardless of global recency** — flood the store with unrelated
   newer decisions; the target PR's rows must still come back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from agent.state_store import FirestoreStateStore, InMemoryStateStore


# --------------------------------------------------------------------------- #
# InMemoryStateStore
# --------------------------------------------------------------------------- #


def _seed(store: InMemoryStateStore, *, decision_id, pr_number, n, created_at):
    store.record_event(f"ev-{decision_id}", {})
    store.record_decision(
        decision_id,
        f"ev-{decision_id}",
        {
            "action": "iac_apply",
            "pr_number": pr_number,
            "n": n,
            "created_at": created_at,
        },
    )


def test_inmemory_returns_only_matching_pr_newest_first():
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    # Two rows for PR 95, two for PR 102, interleaved in time.
    _seed(s, decision_id="d0", pr_number=95, n=0, created_at=base)
    _seed(s, decision_id="d1", pr_number=102, n=1, created_at=base + timedelta(seconds=1))
    _seed(s, decision_id="d2", pr_number=95, n=2, created_at=base + timedelta(seconds=2))
    _seed(s, decision_id="d3", pr_number=102, n=3, created_at=base + timedelta(seconds=3))

    out = s.list_decisions_for_pr(95, limit=10)
    assert [d["n"] for d in out] == [2, 0]  # only PR 95, newest first
    assert all(d["pr_number"] == 95 for d in out)


def test_inmemory_exact_regardless_of_global_recency():
    """The bug ``list_decisions_for_pr`` exists to fix: an old PR's rows must
    surface even when buried under many newer unrelated decisions."""
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    # One old row for PR 95.
    _seed(s, decision_id="old", pr_number=95, n=-1, created_at=base)
    # 50 newer decisions for OTHER PRs.
    for i in range(50):
        _seed(
            s,
            decision_id=f"new-{i}",
            pr_number=200 + i,
            n=i,
            created_at=base + timedelta(seconds=10 + i),
        )

    out = s.list_decisions_for_pr(95, limit=20)
    assert len(out) == 1
    assert out[0]["n"] == -1


def test_inmemory_respects_limit():
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed(s, decision_id=f"d{i}", pr_number=95, n=i, created_at=base + timedelta(seconds=i))

    out = s.list_decisions_for_pr(95, limit=2)
    assert [d["n"] for d in out] == [4, 3]  # 2 newest matching


def test_inmemory_no_match_returns_empty():
    s = InMemoryStateStore()
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    _seed(s, decision_id="d0", pr_number=95, n=0, created_at=base)
    assert s.list_decisions_for_pr(999, limit=10) == []


def test_inmemory_tolerates_missing_created_at():
    """A row missing ``created_at`` must not raise on the sort (sentinel),
    mirroring ``list_decisions``."""
    s = InMemoryStateStore()
    s._decisions["a"] = {"action": "iac_apply", "pr_number": 95, "tag": "old"}
    s._decisions["b"] = {
        "action": "iac_apply",
        "pr_number": 95,
        "tag": "new",
        "created_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
    }
    out = s.list_decisions_for_pr(95, limit=10)
    assert [d["tag"] for d in out] == ["new", "old"]


# --------------------------------------------------------------------------- #
# FirestoreStateStore
# --------------------------------------------------------------------------- #


def _make_snap(data: dict, create_time: datetime):
    snap = MagicMock()
    snap.to_dict.return_value = data
    snap.create_time = create_time
    return snap


def _build_firestore_mock_for_where(snaps: list[MagicMock]):
    """Wire a Firestore client mock where
    ``decisions.where('pr_number','==',n).stream()`` yields ``snaps``.

    The ``where`` MagicMock is captured so a test can assert the filter
    arguments and that ``order_by`` / ``.limit`` were never called.
    """
    mock_db = MagicMock()
    mock_decisions = MagicMock()
    mock_events = MagicMock()

    def collection_dispatch(name):
        if name == "decisions":
            return mock_decisions
        return mock_events

    mock_db.collection.side_effect = collection_dispatch

    mock_query = MagicMock()
    mock_query.stream.return_value = iter(snaps)
    mock_decisions.where.return_value = mock_query
    return mock_db, mock_decisions, mock_query


def test_firestore_filters_with_where_and_sorts_desc():
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    snaps = [
        _make_snap({"action": "iac_apply", "pr_number": 95, "n": 0}, base),
        _make_snap({"action": "iac_apply", "pr_number": 95, "n": 2}, base + timedelta(seconds=2)),
        _make_snap({"action": "iac_apply", "pr_number": 95, "n": 1}, base + timedelta(seconds=1)),
    ]
    mock_db, mock_decisions, mock_query = _build_firestore_mock_for_where(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions_for_pr(95, limit=10)

    # Server-side equality filter on pr_number.
    mock_decisions.where.assert_called_once_with("pr_number", "==", 95)
    # Newest first (client-side create_time sort).
    assert [d["n"] for d in out] == [2, 1, 0]
    # MUST NOT order_by (excludes missing-field docs) or limit-before-sort.
    mock_query.order_by.assert_not_called()
    mock_query.limit.assert_not_called()


def test_firestore_backfills_created_at_and_respects_limit():
    base = datetime(2026, 5, 21, 0, 0, 0, tzinfo=timezone.utc)
    snaps = [
        _make_snap({"action": "iac_apply", "pr_number": 95, "tag": f"d{i}"}, base + timedelta(seconds=i))
        for i in range(5)
    ]
    mock_db, _, _ = _build_firestore_mock_for_where(snaps)
    store = FirestoreStateStore(project="p", client=mock_db)

    out = store.list_decisions_for_pr(95, limit=2)
    assert [d["tag"] for d in out] == ["d4", "d3"]
    # created_at backfilled from snapshot.create_time on every returned row.
    assert out[0]["created_at"] == base + timedelta(seconds=4)


def test_firestore_no_match_returns_empty():
    mock_db, _, _ = _build_firestore_mock_for_where([])
    store = FirestoreStateStore(project="p", client=mock_db)
    assert store.list_decisions_for_pr(404, limit=10) == []
