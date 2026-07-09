"""Integration tests for the coordinator's HITL approval UI (Phase 11.7+11.9).

End-to-end coverage of the /approvals/{id} GET + POST routes:

- GET renders the page with the approval details + a form with the
  hidden token field. Security headers (no-store, no-referrer, frame
  deny) are set.
- POST with decision=approve calls the rollback worker's /execute via
  worker_client.call_execute. On success the page re-renders showing
  the new status.
- POST with decision=reject calls the rollback worker's /deny via
  worker_client.call_deny (Phase 11.9 fix — the pre-11.9 path bypassed
  HMAC verification, a HITL availability bug). The fake fixture also
  flips the store doc so the re-rendered page reflects the new state.
- Replay (POST twice) returns 403.
- Worker 409 (tag preflight) passes through; worker 5xx maps to 502;
  other worker 4xx collapses to 403 (Phase 11.9 watch item #2).
- Missing form fields return 422.

Mocking strategy:
- ``agent.approvals.get_approval_store`` returns an in-memory fake
  ApprovalStore.
- ``agent.main.worker_client.call_execute`` and ``call_deny`` are
  monkeypatched so we never mint a real ID token or POST to a real
  worker URL. The fakes ALSO mutate the in-memory store so the
  coordinator's re-fetch picks up the new status — matching production
  where the worker performs the transactional flip.
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


class _WorkerCallRecorder:
    """Wrapper around the fake ``call_execute`` / ``call_deny`` patches.

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


# Back-compat alias — many tests still reference this name.
_ExecuteRecorder = _WorkerCallRecorder


@pytest.fixture
def execute_calls(
    monkeypatch: pytest.MonkeyPatch, store: _FakeApprovalStore
) -> _WorkerCallRecorder:
    """Records every ``call_execute(approval_id, token)`` invocation.

    The fake worker mirrors production semantics — on a successful call
    it flips the store doc to ``"used"`` (the way the real rollback
    worker's transactional claim_pending would), so the coordinator's
    re-fetch picks up the new status. Tests that want to assert the
    coordinator did NOT touch the doc itself can inspect the store
    before/after (the fake worker is the only thing flipping)."""
    rec = _WorkerCallRecorder()

    def fake_execute(approval_id: str, approval_token: str) -> dict:
        rec.calls.append((approval_id, approval_token))
        if rec.state["raises"] is not None:
            raise rec.state["raises"]
        if rec.state["returns"]:
            return rec.state["returns"].pop(0)
        # Production parity: the worker flips status as part of /execute.
        store.claim_pending(approval_id)
        return {
            "approval_id": approval_id,
            "target_revision": "payment-demo-00002-bbb",
            "status": "executed",
            "operation_name": "operations/fake-op",
        }

    monkeypatch.setattr(worker_client, "call_execute", fake_execute)
    return rec


@pytest.fixture
def deny_calls(
    monkeypatch: pytest.MonkeyPatch, store: _FakeApprovalStore
) -> _WorkerCallRecorder:
    """Records every ``call_deny(approval_id, token)`` invocation.

    Mirrors :fixture:`execute_calls` exactly — Phase 11.9 moved the
    deny operation to the rollback worker so the test surface matches.
    The fake flips the store doc to ``"denied"`` on success."""
    rec = _WorkerCallRecorder()

    def fake_deny(approval_id: str, approval_token: str) -> dict:
        rec.calls.append((approval_id, approval_token))
        if rec.state["raises"] is not None:
            raise rec.state["raises"]
        if rec.state["returns"]:
            return rec.state["returns"].pop(0)
        store.claim_denied(approval_id)
        return {"approval_id": approval_id, "status": "denied"}

    monkeypatch.setattr(worker_client, "call_deny", fake_deny)
    return rec


@pytest.fixture
def client(store, execute_calls, deny_calls) -> TestClient:
    """TestClient with store + execute_calls + deny_calls already wired."""
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


@pytest.mark.parametrize(
    "query",
    [
        "",                       # no ?t at all (link cut before the token)
        "?t=",                    # empty token value
        "?t=%3Credacted%3E",      # the demo scrub's literal placeholder, URL-encoded
        "?t=<redacted>",          # ...and pasted raw (client encodes it the same way)
    ],
)
def test_get_pending_without_usable_token_explains_instead_of_form(
    client, store, query
) -> None:
    """A pending approval reached WITHOUT a usable one-time token (link cut
    short, or a visitor pasting the ``?t=<redacted>`` placeholder that the
    surviving scrubs — /runs, read_conversations, model-facing reads — still
    emit) renders an explanatory note INSTEAD of the Approve/Reject form. Both
    POST actions need the real token (the worker verifies the HMAC), so the
    form could only manufacture a doomed POST — observed live 2026-07-08 as a
    raw 422 on a tokenless Approve click. Post-2026-07-09 the note points the
    visitor back to the chat reply for the full link."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.get(f"/approvals/{approval.approval_id}{query}")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="no-token-note"' in body
    assert "chat reply" in body
    # No form at all: neither button, no hidden token field.
    assert 'value="approve"' not in body
    assert 'value="reject"' not in body
    assert 'name="t"' not in body
    # The approval details still render (this is a view, not an error page).
    assert "payment-demo-00002-bbb" in body


def test_get_no_token_note_outranks_paused_note(client, store, monkeypatch) -> None:
    """Token-missing outranks the paused display: the paused/autonomy notes
    describe Approve/Reject button states, and with no usable token there are
    no buttons to describe."""
    from agent import main as agent_main
    from agent.pause import PauseState

    monkeypatch.setattr(
        agent_main,
        "_pause_state_fail_closed",
        lambda: PauseState(paused=True, reason="test pause"),
    )
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.get(f"/approvals/{approval.approval_id}")
    assert r.status_code == 200
    assert 'data-testid="no-token-note"' in r.text
    assert 'data-testid="paused-note"' not in r.text
    assert 'value="approve"' not in r.text


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


def test_post_approve_does_not_flip_status_locally(
    client, store, execute_calls, monkeypatch
) -> None:
    """The coordinator does NOT update the approval status itself on
    approve — that's the worker's job (transactional pending→used
    via the worker's claim_pending). The coordinator only calls the
    worker; the worker owns the state transition.

    To isolate "did the coordinator touch the doc itself", we override
    the fake worker so it returns success WITHOUT flipping the store.
    The status should remain pending — proving the coordinator's reject
    path itself does not mutate Firestore."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )

    # Override the auto-flipping fake — return success without touching
    # the store, so any status flip we observe must come from the
    # coordinator itself (which it shouldn't).
    def fake_execute_no_flip(approval_id, approval_token):  # noqa: ANN001
        return {
            "approval_id": approval_id,
            "target_revision": "payment-demo-00002-bbb",
            "status": "executed",
            "operation_name": "operations/fake-op",
        }

    monkeypatch.setattr(worker_client, "call_execute", fake_execute_no_flip)

    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 200
    # Status STILL pending — the coordinator itself did not flip it.
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
# POST /approvals/{id} — reject path (Phase 11.9)
# --------------------------------------------------------------------------- #
#
# Codex review of 11.7 (critical finding #1): the pre-11.9 reject path
# called approval_helpers.deny() on the coordinator directly without
# validating the approval token. Anyone holding just the approval_id
# could deny a pending rollback (HITL availability bug). The fix routes
# /reject through worker_client.call_deny — same shape as the approve
# path, so the rollback worker (the only service holding the HMAC key)
# verifies the operator's intent on both decision paths.


def test_post_reject_calls_worker_deny(client, store, deny_calls, execute_calls) -> None:
    """The coordinator delegates the reject decision to the rollback
    worker's /deny via call_deny — mirroring the approve path's call to
    /execute. Worker performs the HMAC verify + transactional flip; the
    coordinator does not touch Firestore status directly."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "reject"},
    )
    assert r.status_code == 200, r.text
    # call_deny was invoked with the operator-supplied token.
    assert deny_calls == [(approval.approval_id, "raw-token-abc")]
    # call_execute was NOT invoked for the reject path.
    assert execute_calls == []
    # The fake worker's claim_denied flipped the doc — production parity.
    assert store.docs[approval.approval_id]["status"] == "denied"
    # Security headers still set on POST response.
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_post_reject_wrong_token_returns_403(
    client, store, deny_calls
) -> None:
    """Critical: a leaked approval_id alone must not be sufficient to
    deny a pending rollback. When the worker rejects the token (HMAC
    mismatch) the coordinator surfaces 403, identical to the approve
    path's bad-token behavior. This is the security property the pre-
    11.9 design lacked entirely."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    deny_calls.state["raises"] = worker_client.WorkerClientError(
        403, "invalid approval token", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "guessed-or-leaked", "decision": "reject"},
    )
    assert r.status_code == 403
    # Coordinator must not have flipped the doc — the worker refused.
    assert store.docs[approval.approval_id]["status"] == "pending"


def test_post_reject_replay_returns_403(client, store, deny_calls) -> None:
    """Second reject submission on an already-denied approval → 403.
    The first call succeeds and the worker (fake) flips status to denied;
    the second call hits the worker's status pre-check and gets 403."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    r1 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r1.status_code == 200
    # Simulate the worker's response for the second attempt — it would
    # see status="denied" and refuse with 403.
    deny_calls.state["raises"] = worker_client.WorkerClientError(
        403, "approval status is 'denied'", "rollback"
    )
    r2 = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r2.status_code == 403


def test_post_reject_for_missing_approval_returns_403(
    client, store, deny_calls
) -> None:
    """Worker returns 404 for missing approvals; the coordinator
    collapses non-409/non-5xx errors to 403 so probing cannot use the
    response code to enumerate approval doc existence."""
    fake_id = "00000000-0000-0000-0000-000000000000"
    deny_calls.state["raises"] = worker_client.WorkerClientError(
        404, "approval not found", "rollback"
    )
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


# --------------------------------------------------------------------------- #
# Worker error mapping (Phase 11.9 watch item #2)
# --------------------------------------------------------------------------- #
#
# The pre-11.9 coordinator collapsed every worker error into 403. Codex
# review of 11.7 flagged that this destroys operationally important
# signals: a 409 tag-preflight conflict (operator can clear the tag and
# retry the SAME approval) and a 5xx worker outage are both materially
# different from "your approval token is bad". The fix in
# :func:`agent.main._map_worker_error`:
#   - 409 → passes through
#   - 5xx → maps to 502 (upstream availability)
#   - other 4xx → collapses to 403 (state enumeration defense preserved)


def test_post_approve_worker_409_passes_through(
    client, store, execute_calls
) -> None:
    """Tag-preflight conflict is an operationally recoverable state —
    operator clears the tag and retries the same approval. The 409 MUST
    NOT be collapsed to 403, otherwise the operator can't tell the
    failure from a token/state error."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    execute_calls.state["raises"] = worker_client.WorkerClientError(
        409, "service has a tagged traffic target", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 409


def test_post_approve_worker_5xx_maps_to_502(
    client, store, execute_calls
) -> None:
    """Worker outage / transport failure is upstream-availability, not
    "your approval is bad". Map to 502 so observability + retries can
    distinguish."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    execute_calls.state["raises"] = worker_client.WorkerClientError(
        503, "rollback unreachable: ConnectError", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "approve"},
    )
    assert r.status_code == 502


def test_post_approve_worker_403_stays_403(
    client, store, execute_calls
) -> None:
    """All non-409, non-5xx worker errors still collapse to 403 so an
    unauthenticated probe can't enumerate approval state from response
    codes (was it expired? wrong token? already used? all 403)."""
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
    # And the detail must not echo the worker's body verbatim — leaking
    # "approval status is 'used'" back to the operator would let an
    # unauthenticated probe enumerate state. The generic mapping in
    # :func:`agent.main._map_worker_error` strips the worker's body.
    assert "'used'" not in r.text


def test_post_reject_worker_409_passes_through(
    client, store, deny_calls
) -> None:
    """Same mapping holds on the reject path — 409 passes through."""
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    deny_calls.state["raises"] = worker_client.WorkerClientError(
        409, "concurrent state change", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r.status_code == 409


def test_post_reject_worker_5xx_maps_to_502(
    client, store, deny_calls
) -> None:
    approval = store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    deny_calls.state["raises"] = worker_client.WorkerClientError(
        503, "rollback unreachable", "rollback"
    )
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "tok", "decision": "reject"},
    )
    assert r.status_code == 502
