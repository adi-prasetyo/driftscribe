"""Integration tests for the coordinator's HITL approval UI (Phase 11.7).

End-to-end coverage of the /approvals/{id} GET + POST routes:

- GET renders the page with the approval details + a form with the
  hidden token field. Security headers (no-store, no-referrer, frame
  deny) are set.
- POST with decision=approve calls the rollback worker's /execute via
  worker_client.call_execute. On success the page re-renders showing
  the new status.
- POST with decision=reject transactionally flips status pending→denied
  via ApprovalStore.claim_denied. Replay returns 403.
- Replay (POST twice) returns 403.
- Missing form fields return 422.

Mocking strategy:
- ``agent.approvals.get_approval_store`` returns an in-memory fake
  ApprovalStore (same FakeApprovalStore shape as workers/rollback's
  tests, except we only need ``get`` / ``claim_denied`` / ``create``).
- ``agent.main.worker_client.call_execute`` is monkeypatched so we
  never mint a real ID token or POST to a real worker URL.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent import approvals as approval_helpers
from agent import worker_client
from agent.main import app
from driftscribe_lib.approvals import Approval


class _FakeApprovalStore:
    """In-memory ApprovalStore — supports the subset the coordinator uses.

    The Rollback Agent's tests already vet the real store's transactional
    flip semantics; here we only need a place to write/read approval docs.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def create_pending(
        self,
        *,
        target_revision: str,
        reason: str,
        created_by: str = "coordinator@test",
        ttl_minutes: int = 15,
    ) -> Approval:
        """Test helper: write a pending doc directly without going through
        the worker's /propose path."""
        approval_id = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        data = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": "fake-hmac",
            "expires_at": now + dt.timedelta(minutes=ttl_minutes),
            "created_at": now,
            "created_by": created_by,
        }
        self.docs[approval_id] = data
        return Approval(approval_id=approval_id, **data)

    def get(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_denied(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        data = self.docs[approval_id]
        if data.get("status") != "pending":
            return None
        data["status"] = "denied"
        return Approval(approval_id=approval_id, **data)

    def claim_pending(self, approval_id: str) -> Approval | None:
        # Not called in approval tests, but provided for completeness.
        if approval_id not in self.docs:
            return None
        data = self.docs[approval_id]
        if data.get("status") != "pending":
            return None
        data["status"] = "used"
        return Approval(approval_id=approval_id, **data)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeApprovalStore:
    """Per-test in-memory store, monkeypatched into the coordinator's
    approval-store accessor."""
    s = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: s)
    return s


class _ExecuteRecorder:
    """Wrapper around the fake ``call_execute`` patch.

    Exposes:
    - ``.calls``: list of (approval_id, token) tuples actually invoked
    - ``.state["raises"]``: set to an exception to make the next call
      raise (used to simulate worker rejection paths)
    - ``.state["returns"]``: prepend dicts to override the default
      success response
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.state: dict[str, Any] = {"returns": [], "raises": None}

    def __iter__(self):
        return iter(self.calls)

    def __eq__(self, other):
        return self.calls == other

    def __len__(self):
        return len(self.calls)


@pytest.fixture
def execute_calls(monkeypatch: pytest.MonkeyPatch) -> _ExecuteRecorder:
    """Records every ``call_execute(approval_id, token)`` invocation."""
    rec = _ExecuteRecorder()

    def fake_execute(approval_id: str, approval_token: str) -> dict:
        rec.calls.append((approval_id, approval_token))
        if rec.state["raises"] is not None:
            raise rec.state["raises"]
        if rec.state["returns"]:
            return rec.state["returns"].pop(0)
        return {
            "approval_id": approval_id,
            "target_revision": "payment-demo-00002-bbb",
            "status": "executed",
            "operation_name": "operations/fake-op",
        }

    monkeypatch.setattr(worker_client, "call_execute", fake_execute)
    return rec


@pytest.fixture
def client(store, execute_calls) -> TestClient:
    """TestClient with store + execute_calls already wired."""
    return TestClient(app)


# --------------------------------------------------------------------------- #
# GET /approvals/{id}
# --------------------------------------------------------------------------- #


def test_get_renders_pending_approval_with_form(client, store) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb",
        reason="rollback to last known good",
    )
    r = client.get(f"/approvals/{approval.approval_id}?t=raw-token-abc")
    assert r.status_code == 200
    body = r.text
    # Page content reflects the approval.
    assert "payment-demo-00002-bbb" in body
    assert "rollback to last known good" in body
    # Form is rendered with both buttons.
    assert f'action="/approvals/{approval.approval_id}"' in body
    assert 'name="decision"' in body
    assert 'value="approve"' in body
    assert 'value="reject"' in body
    # Token is in the hidden field — operator's click POSTs it back.
    assert 'name="t"' in body
    assert 'value="raw-token-abc"' in body


def test_get_security_headers_set(client, store) -> None:
    """Cache-Control, Referrer-Policy, X-Frame-Options MUST be set on
    the response — the URL carries the raw approval token and the
    headers minimize the surfaces where it could leak."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_get_missing_approval_renders_not_found_page(client) -> None:
    """A probing GET for a non-existent ID returns 200 with a
    not-found message, NOT 404. We don't want the status code to
    distinguish "doc exists" from "doc doesn't exist" for an
    unauthenticated probe."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    r = client.get(f"/approvals/{fake_id}?t=anything")
    assert r.status_code == 200
    assert "not found" in r.text.lower()
    # No form should be rendered for a missing approval.
    assert 'value="approve"' not in r.text


def test_get_already_used_approval_hides_form(client, store) -> None:
    """If status != pending, the form is replaced with a 'resolved'
    message. The page itself doesn't 403 — that's enforced at POST."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    store.docs[approval.approval_id]["status"] = "used"
    r = client.get(f"/approvals/{approval.approval_id}?t=anything")
    assert r.status_code == 200
    assert 'value="approve"' not in r.text
    assert "resolved" in r.text.lower()


def test_get_expired_approval_shows_expired_message(client, store) -> None:
    """Once past the TTL, the form is hidden and an explicit 'expired'
    message rendered. The TTL is enforced at /execute on the worker
    side; this is purely UX."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    store.docs[approval.approval_id]["expires_at"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    )
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    assert "expired" in r.text.lower()
    assert 'value="approve"' not in r.text


def test_get_no_external_assets(client, store) -> None:
    """The approval page MUST not pull from any external CDN — that
    would let a CDN compromise inject JS into the approval flow. Pin
    that the rendered HTML contains no external script/style tags."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    text = r.text
    # No external <script src=...>
    assert 'src="http' not in text
    assert 'src="//' not in text
    # No external <link href=... rel="stylesheet">
    assert 'href="http' not in text
    assert 'href="//' not in text


# --------------------------------------------------------------------------- #
# POST /approvals/{id} — approve path
# --------------------------------------------------------------------------- #


def test_post_approve_calls_worker_execute(client, store, execute_calls) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "approve"},
    )
    assert r.status_code == 200, r.text
    # The coordinator delegated to worker_client.call_execute.
    assert execute_calls == [(approval.approval_id, "raw-token-abc")]
    # Security headers still set on POST response.
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_post_approve_does_not_flip_status_locally(client, store, execute_calls) -> None:
    """The coordinator does NOT update the approval status itself on
    approve — that's the worker's job (transactional pending→used
    via the worker's claim_pending). The coordinator only calls the
    worker; the worker owns the state transition."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    # The fake worker fixture doesn't update the store (it would in
    # real life — but here we want to assert the coordinator itself
    # doesn't touch the doc on approve).
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 200
    # Status STILL pending (the worker would have flipped it; our
    # fake didn't because we want to isolate "did the coordinator
    # touch the doc itself").
    assert store.docs[approval.approval_id]["status"] == "pending"


def test_post_approve_worker_failure_surfaces_403(client, store, execute_calls) -> None:
    """If the worker rejects /execute (bad token, expired, replay),
    the operator gets a 403. The detail should NOT echo the worker's
    full body — that could include internal URLs or stack traces."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    execute_calls.state["raises"] = worker_client.WorkerClientError(
        403, "approval status is 'used'", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# POST /approvals/{id} — reject path
# --------------------------------------------------------------------------- #


def test_post_reject_flips_status_to_denied(client, store, execute_calls) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r.status_code == 200, r.text
    # Coordinator-owned transition: pending → denied.
    assert store.docs[approval.approval_id]["status"] == "denied"
    # Worker was NOT called for the reject path.
    assert execute_calls == []


def test_post_reject_replay_returns_403(client, store, execute_calls) -> None:
    """Second reject submission on an already-denied approval → 403.
    The transactional claim_denied returns None on non-pending docs,
    which the handler maps to 403."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r1 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r2.status_code == 403


def test_post_reject_for_missing_approval_returns_403(client, store) -> None:
    fake_id = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/approvals/{fake_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Form validation
# --------------------------------------------------------------------------- #


def test_post_missing_token_returns_422(client, store) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"decision": "approve"},  # no `t`
    )
    assert r.status_code == 422


def test_post_invalid_decision_returns_422(client, store) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "delete"},  # not approve/reject
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Cross-path: approve after reject (or vice versa)
# --------------------------------------------------------------------------- #


def test_post_approve_after_reject_returns_403(client, store, execute_calls) -> None:
    """If the operator already rejected, a malicious follow-up approve
    request must fail. The worker's /execute will see status="denied"
    and return 403; the coordinator surfaces that as 403."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r1 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r1.status_code == 200

    # Simulate the worker's response for an already-denied approval.
    execute_calls.state["raises"] = worker_client.WorkerClientError(
        403, "approval status is 'denied'", "rollback"
    )
    r2 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r2.status_code == 403
