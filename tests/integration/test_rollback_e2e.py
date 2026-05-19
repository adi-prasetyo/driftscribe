"""End-to-end integration tests for the ROLLBACK control flow (Phase 13.3).

Closes Phase 11.9 Codex carry-over #3: ``DecisionAction.ROLLBACK`` must
preserve the worker/HITL boundary. The model can PROPOSE rollback; the
operator's click is the ONLY gate that runs ``/execute``. These tests pin
that property explicitly — Test 2 below is the safety assertion the
carry-over calls for.

What's mocked vs. exercised end-to-end:

- ``_run_adk_agent`` — mocked to return a canned ROLLBACK proposal so we
  don't need a live Gemini call. The structured-output contract is
  exercised by unit tests over ``DecisionProposal``; here we focus on
  the orchestrator routing.
- ``worker_client.call`` — dispatched per-worker via a side_effect table.
  Returns the canonical envelope shapes (reader, rollback, notifier) the
  real workers produce. This mocks at the HTTP boundary, so the
  coordinator's own dispatch (``call(...) → mint_id_token → httpx.post``)
  is bypassed and we exercise the coordinator's response shape + state
  transitions.
- ``worker_client.call_execute`` / ``call_deny`` — mocked at the call
  site (``agent.main.worker_client.call_execute`` etc.) so the operator
  approval POST tests assert "did we call the right worker function with
  the right args" without minting an ID token.

The approval POST tests (3 + 4) intentionally don't run a recheck first —
they directly POST to ``/approvals/{id}`` with a known approval doc
already in the in-memory store. This isolates the operator-decision path
from the propose-recheck path; the propose-recheck path is covered by
tests 1, 2, 5, 6.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent import approvals as approval_helpers
from agent import worker_client
from agent.config import get_settings
from agent.main import _reset_state_for_tests, app
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
from driftscribe_lib.approvals import Approval


# --------------------------------------------------------------------------- #
# Canned fixtures & helpers
# --------------------------------------------------------------------------- #


_TARGET_REVISION = "payment-demo-00041-xyz"
_APPROVAL_ID = "abc-uuid-1234-5678-9012-345678901234"
_APPROVAL_TOKEN = "tok-xyz-43chars-aaaaaaaaaaaaaaaaaaaaaaaaa"
_APPROVAL_URL = (
    f"https://coordinator.example/approvals/{_APPROVAL_ID}?t={_APPROVAL_TOKEN}"
)
# Far-future expiry so the existing idempotency / cache-hit tests aren't
# accidentally time-sensitive after the Phase 13 Codex W2 fix (cached
# rollback decisions whose expires_at is past now-UTC are now treated as
# cache misses). A specific test below pins the past-expiry behavior.
_EXPIRES_AT_ISO = "2099-01-01T00:00:00+00:00"


def _rollback_proposal() -> DecisionProposal:
    """Canonical ROLLBACK proposal the validator will accept.

    PAYMENT_MODE is the demo's allow_manual_change=false variable, so a
    diff with contract_status=present_disallow_manual is exactly the case
    the validator policy admits for rollback.
    """
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision=_TARGET_REVISION,
        rationale=(
            "PAYMENT_MODE drifted from 'mock' to 'live'; the contract marks "
            "this var as allow_manual_change=false. A previous revision "
            f"({_TARGET_REVISION}) was contract-compliant — proposing rollback "
            "with operator approval."
        ),
        confidence=0.9,
        requires_human_review=True,
    )


def _reader_envelope(env: dict[str, str]) -> dict[str, Any]:
    """Shape a Reader Worker /read response — same as other integration tests."""
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00042-cur",
    }


def _propose_envelope() -> dict[str, Any]:
    """Shape a Rollback Worker /propose response. The real worker returns
    approval_token alongside approval_url; the coordinator MUST NOT echo
    approval_token back to its caller (URL embeds the token already)."""
    return {
        "approval_id": _APPROVAL_ID,
        "approval_token": _APPROVAL_TOKEN,
        "approval_url": _APPROVAL_URL,
        "expires_at": _EXPIRES_AT_ISO,
    }


def _notifier_envelope() -> dict[str, Any]:
    """Shape a Notifier Worker /notify response on a successful webhook post."""
    return {
        "status": "ok",
        "channel": "approval",
        "severity": "high",
        "downstream_status": 200,
    }


def _make_dispatch(
    *,
    live_env: dict[str, str] | None = None,
    propose: dict[str, Any] | Exception | None = None,
    notify: dict[str, Any] | Exception | None = None,
) -> Callable[..., Any]:
    """Build a ``worker_client.call(worker, payload, ...)`` dispatcher.

    Each kwarg is the response for that worker; if it's an Exception
    instance the dispatcher raises it (used to simulate worker failures).
    Sensible defaults so a test only specifies what it cares about.
    """
    if live_env is None:
        live_env = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
    if propose is None:
        propose = _propose_envelope()
    if notify is None:
        notify = _notifier_envelope()

    reader_response = _reader_envelope(live_env)

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return reader_response
        if worker == "rollback":
            if isinstance(propose, Exception):
                raise propose
            return propose
        if worker == "notifier":
            if isinstance(notify, Exception):
                raise notify
            return notify
        raise AssertionError(f"unexpected worker call: {worker!r}")

    return dispatch


# --------------------------------------------------------------------------- #
# /recheck — ADK proposes ROLLBACK
# --------------------------------------------------------------------------- #


def test_rollback_recheck_routes_through_worker_and_renders_approval_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 1: the full happy path — propose-via-worker → render → notify.

    Asserts:
    - Response action is ``rollback`` and decision_path is ``adk``.
    - The approval URL the worker returned is faithfully present in the
      response and embedded in the rendered body.
    - ``approval_token`` is NOT present in the response anywhere (the URL
      is the only carrier — exposing the token as a separate field would
      double the leak surface).
    - The rollback worker was called exactly once with the proposal's
      target_revision + rationale.
    - The notifier worker was called exactly once with the approval
      channel + the rendered body.
    - Neither ``call_execute`` nor ``call_deny`` was invoked — the operator
      has not yet clicked.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
        patch("agent.main.worker_client.call_execute") as m_execute,
        patch("agent.main.worker_client.call_deny") as m_deny,
    ):
        m_call.side_effect = _make_dispatch()
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "rollback"
    assert body["decision_path"] == "adk"
    assert body["target_revision"] == _TARGET_REVISION
    assert body["requires_human_review"] is True

    # Approval block shape — approval_url is the only token carrier.
    assert body["approval"]["approval_id"] == _APPROVAL_ID
    assert body["approval"]["approval_url"] == _APPROVAL_URL
    assert body["approval"]["expires_at"] == _EXPIRES_AT_ISO
    # approval_token MUST NOT appear anywhere in the response body — the
    # URL already embeds ?t=<token>; exposing it separately doubles the
    # leak surface. Scan the whole serialized JSON, not just the approval
    # subdict, in case a future refactor moves it elsewhere.
    body_json = r.text
    assert "approval_token" not in body_json
    # And the rendered body MUST contain the operator-facing approval URL
    # so the Notifier delivery is self-sufficient.
    assert _APPROVAL_URL in body["rendered_body"]

    # The rollback worker was called exactly once with the canonical payload.
    rollback_calls = [c for c in m_call.call_args_list if c.args[0] == "rollback"]
    assert len(rollback_calls) == 1
    assert rollback_calls[0].args[1] == {
        "target_revision": _TARGET_REVISION,
        "reason": _rollback_proposal().rationale,
    }

    # The notifier was called exactly once with channel=approval +
    # severity=high + the rendered body containing the approval URL.
    notifier_calls = [c for c in m_call.call_args_list if c.args[0] == "notifier"]
    assert len(notifier_calls) == 1
    notifier_payload = notifier_calls[0].args[1]
    assert notifier_payload["channel"] == "approval"
    assert notifier_payload["severity"] == "high"
    assert _APPROVAL_URL in notifier_payload["body"]

    # NEITHER execute NOR deny was invoked — the operator has not clicked.
    m_execute.assert_not_called()
    m_deny.assert_not_called()


def test_rollback_decision_does_not_execute_the_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 2: Phase 11.9 Codex carry-over #3 — the safety property.

    A ROLLBACK ``DecisionProposal`` flowing through ``/recheck`` MUST NOT
    cause any Cloud Run mutation. The coordinator's only side effects are
    (a) the rollback worker's /propose call and (b) the notifier's
    webhook. The Cloud Run admin client (which the rollback worker calls
    from /execute, not from /propose) MUST NOT be reachable on this path.

    Validated here by asserting that ``worker_client.call_execute`` and
    ``worker_client.call_deny`` are NEVER invoked, and the only workers
    contacted are reader/rollback/notifier — none of which mutate Cloud
    Run on the /propose surface. ``dry_run`` is intentionally left at the
    autouse default (True) — the assertion is that no execution happens
    regardless of dry-run-ness, and the contrast point ("dry_run=False
    would also not execute") is documented here rather than exercised
    separately so the test stays under the FirestoreStateStore-bypass
    constraint in conftest (which keys InMemoryStateStore on dry_run).
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
        patch("agent.main.worker_client.call_execute") as m_execute,
        patch("agent.main.worker_client.call_deny") as m_deny,
    ):
        m_call.side_effect = _make_dispatch()
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "rollback"

    # The HITL safety property: no /execute call, no /deny call.
    m_execute.assert_not_called()
    m_deny.assert_not_called()

    # No worker other than reader / rollback / notifier was contacted —
    # specifically, no call_execute path that would route to the rollback
    # worker's /execute (the only Cloud Run mutation surface).
    workers_called = {c.args[0] for c in m_call.call_args_list}
    assert workers_called <= {"reader", "rollback", "notifier"}

    # Phase 13 Codex W4: also defend against an endpoint override. A future
    # ``worker_client.call("rollback", payload, endpoint="/execute")`` would
    # bypass the worker-name allowlist above but is still a HITL violation.
    # Pin that no call site overrode endpoint to a mutation surface.
    for call in m_call.call_args_list:
        endpoint = call.kwargs.get("endpoint")
        assert endpoint not in ("/execute", "/deny"), (
            f"worker_client.call invoked with endpoint={endpoint!r} — that "
            f"is a mutation surface and must only be reached via "
            f"call_execute/call_deny on the operator-POST path."
        )


# --------------------------------------------------------------------------- #
# /approvals/{id} POST — operator-click path
# --------------------------------------------------------------------------- #
#
# These two tests directly exercise the existing approval POST handler in
# agent/main.py. They do NOT run /recheck first — the goal is to pin that
# the handler routes approve/reject to the right worker call. The recheck-
# to-approval handoff is covered structurally by Test 1 (the approval_url
# is faithfully present in the response).


class _FakeApprovalStore:
    """Minimal in-memory ApprovalStore used by the approval POST tests.

    Mirrors the shape of the fake in ``test_approvals.py`` but kept local
    so a refactor of one doesn't silently move the other."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def create_pending(self) -> Approval:
        now = dt.datetime.now(dt.timezone.utc)
        data = {
            "status": "pending",
            "target_revision": _TARGET_REVISION,
            "reason": "rollback proposed",
            "token_hmac": "fake-hmac",
            "expires_at": now + dt.timedelta(minutes=15),
            "created_at": now,
            "created_by": "coordinator@test",
        }
        self.docs[_APPROVAL_ID] = data
        return Approval(approval_id=_APPROVAL_ID, **data)

    def get(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_pending(self, approval_id: str) -> Approval | None:
        d = self.docs.get(approval_id)
        if not d or d["status"] != "pending":
            return None
        d["status"] = "used"
        return Approval(approval_id=approval_id, **d)

    def claim_denied(self, approval_id: str) -> Approval | None:
        d = self.docs.get(approval_id)
        if not d or d["status"] != "pending":
            return None
        d["status"] = "denied"
        return Approval(approval_id=approval_id, **d)


def test_operator_approve_post_routes_to_worker_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 3: the operator's Approve click POSTs to /approvals/{id} →
    coordinator's existing handler calls worker_client.call_execute with
    the approval_id and the token from the form.

    This is the second half of the HITL flow: the rollback ONLY runs when
    the operator clicks, and "clicking" means a token-bearing POST that
    the handler authenticates by handing the token to the rollback
    worker's /execute (which is the only service holding the HMAC key)."""
    store = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: store)
    approval = store.create_pending()

    execute_calls: list[tuple[str, str]] = []

    def fake_execute(approval_id: str, token: str) -> dict:
        execute_calls.append((approval_id, token))
        # Production parity: the worker flips status as part of /execute.
        store.claim_pending(approval_id)
        return {
            "approval_id": approval_id,
            "status": "executed",
            "target_revision": _TARGET_REVISION,
            "operation_name": "operations/fake-op",
        }

    monkeypatch.setattr(worker_client, "call_execute", fake_execute)

    client = TestClient(app)
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": _APPROVAL_TOKEN, "decision": "approve"},
    )
    assert r.status_code == 200, r.text
    # call_execute was invoked with the right approval_id and the
    # operator-supplied token (from the form's hidden field).
    assert execute_calls == [(approval.approval_id, _APPROVAL_TOKEN)]
    # The fake worker (mirroring production) flipped the doc's status.
    assert store.docs[approval.approval_id]["status"] == "used"


def test_operator_reject_post_routes_to_worker_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 4: the operator's Reject click POSTs to /approvals/{id} →
    coordinator's existing handler calls worker_client.call_deny with
    the approval_id and the token from the form. Phase 11.9 moved the
    deny operation to the worker (the only service holding the HMAC
    key); the coordinator no longer flips status directly."""
    store = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: store)
    approval = store.create_pending()

    deny_calls: list[tuple[str, str]] = []

    def fake_deny(approval_id: str, token: str) -> dict:
        deny_calls.append((approval_id, token))
        store.claim_denied(approval_id)
        return {"approval_id": approval_id, "status": "denied"}

    monkeypatch.setattr(worker_client, "call_deny", fake_deny)

    client = TestClient(app)
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": _APPROVAL_TOKEN, "decision": "reject"},
    )
    assert r.status_code == 200, r.text
    assert deny_calls == [(approval.approval_id, _APPROVAL_TOKEN)]
    assert store.docs[approval.approval_id]["status"] == "denied"


# --------------------------------------------------------------------------- #
# Failure modes — claim release & idempotent retry
# --------------------------------------------------------------------------- #


def test_notifier_failure_releases_claim_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 5: notifier failure rolls back the event-key claim.

    The rollback worker succeeds (the approval doc exists in Firestore
    with its 15-min TTL), but the notifier raises 503. The coordinator
    surfaces 502 — and CRITICALLY releases the claim so a subsequent
    recheck (same input, no force) can re-propose rather than 409.

    This matches the existing claim-release semantics for the other
    actions (test_side_effect_failure_releases_claim_so_retry_can_proceed
    in test_recheck_dry_run.py)."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())

    # First call: notifier fails. Second call (same input, no force):
    # claim must be released so this succeeds with a fresh proposal.
    notifier_state: dict[str, Any] = {"fail_next": True}

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return _propose_envelope()
        if worker == "notifier":
            if notifier_state["fail_next"]:
                notifier_state["fail_next"] = False
                raise worker_client.WorkerClientError(
                    503, "downstream webhook timeout", "notifier"
                )
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r1 = client.post("/recheck")
        # First attempt: notifier raised → 502 with the notify failure detail.
        assert r1.status_code == 502
        assert "rollback notify failed" in r1.json()["detail"]
        # Second attempt — claim was released, so this re-runs cleanly.
        r2 = client.post("/recheck")

    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["action"] == "rollback"
    assert body["approval"]["approval_url"] == _APPROVAL_URL


def test_propose_failure_releases_claim_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bonus failure mode: the rollback worker's /propose itself fails.

    Symmetric with the notifier-failure test — the coordinator must
    release the claim on a propose failure so the operator's retry
    isn't met with a 409 from an orphan claim."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())

    propose_state: dict[str, Any] = {"fail_next": True}

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            if propose_state["fail_next"]:
                propose_state["fail_next"] = False
                raise worker_client.WorkerClientError(
                    503, "rollback worker unreachable", "rollback"
                )
            return _propose_envelope()
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r1 = client.post("/recheck")
        assert r1.status_code == 502
        assert "rollback propose failed" in r1.json()["detail"]
        r2 = client.post("/recheck")

    assert r2.status_code == 200
    assert r2.json()["action"] == "rollback"


def test_malformed_propose_response_returns_502_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bonus failure mode: the rollback worker returns 200 but with an
    incomplete body (missing approval_url). The coordinator MUST refuse
    rather than render a broken approval body, and the claim MUST be
    released so retries can succeed once the worker is fixed."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())

    propose_state: dict[str, Any] = {"first": True}

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            if propose_state["first"]:
                propose_state["first"] = False
                # Malformed — approval_id is there but approval_url is missing.
                return {
                    "approval_id": _APPROVAL_ID,
                    "approval_token": _APPROVAL_TOKEN,
                    "expires_at": _EXPIRES_AT_ISO,
                }
            return _propose_envelope()
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r1 = client.post("/recheck")
        assert r1.status_code == 502
        assert "missing approval_url" in r1.json()["detail"]
        # Claim was released — retry succeeds with the well-formed response.
        r2 = client.post("/recheck")

    assert r2.status_code == 200
    assert r2.json()["approval"]["approval_url"] == _APPROVAL_URL


def test_idempotent_retry_returns_cached_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 6: a second /recheck with the same input returns the cached
    decision rather than minting a fresh approval.

    This is the same idempotency contract the other actions have
    (test_recheck_same_live_env_returns_cached_decision in
    test_recheck_dry_run.py). For rollback the property is especially
    important: re-running a recheck should NOT cause a second approval
    doc to appear in Firestore, otherwise an operator who already saw
    the first URL would be confused by a second pending approval."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _make_dispatch()
        client = TestClient(app)
        r1 = client.post("/recheck")
        r2 = client.post("/recheck")

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Same decision_id, same approval URL, same event_key — cache hit.
    assert r1.json()["decision_id"] == r2.json()["decision_id"]
    assert (
        r1.json()["approval"]["approval_url"]
        == r2.json()["approval"]["approval_url"]
    )
    assert r1.json()["event_key"] == r2.json()["event_key"]

    # The rollback worker was called exactly ONCE across both /recheck calls
    # — the cached decision was returned without re-proposing.
    rollback_calls = [c for c in m_call.call_args_list if c.args[0] == "rollback"]
    assert len(rollback_calls) == 1


def test_cached_rollback_with_expired_approval_re_proposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 13 Codex W2: a cached rollback decision past its 15-min TTL
    must NOT be returned as a cache hit.

    Without this guard, an operator who re-runs ``/recheck`` 16+ minutes
    after the first rollback proposal would receive the dead approval URL
    from the cache, with no way to recover short of ``force=true``. With
    the guard, the expired cached decision is treated as a cache miss and
    a fresh approval is minted.

    The first call uses a deliberately stale ``expires_at`` (10 minutes in
    the past); the second call uses the default far-future fixture.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    stale_propose = {
        "approval_id": _APPROVAL_ID,
        "approval_token": _APPROVAL_TOKEN,
        "approval_url": _APPROVAL_URL,
        "expires_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        ).isoformat(),
    }
    fresh_propose = _propose_envelope()  # uses _EXPIRES_AT_ISO (far future)

    propose_results = [stale_propose, fresh_propose]
    propose_call_count = {"n": 0}

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(
                {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
            )
        if worker == "rollback":
            i = propose_call_count["n"]
            propose_call_count["n"] += 1
            return propose_results[i]
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r1 = client.post("/recheck")
        r2 = client.post("/recheck")

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # First response carries the now-stale expires_at.
    assert r1.json()["approval"]["expires_at"] == stale_propose["expires_at"]
    # Second response: cache hit was DROPPED (expired), so a fresh propose
    # ran — operator gets the new far-future expires_at.
    assert r2.json()["approval"]["expires_at"] == _EXPIRES_AT_ISO
    # Rollback worker was called TWICE total — once per /recheck, because
    # the first cache entry was treated as a miss.
    assert propose_call_count["n"] == 2


def test_concurrent_expired_rollback_evictions_only_one_re_proposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 14 (Codex Phase 13 second-pass W2): two concurrent /recheck
    retries observing the same expired cached rollback must NOT both
    re-propose. The compare-and-delete eviction (evict_cached_decision)
    ensures exactly one caller wins; the loser returns the winner's fresh
    decision instead of minting a parallel approval doc.

    Simulating real Firestore concurrency in-process is impossible, so we
    pin the eviction-CAS contract directly: the loser's
    ``evict_cached_decision`` returns False (it lost the race). At that
    point the cache lookup must re-read state and return the fresh
    decision written by the winner, NOT issue a second /propose call
    against the rollback worker.

    Setup: r1 runs cleanly and writes a far-future cached decision. The
    loser /recheck is then forced through the "stale cache" branch by
    monkeypatching ``_cached_rollback_is_expired`` to lie once, while
    ``evict_cached_decision`` is replaced with a stub that returns False
    (the CAS-loser outcome on real Firestore).
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    propose_call_count = {"n": 0}

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(
                {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
            )
        if worker == "rollback":
            propose_call_count["n"] += 1
            return _propose_envelope()  # far-future expires_at
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        # Winner call: empty cache → propose runs → fresh decision cached.
        r1 = client.post("/recheck")
        assert r1.status_code == 200, r1.text
        assert propose_call_count["n"] == 1

        from agent import main as agent_main

        state = agent_main.get_state()
        cached = state.find_decision_for_event(r1.json()["event_key"])
        assert cached is not None
        assert cached["approval"]["expires_at"] == _EXPIRES_AT_ISO

        evict_calls: list[tuple[str, str]] = []

        def losing_evict(event_key: str, decision_id: str) -> bool:
            evict_calls.append((event_key, decision_id))
            return False  # CAS-loser

        expired_calls = {"n": 0}
        real_is_expired = agent_main._cached_rollback_is_expired

        def flaky_is_expired(cached_dict: dict) -> bool:
            expired_calls["n"] += 1
            # First call (initial cache lookup): claim expired so we enter
            # the eviction branch. Re-read after the failed CAS: defer to
            # the real check (which returns False on the far-future doc).
            if expired_calls["n"] == 1:
                return True
            return real_is_expired(cached_dict)

        with (
            patch.object(state, "evict_cached_decision", losing_evict),
            patch(
                "agent.main._cached_rollback_is_expired",
                flaky_is_expired,
            ),
        ):
            r2 = client.post("/recheck")

    assert r2.status_code == 200, r2.text
    # The CAS-loser must NOT issue a second /propose.
    assert propose_call_count["n"] == 1, (
        "loser must NOT re-propose after losing the eviction CAS"
    )
    assert len(evict_calls) == 1
    assert evict_calls[0][1] == r1.json()["decision_id"]
    # And the loser returns the winner's fresh decision verbatim.
    assert r2.json()["decision_id"] == r1.json()["decision_id"]
    assert r2.json()["approval"]["approval_url"] == _APPROVAL_URL


def test_rollback_on_non_adk_path_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: if a ROLLBACK proposal somehow appears on the
    classifier path (impossible in current code — the classifier has no
    rollback branch — but pinned here so a future classifier extension
    can't silently bypass the ADK-only assumption), the coordinator
    refuses with 500.

    The 500 is intentional: a rollback on the classifier path means the
    coordinator deploy is broken (classifier emitted an action it can't
    on this revision). Surfacing as 502 would mislead the on-call into
    chasing an upstream-failure root cause."""
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()
    _reset_state_for_tests()

    # The classifier doesn't emit ROLLBACK on any input, so we can't
    # naturally reach this branch — we patch classify to return one.
    with (
        patch("agent.main.classify", return_value=_rollback_proposal()),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _make_dispatch()
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 500
    assert "rollback action emitted on non-ADK path" in r.json()["detail"]
