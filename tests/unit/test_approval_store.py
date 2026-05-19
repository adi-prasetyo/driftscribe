"""Unit tests for ``driftscribe_lib.approvals`` (Phase 11.5).

The :class:`ApprovalStore` is the data layer shared between the Rollback
Agent (creates pending approvals + transactionally claims them) and the
Coordinator (Phase 11.7: reads pending approvals to render the approval
UI; later writes approved/denied status back).

These tests exercise the store against a mocked Firestore client — same
pattern as ``test_state_store.py``. The Firestore emulator is **not**
required; we stub the client surface the store actually touches
(``client.collection``, ``client.transaction``, ``firestore.transactional``).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from driftscribe_lib.approvals import (
    Approval,
    ApprovalStore,
    compute_token_hmac,
)


# --------------------------------------------------------------------------- #
# compute_token_hmac
# --------------------------------------------------------------------------- #


def test_compute_token_hmac_deterministic() -> None:
    a = compute_token_hmac("t-abc", "approval-1", "rev-1", "secret")
    b = compute_token_hmac("t-abc", "approval-1", "rev-1", "secret")
    assert a == b
    # SHA-256 hex digest is 64 chars.
    assert len(a) == 64


def test_compute_token_hmac_binds_revision() -> None:
    """A stolen approval for rev-A must not validate for rev-B."""
    a = compute_token_hmac("token", "approval-1", "rev-A", "secret")
    b = compute_token_hmac("token", "approval-1", "rev-B", "secret")
    assert a != b


def test_compute_token_hmac_binds_to_approval_id() -> None:
    """Phase 11.9 defense-in-depth: same token + same revision but two
    different ``approval_id`` values must produce different HMACs. Closes
    the theoretical cross-approval replay where an attacker correlates
    two pending approvals for the same target revision and tries to use
    approval A's token on approval B."""
    a = compute_token_hmac("token", "approval-A", "rev-1", "secret")
    b = compute_token_hmac("token", "approval-B", "rev-1", "secret")
    assert a != b


def test_compute_token_hmac_differs_per_token() -> None:
    a = compute_token_hmac("token-1", "approval-1", "rev-1", "secret")
    b = compute_token_hmac("token-2", "approval-1", "rev-1", "secret")
    assert a != b


def test_compute_token_hmac_differs_per_key() -> None:
    """If the HMAC key is rotated, previously-issued tokens must stop
    validating — confirms the key is meaningfully mixed in."""
    a = compute_token_hmac("token", "approval-1", "rev-1", "key-1")
    b = compute_token_hmac("token", "approval-1", "rev-1", "key-2")
    assert a != b


# --------------------------------------------------------------------------- #
# Fakes for the bits of Firestore we touch
# --------------------------------------------------------------------------- #


class _FakeDocRef:
    """Stand-in for ``firestore.DocumentReference`` — supports the small
    subset of methods ``ApprovalStore`` calls: ``set``, ``get``, and
    being passed into a transaction (transaction.get / transaction.update)."""

    def __init__(self, store: dict[str, dict[str, Any]], path: str) -> None:
        self._store = store
        self.path = path

    def set(self, data: dict[str, Any]) -> None:
        self._store[self.path] = dict(data)

    def get(self, transaction: Any = None) -> SimpleNamespace:
        if self.path not in self._store:
            return SimpleNamespace(exists=False, to_dict=lambda: None)
        data = dict(self._store[self.path])
        return SimpleNamespace(exists=True, to_dict=lambda: data)

    def update(self, data: dict[str, Any]) -> None:
        self._store[self.path].update(data)


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]], name: str) -> None:
        self._store = store
        self._name = name

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, f"{self._name}/{doc_id}")


class _FakeTransaction:
    """Just enough of ``firestore.Transaction`` to drive the ``@transactional``
    decorator. The real decorator wraps a function, opens a transaction, and
    retries on contention; the store under test invokes it directly, so we
    only need ``transaction.update`` to mutate via the doc ref."""

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def update(self, ref: _FakeDocRef, data: dict[str, Any]) -> None:
        ref.update(data)


class _FakeFirestore:
    """In-memory Firestore stand-in. ``client.collection(...).document(...)``
    reads/writes a single in-process dict; ``client.transaction()`` returns
    a fake that mutates the same dict."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self._store, name)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._store)

    # Test helpers
    def raw(self, path: str) -> dict[str, Any] | None:
        return self._store.get(path)


@pytest.fixture(autouse=True)
def _bypass_transactional(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@firestore.transactional`` in production wraps the function so it
    receives a real ``Transaction`` and retries on contention. For the unit
    tests we replace the decorator with a passthrough that invokes the
    wrapped function with whatever transaction the caller passed in. This
    keeps the call signature identical to production code while letting our
    fake transaction do its work synchronously and deterministically."""
    from driftscribe_lib import approvals as approvals_mod

    def passthrough(fn):  # noqa: ANN001
        def wrapper(transaction, *args, **kwargs):  # noqa: ANN001
            return fn(transaction, *args, **kwargs)
        return wrapper

    # The store imports ``firestore`` at module import time; monkeypatch the
    # already-bound name so our replacement is seen by ``claim_pending``.
    monkeypatch.setattr(approvals_mod.firestore, "transactional", passthrough)


# --------------------------------------------------------------------------- #
# ApprovalStore
# --------------------------------------------------------------------------- #


def _make_store() -> tuple[ApprovalStore, _FakeFirestore]:
    fake = _FakeFirestore()
    return ApprovalStore(project="test-proj", client=fake), fake


def test_create_returns_approval_and_raw_token() -> None:
    store, fake = _make_store()
    approval, raw_token = store.create(
        target_revision="payment-demo-00003-xyz",
        reason="rollback to last known good",
        hmac_key="test-key",
        created_by="coord@x.iam.gserviceaccount.com",
    )
    assert isinstance(approval, Approval)
    # secrets.token_urlsafe(32) → 43 chars of URL-safe base64.
    assert len(raw_token) == 43
    assert approval.status == "pending"
    assert approval.target_revision == "payment-demo-00003-xyz"
    assert approval.reason == "rollback to last known good"
    assert approval.created_by == "coord@x.iam.gserviceaccount.com"
    # The expiry is in the future and bounded by the ttl.
    now = dt.datetime.now(dt.timezone.utc)
    assert approval.expires_at > now
    assert approval.expires_at <= now + dt.timedelta(minutes=20)


def test_create_stores_hmac_not_raw_token() -> None:
    """Critical safety property — the raw token must NEVER touch Firestore.
    Only the HMAC of (token, revision) is persisted, so a Firestore leak
    cannot be exchanged for execute authority."""
    store, fake = _make_store()
    approval, raw_token = store.create(
        target_revision="rev-1",
        reason="why",
        hmac_key="k",
        created_by="who@x",
    )
    raw = fake.raw(f"approvals/{approval.approval_id}")
    assert raw is not None
    assert "token_hmac" in raw
    assert raw["token_hmac"] == compute_token_hmac(
        raw_token, approval.approval_id, "rev-1", "k"
    )
    # And the raw token itself must not appear in the document.
    assert raw_token not in raw.values()
    for v in raw.values():
        assert raw_token not in str(v)


def test_create_generates_distinct_ids_and_tokens() -> None:
    store, _ = _make_store()
    a1, t1 = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    a2, t2 = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    assert a1.approval_id != a2.approval_id
    assert t1 != t2


def test_get_returns_approval_for_existing() -> None:
    store, _ = _make_store()
    created, _ = store.create(
        target_revision="rev-1", reason="why", hmac_key="k", created_by="u@x"
    )
    fetched = store.get(created.approval_id)
    assert fetched is not None
    assert fetched.approval_id == created.approval_id
    assert fetched.target_revision == "rev-1"
    assert fetched.status == "pending"


def test_get_returns_none_for_missing() -> None:
    store, _ = _make_store()
    assert store.get("does-not-exist") is None


def test_claim_pending_flips_status_once() -> None:
    store, fake = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    claimed = store.claim_pending(created.approval_id)
    assert claimed is not None
    assert claimed.status == "used"
    # Underlying doc was actually mutated.
    raw = fake.raw(f"approvals/{created.approval_id}")
    assert raw["status"] == "used"


def test_claim_pending_returns_none_on_second_call() -> None:
    """Replay defense: only one /execute can ever flip the doc."""
    store, _ = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    first = store.claim_pending(created.approval_id)
    assert first is not None
    second = store.claim_pending(created.approval_id)
    assert second is None


def test_claim_pending_returns_none_for_missing_doc() -> None:
    store, _ = _make_store()
    assert store.claim_pending("ghost-id") is None


def test_claim_pending_returns_none_when_status_already_revoked() -> None:
    """If a coordinator-side workflow marks the approval as ``denied`` (or
    any non-pending state), the rollback worker MUST refuse to claim it."""
    store, fake = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    # Simulate the coordinator denying the approval.
    fake.raw(f"approvals/{created.approval_id}")["status"] = "denied"
    assert store.claim_pending(created.approval_id) is None


# --------------------------------------------------------------------------- #
# claim_denied (Phase 11.7 — coordinator-owned deny path)
# --------------------------------------------------------------------------- #


def test_claim_denied_flips_status_once() -> None:
    """Operator presses Reject on the approval page → coordinator flips
    pending → denied. A subsequent /execute against this approval will
    see status != "pending" and bounce out at the worker's status check."""
    store, fake = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    denied = store.claim_denied(created.approval_id)
    assert denied is not None
    assert denied.status == "denied"
    raw = fake.raw(f"approvals/{created.approval_id}")
    assert raw["status"] == "denied"


def test_claim_denied_returns_none_on_second_call() -> None:
    """Replay defense: once denied, the doc stays denied — a second Reject
    submission can't reach the worker or re-flip the doc."""
    store, _ = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    first = store.claim_denied(created.approval_id)
    assert first is not None
    second = store.claim_denied(created.approval_id)
    assert second is None


def test_claim_denied_returns_none_for_missing_doc() -> None:
    store, _ = _make_store()
    assert store.claim_denied("ghost-id") is None


def test_claim_denied_returns_none_when_status_used() -> None:
    """If the worker already executed the rollback (status=used), a later
    deny attempt must be refused — the action is no longer pending."""
    store, _ = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    store.claim_pending(created.approval_id)  # status -> used
    assert store.claim_denied(created.approval_id) is None


def test_claim_denied_then_claim_pending_both_refuse() -> None:
    """End-to-end state machine sanity: deny first, then a malicious
    /execute attempt against the same approval ID must observe status=denied
    and bounce out."""
    store, _ = _make_store()
    created, _ = store.create(
        target_revision="r", reason="x", hmac_key="k", created_by="u"
    )
    assert store.claim_denied(created.approval_id) is not None
    # Worker's transactional claim sees status=denied and refuses.
    assert store.claim_pending(created.approval_id) is None
