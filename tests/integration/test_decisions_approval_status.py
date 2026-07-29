"""Integration tests for the serve-time approval-status enrichment on
``GET /decisions`` (Task 3.0b, 2026-07-28).

A rollback decision row carries an ``approval`` sub-object
(``approval_id``/``approval_url``/``expires_at``) minted at propose time. At
serve time the route now joins the LIVE approval doc's ``status`` and its
resolution timestamp (``resolved_at``, Part A of this task) into that
sub-object — compute-only, never persisted back onto the decision.

Mocking strategy mirrors ``tests/integration/test_approvals.py``:
``agent.approvals.get_approval_store`` (imported into ``agent.main`` as
``approval_helpers``) is monkeypatched to an in-memory fake so no real
Firestore client is ever constructed.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import approvals as approval_helpers
from agent.main import app, get_state
from driftscribe_lib.approvals import Approval

_APPROVAL_ID = "4f1c2d3e-aaaa-bbbb-cccc-1234567890ab"


class _FakeApprovalStore:
    """In-memory ApprovalStore — only ``get`` is needed by this route."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.get_calls = 0

    def put(self, approval_id: str, **fields: Any) -> None:
        self.docs[approval_id] = fields

    def get(self, approval_id: str) -> Approval | None:
        self.get_calls += 1
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeApprovalStore:
    s = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: s)
    return s


def _base_fields(**over: Any) -> dict[str, Any]:
    base = dict(
        target_revision="payment-demo-00003-xyz",
        reason="hard contract violation",
        token_hmac="deadbeef",
        expires_at=dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.timezone.utc),
        created_at=dt.datetime(2026, 7, 28, 11, 45, tzinfo=dt.timezone.utc),
        created_by="coordinator@x",
        status="pending",
        resolved_at=None,
    )
    base.update(over)
    return base


def _seed_rollback_row(*, approval_id=_APPROVAL_ID, decision_id="dec-rb-1"):
    state = get_state()
    state.record_event("ev-rb-1", {})
    state.record_decision(
        decision_id,
        "ev-rb-1",
        {
            "decision_id": decision_id,
            "action": "rollback",
            "trace_id": "b" * 32,
            "approval": {
                "approval_id": approval_id,
                "approval_url": f"https://x/approvals/{approval_id}?t=tok",
                "expires_at": "2026-07-28T12:00:00Z",
            },
            "created_at": dt.datetime(2026, 7, 28, 11, 30, tzinfo=dt.timezone.utc),
        },
    )


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #


def test_used_approval_enriches_status_and_resolved_at(store):
    resolved = dt.datetime(2026, 7, 28, 12, 5, tzinfo=dt.timezone.utc)
    store.put(_APPROVAL_ID, **_base_fields(status="used", resolved_at=resolved))
    _seed_rollback_row()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["approval"]["status"] == "used"
    # FastAPI's (pydantic-core) JSON encoding renders a UTC-aware datetime
    # with a "Z" suffix rather than "+00:00" — same convention already
    # observed on this route's own created_at (test_decisions_endpoint.py).
    assert row["approval"]["resolved_at"] == "2026-07-28T12:05:00Z"


def test_pending_approval_enriches_status_with_none_resolved_at(store):
    store.put(_APPROVAL_ID, **_base_fields(status="pending", resolved_at=None))
    _seed_rollback_row()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["approval"]["status"] == "pending"
    assert row["approval"]["resolved_at"] is None


def test_denied_approval_enriches_status_and_resolved_at(store):
    resolved = dt.datetime(2026, 7, 28, 12, 3, tzinfo=dt.timezone.utc)
    store.put(_APPROVAL_ID, **_base_fields(status="denied", resolved_at=resolved))
    _seed_rollback_row()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["approval"]["status"] == "denied"
    assert row["approval"]["resolved_at"] == "2026-07-28T12:03:00Z"


# --------------------------------------------------------------------------- #
# honest degradation (Part C)
# --------------------------------------------------------------------------- #


def test_old_doc_with_no_resolved_at_key_degrades_honestly(store):
    """A doc predating Part A never had a ``resolved_at`` key written —
    ``Approval``'s default (None) applies. The row must still show status,
    but resolved_at stays None, and it must NOT fall back to created_at."""
    store.docs[_APPROVAL_ID] = {
        k: v for k, v in _base_fields(status="used").items() if k != "resolved_at"
    }
    _seed_rollback_row()
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["approval"]["status"] == "used"
    assert row["approval"]["resolved_at"] is None


def test_missing_approval_doc_leaves_row_unenriched(store):
    # Nothing put into the store for this id.
    _seed_rollback_row()
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert "status" not in row["approval"]
    assert "resolved_at" not in row["approval"]


def test_store_error_is_fail_soft_200(monkeypatch):
    class _BoomStore:
        def get(self, approval_id):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: _BoomStore())
    _seed_rollback_row()
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    row = resp.json()["decisions"][0]
    assert "status" not in row["approval"]
    # Sibling fields (minted at propose time) still present — un-enriched, not dropped.
    assert row["approval"]["approval_id"] == _APPROVAL_ID


# --------------------------------------------------------------------------- #
# non-rollback rows / passthrough
# --------------------------------------------------------------------------- #


def test_non_rollback_row_is_byte_identical_passthrough(store):
    state = get_state()
    state.record_event("ev-di-1", {})
    state.record_decision(
        "dec-di-1",
        "ev-di-1",
        {
            "decision_id": "dec-di-1",
            "action": "drift_issue",
            "trace_id": "c" * 32,
            "rationale": "PAYMENT_MODE drifted",
            "created_at": dt.datetime(2026, 7, 28, 11, 0, tzinfo=dt.timezone.utc),
        },
    )
    row = TestClient(app).get("/decisions").json()["decisions"][0]
    assert row["action"] == "drift_issue"
    assert "approval" not in row
    assert store.get_calls == 0  # never even attempted a read


# --------------------------------------------------------------------------- #
# Part D — per-request memoization: N rows sharing an approval_id => 1 read
# --------------------------------------------------------------------------- #


def test_repeated_approval_id_is_read_once_per_request(store):
    resolved = dt.datetime(2026, 7, 28, 12, 5, tzinfo=dt.timezone.utc)
    store.put(_APPROVAL_ID, **_base_fields(status="used", resolved_at=resolved))
    _seed_rollback_row(decision_id="dec-rb-1")
    _seed_rollback_row(decision_id="dec-rb-2")
    resp = TestClient(app).get("/decisions")
    assert resp.status_code == 200
    rows = resp.json()["decisions"]
    assert len(rows) == 2
    for row in rows:
        assert row["approval"]["status"] == "used"
    assert store.get_calls == 1
