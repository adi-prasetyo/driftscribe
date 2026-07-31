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

import copy
import datetime as dt
import json
import urllib.parse
from typing import Any, Callable
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import agent.main as agent_main

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
# ds-hdt: shapes the WORKER CAN ACTUALLY MINT — a UUID4 id
# (``str(uuid.uuid4())``) and a 43-char urlsafe token
# (``secrets.token_urlsafe(32)``). The previous values were neither: the id
# carried non-hex characters ("abc-uuid-…") and the token was 41 chars. They
# passed while _do_rollback only checked truthiness, but described a /propose
# response ``workers/rollback/main.py`` cannot emit — and a test built on an
# impossible fixture proves nothing about production.
_APPROVAL_ID = "3f8a1c22-9d4e-4b7a-8e61-2c5d0f7a93bb"
# Shape-valid but deliberately LOW-entropy and self-describing. The guard it
# has to satisfy is _APPROVAL_TOKEN_SHAPE ([A-Za-z0-9_-]{43,64}) — alphabet and
# length, not randomness — so a readable string exercises it exactly as well as
# a random one. An earlier version of this fixture used a realistic 43-char
# base64url token and GitGuardian correctly flagged it as a Generic High
# Entropy Secret in all three files: making a fixture realistic enough to test
# the shape guard had made it indistinguishable from a live credential. The
# lesson from the "impossible fixture" defects was that a fixture must be a
# shape the producer CAN mint, not that it must look random.
_APPROVAL_TOKEN = "driftscribe-fixture-approval-token-not-real"
_APPROVAL_URL = (
    f"https://coordinator.example/approvals/{_APPROVAL_ID}?t={_APPROVAL_TOKEN}"
)
# Far-future expiry so the existing idempotency / cache-hit tests aren't
# accidentally time-sensitive after the Phase 13 Codex W2 fix (cached
# rollback decisions whose expires_at is past now-UTC are now treated as
# cache misses). A specific test below pins the past-expiry behavior.
_EXPIRES_AT_ISO = "2099-01-01T00:00:00+00:00"
# What the autonomous lane PERSISTS: same id + token, no host. The row is
# canonicalized to a relative url so a drifted worker COORDINATOR_URL can never
# put an off-origin link where the desk is the only surface (safeApprovalHref
# drops off-origin, which would recreate ds-hdt). The absolute form above is
# still what the notification body carries.
_APPROVAL_URL_ROW = f"/approvals/{_APPROVAL_ID}?t={_APPROVAL_TOKEN}"


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


_DEFAULT_LIVE_ENV = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
_DEFAULT_REVISION = "payment-demo-00042-cur"


def _adk(proposal: Any) -> AsyncMock:
    """A ``_run_adk_agent`` double that reads live state the way the real agent
    does, then reports what it saw.

    ds-q38: the production agent reaches live state through
    ``read_live_env_tool`` -> ``worker_client.call("reader", {})``, and
    ``run_agent`` reports each such response back to ``_do_recheck`` through
    ``reader_sink``, so the coordinator can confirm the decision it is about to
    persist describes the world its idempotency key names.

    So this double performs that same call against whatever dispatcher the test
    has patched in, rather than being handed a canned env. That keeps it
    faithful automatically when a test customises the Reader response — and
    faithfulness is the whole point here: the incident survived two attempted
    fixes precisely because the doubles did not behave like the pipeline.
    """

    async def _run(*_a: Any, reader_sink: list | None = None, **_k: Any) -> Any:
        if reader_sink is not None:
            try:
                payload = worker_client.call("reader", {})
            except Exception:
                # DELIBERATE SIMPLIFICATION, and the direction matters. In
                # production a Reader failure inside the tool ABORTS the turn:
                # read_live_env_tool does not catch WorkerClientError and ADK
                # re-raises a tool exception when no on_tool_error callback
                # handles it (build_agent installs none), so _do_recheck sees
                # "adk agent failed" -> 502 and never reaches the coherence
                # gate. Swallowing here lets a test keep exercising the
                # COORDINATOR-side read failure it actually cares about, with
                # the agent's own read having succeeded. The real abort is
                # pinned by test_a_reader_failure_inside_the_turn_is_a_502.
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("env"), dict):
                reader_sink.append(
                    {"env": dict(payload["env"]),
                     "revision": payload.get("revision") or None}
                )
        return proposal

    return AsyncMock(side_effect=_run)


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

    mock_run_agent = _adk(_rollback_proposal())
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
    assert body["approval"]["approval_url"] == _APPROVAL_URL_ROW
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
    # Whole-dict, deliberately: this pins the FULL propose payload, so a field
    # added to it has to be re-justified here rather than riding along.
    # ds-uwc adds the contract preview — the contract's own public literals, so
    # the worker can compute "does the target revision satisfy the contract"
    # without ever being handed an observed env value.
    payload = rollback_calls[0].args[1]
    assert payload["target_revision"] == _TARGET_REVISION
    assert payload["reason"] == _rollback_proposal().rationale
    assert payload["contract_env"] == {
        "PAYMENT_MODE": "mock",
        "FEATURE_NEW_CHECKOUT": "false",
    }
    assert isinstance(payload["contract_hash"], str) and payload["contract_hash"]
    assert set(payload) == {
        "target_revision",
        "reason",
        "contract_env",
        "contract_hash",
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


def _rollback_proposal_with_secret(secret_url: str) -> DecisionProposal:
    """A ROLLBACK proposal whose live value is a credentialed URL quoted in the
    rationale — so the source scrub (PR 2) has something real to redact in the
    worker ``reason`` (``should_redact`` fires on the credentialed value)."""
    return DecisionProposal(
        action=DecisionAction.ROLLBACK,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live=secret_url,
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision=_TARGET_REVISION,
        rationale=(
            f"PAYMENT_MODE drifted from 'mock' to {secret_url}; a previous "
            f"revision ({_TARGET_REVISION}) was contract-compliant — proposing "
            "rollback with operator approval."
        ),
        confidence=0.9,
        requires_human_review=True,
    )


def test_rollback_reason_payload_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 2 — the rollback worker ``reason`` is rendered on the approval page
    (workers/rollback/main.py), so a secret quoted in the rationale must be
    scrubbed at the source before the worker call. The benign-PAYMENT_MODE
    assertion in test 1 is the 'unchanged' regression guard; this is the
    'actually scrubs' case."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    secret_url = "https://admin:hunter2ROLL@svc.internal/api"
    mock_run_agent = _adk(_rollback_proposal_with_secret(secret_url))
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
        patch("agent.main.worker_client.call_execute"),
        patch("agent.main.worker_client.call_deny"),
    ):
        m_call.side_effect = _make_dispatch(
            live_env={"PAYMENT_MODE": secret_url, "FEATURE_NEW_CHECKOUT": "false"}
        )
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    # The boundary that matters: the worker payload (worker stores + renders it).
    rollback_calls = [c for c in m_call.call_args_list if c.args[0] == "rollback"]
    assert len(rollback_calls) == 1
    reason = rollback_calls[0].args[1]["reason"]
    assert "hunter2ROLL" not in reason
    assert secret_url not in reason
    assert "PAYMENT_MODE" in reason          # var name survives
    # And the /recheck response rationale is scrubbed too (the handler wrap).
    assert secret_url not in r.json()["rationale"]


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

    mock_run_agent = _adk(_rollback_proposal())
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


def test_notifier_failure_still_records_a_reachable_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ds-hdt: notifier failure must NOT strand the approval.

    Inverts the previous contract, deliberately. This test used to assert that
    a notifier 503 produced a 502 and released the claim so a retry could mint
    a fresh approval. That was the ds-hdt outage: the webhook pointed at
    httpbin.org, httpbin returned 503 for everything, and because the decision
    row was written only AFTER a successful notify, every autonomous rollback
    minted an approval that no surface in the product could reach
    (``ApprovalStore`` is primary-key-only; ``/infra/pending-approvals`` covers
    IaC PRs alone). The self-heal could never complete.

    The row IS the surface now — the desk renders a pending rollback from
    ``approval.approval_url`` — so notification is advisory:

    * 200, with the decision returned;
    * the approval block intact, so the desk CTA works;
    * ``notify.state == "failed"`` — honest, never silently "delivered";
    * NO exception text persisted (it can echo the tokened URL — see
      ``_notify_rollback_approval``);
    * the claim KEPT, so a retry returns the same decision instead of minting
      a second live approval for one drift.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = _adk(_rollback_proposal())

    # First call: notifier fails. Second call (same input, no force): the claim
    # is KEPT, so this must return the SAME cached decision — not re-propose.
    notifier_state: dict[str, Any] = {"fail_next": True}
    propose_calls: list[int] = []

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            propose_calls.append(1)
            return _propose_envelope()
        if worker == "notifier":
            if notifier_state["fail_next"]:
                notifier_state["fail_next"] = False
                # The body carries the tokened approval URL back — exactly what
                # httpbin.org/post does by echoing the request, and what the
                # Notifier then embeds in its own 502 detail. If any of this
                # reached the persisted row, /decisions would publish a live
                # single-use credential to anonymous demo visitors.
                raise worker_client.WorkerClientError(
                    503,
                    f"webhook returned 503: {{\"text\": \"...{_APPROVAL_URL}\"}}",
                    "notifier",
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
        # The notification failed, but the decision is durable and actionable.
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["action"] == "rollback"
        assert b1["approval"]["approval_url"] == _APPROVAL_URL_ROW
        assert b1["notify"] == {
            "state": "failed",
            "error_code": "worker_error",
            "status_code": 503,
        }
        # Second attempt — the claim was KEPT, so this returns the SAME
        # decision rather than minting a second live approval for one drift.
        r2 = client.post("/recheck")

    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["action"] == "rollback"
    assert body["approval"]["approval_url"] == _APPROVAL_URL_ROW
    assert body["decision_id"] == b1["decision_id"]
    assert len(propose_calls) == 1, "a retry must not re-propose"

    # The persisted row carries the failure classification and NOTHING that
    # could echo the credential: no exception text, no downstream body.
    stored = agent_main.get_state().get_decision(b1["decision_id"])
    assert stored["notify"]["state"] == "failed"
    assert "error" not in stored["notify"]
    flat = json.dumps(stored["notify"])
    assert _APPROVAL_TOKEN not in flat
    assert "webhook returned" not in flat


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

    mock_run_agent = _adk(_rollback_proposal())

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

    mock_run_agent = _adk(_rollback_proposal())

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
        # ds-hdt: the check is now _validated_approval (the chat lane's ds-y5i
        # correlation), not a bare truthiness test, so the detail names the
        # correlation rather than one absent field.
        assert "did not carry a usable approval" in r1.json()["detail"]
        # Nothing was recorded: a decision whose approval the operator cannot
        # act on is worse than no decision, now that the row IS the surface.
        assert agent_main.get_state().list_decisions(limit=10) == []
        # Claim was released — retry succeeds with the well-formed response.
        r2 = client.post("/recheck")

    assert r2.status_code == 200
    assert r2.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW


@pytest.mark.parametrize(
    ("mutation", "why"),
    [
        ({"approval_url": None}, "absent url"),
        ({"approval_id": "not-a-uuid"}, "id is not UUID-shaped"),
        ({"approval_token": "short"}, "token is not token-shaped"),
        ({"expires_at": "whenever"}, "expiry does not parse"),
        (
            {"approval_url": "https://evil.example/approvals/x?t=" + _APPROVAL_TOKEN},
            "url names a different approval than approval_id",
        ),
        (
            {"approval_url": f"javascript:alert(1)#/approvals/{_APPROVAL_ID}"},
            "non-navigable scheme",
        ),
    ],
)
def test_an_unusable_approval_is_never_recorded(
    monkeypatch: pytest.MonkeyPatch, mutation: dict, why: str
) -> None:
    """Every way a /propose response can fail to be ACTIONABLE must 502 before
    a decision row exists.

    ds-hdt makes the decision row the operator's surface, so a row is a promise
    that its approval can be acted on. Each mutation below breaks that promise
    in a different place — a dead CTA, a row whose status joins from one
    approval while the click executes another (ds-y5i), or an expiry that never
    retires the card because an unparseable value fail-safes to "not expired".
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = _adk(_rollback_proposal())

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return {**_propose_envelope(), **mutation}
        if worker == "notifier":
            raise AssertionError("must not notify an unusable approval")
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502, f"{why}: {r.text}"
    assert "did not carry a usable approval" in r.json()["detail"], why
    assert agent_main.get_state().list_decisions(limit=10) == [], why


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

    mock_run_agent = _adk(_rollback_proposal())

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

    mock_run_agent = _adk(_rollback_proposal())
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

    mock_run_agent = _adk(_rollback_proposal())
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
    assert r2.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW


def test_cas_loser_with_no_fresh_decision_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 15.3 (Codex carry-over from Phase 14): if the CAS-loser
    re-reads state and finds NO fresh decision (the winner has already
    evicted the old doc but its re-claim + new decision write hasn't
    landed yet), the handler must NOT fall through to ``record_event``
    and become the new proposer — that would duplicate-mint approval
    docs the moment the winner finishes.

    Surface 409 ``event in-progress, retry`` so Eventarc/the operator
    retries cleanly. The winner's re-cache write will land on the next
    attempt.

    Setup: r1 writes a cached decision and then we manually clear the
    event slot to simulate "winner has evicted but not re-cached"; r2
    forces the "expired" branch via patched
    ``_cached_rollback_is_expired``, gets a False from a stubbed
    ``evict_cached_decision`` (CAS-loser). The fall-through path would
    succeed at ``record_event`` (event slot is empty) and call a second
    /propose — that's the bug. The fix short-circuits to 409 instead.
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
            return _propose_envelope()
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = _adk(_rollback_proposal())
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
        event_key = r1.json()["event_key"]

        # Snapshot the cached decision so the initial find returns it (and
        # we enter the "expired" branch), but underneath, clear the event
        # slot to simulate "winner has evicted but not re-cached". This is
        # the precise race the fix targets: the fall-through path would
        # succeed at record_event (event slot empty) and call a second
        # /propose.
        cached_snapshot = state.find_decision_for_event(event_key)
        assert cached_snapshot is not None
        # Clear the underlying state — fall-through code path would
        # successfully claim the event again and run a second propose.
        _reset_state_for_tests()

        find_call_count = {"n": 0}

        def stub_find(ek: str):
            find_call_count["n"] += 1
            if find_call_count["n"] == 1:
                return cached_snapshot  # initial lookup → enter expired branch
            return None  # post-CAS re-read → winner mid-flight, no fresh decision

        def losing_evict(ek: str, decision_id: str) -> bool:
            return False  # CAS-loser

        # Force "expired" so we always enter the eviction branch.
        def always_expired(cached_dict: dict) -> bool:
            return True

        # Re-fetch state since we reset, then patch on the new instance.
        state = agent_main.get_state()
        with (
            patch.object(state, "find_decision_for_event", stub_find),
            patch.object(state, "evict_cached_decision", losing_evict),
            patch("agent.main._cached_rollback_is_expired", always_expired),
        ):
            r2 = client.post("/recheck")

    # The CAS-loser short-circuits with 409 — does NOT re-propose.
    assert r2.status_code == 409, r2.text
    assert "in-progress" in r2.json()["detail"].lower()
    # The rollback worker was called exactly once total (r1's propose).
    # WITHOUT the fix, fall-through reaches _do_rollback and propose runs
    # a second time (propose_call_count == 2).
    assert propose_call_count["n"] == 1, (
        "CAS-loser must NOT re-propose when no fresh decision is available"
    )


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


# --------------------------------------------------------------------------- #
# ds-b3m — the /recheck call site must never hand the validator a live_env it
# rebuilt from the model's own diffs.
#
# On the ADK path a failed Reader Worker read is not fatal: the coordinator
# reconstructs a live_env-shaped dict out of ``proposal.env_diffs`` purely so
# the idempotency event key stays stable. That reconstruction is the model's
# array wearing a different name, and feeding it to a gate whose whole job is to
# corroborate the model INDEPENDENTLY makes the gate re-derive the model's own
# answer. This is the same laundering shape ds-qua shipped and had to fix.
# --------------------------------------------------------------------------- #


def test_rollback_refused_when_the_reader_read_fails_on_the_adk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reader down → no ground truth → the rollback is refused (502), NOT
    proposed off the back of the reconstructed env.

    The reconstruction still happens (the event key needs it), so this test is
    exactly the difference between "we kept the cache key working" and "we let
    the cache key vouch for a traffic shift"."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            raise RuntimeError("reader worker unreachable")
        if worker == "rollback":
            raise AssertionError(
                "the rollback worker must NOT be called: without an observed "
                "env there is nothing to corroborate the proposal"
            )
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = _adk(_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
        patch("agent.main.worker_client.call_execute") as m_execute,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r = client.post("/recheck")

    # 502 with the safety-gate detail — the ADK branch's mapping for a proposal
    # the deterministic gate refused.
    assert r.status_code == 502, r.text
    assert "safety gate" in r.text
    assert "no observed live env" in r.text
    m_execute.assert_not_called()


def test_rollback_refused_when_the_reader_returns_a_malformed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 from the reader is not the same as an observation.

    A payload whose ``env`` is not a dict of str->str is treated as NO ground
    truth rather than being coerced to ``{}`` — an empty dict would tell the
    gate that every contract var is missing, which reads as a confident
    'everything has drifted' instead of an honest 'we could not look'."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return {"service": "payment-demo", "env": {"PAYMENT_MODE": ["live"]}}
        if worker == "rollback":
            raise AssertionError("must not propose off a malformed reader read")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = _adk(_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 502, r.text
    assert "no observed live env" in r.text


@pytest.mark.parametrize(
    "bad_payload",
    [
        pytest.param({"service": "payment-demo"}, id="env-key-missing"),
        pytest.param({"service": "payment-demo", "env": None}, id="env-is-null"),
        pytest.param({"service": "payment-demo", "env": []}, id="env-is-a-list"),
        pytest.param({"service": "payment-demo", "env": "PAYMENT_MODE=live"}, id="env-is-a-string"),
        pytest.param("not-a-dict-at-all", id="payload-is-not-a-dict"),
    ],
)
def test_a_reader_payload_with_no_usable_env_block_is_not_read_as_an_empty_env(
    monkeypatch: pytest.MonkeyPatch, bad_payload: Any
) -> None:
    """Coercing an unusable payload to ``{}`` is the dangerous degradation here,
    not a tidy one, and it is worth its own test because it FAILS OPEN.

    An empty observed env makes every contract-declared disallow_manual var read
    as "not at its contract value" — so a malformed reader response would
    manufacture a hard violation and AUTHORIZE the rollback it was supposed to
    make unprovable. `None` (no observation) refuses; `{}` (a real service with
    no env block) is a genuine observation. Only the wrapper shape distinguishes
    them, so the wrapper shape is what gets checked."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return bad_payload
        if worker == "rollback":
            raise AssertionError("must not propose off an unusable reader read")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = _adk(_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 502, r.text
    assert "no observed live env" in r.text


def test_rollback_refused_when_observed_env_contradicts_the_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE POINT OF ds-b3m, end to end.

    The model returns a well-formed rollback proposal claiming PAYMENT_MODE has
    drifted to 'live'. Every check that reads the proposal is satisfied — the
    diff derives present_disallow_manual from the real contract. But the Reader
    Worker says PAYMENT_MODE is 'mock'. Before this change the coordinator
    proposed the rollback and minted an approval anyway, because nothing ever
    compared the claim to the service."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(
                {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
            )
        if worker == "rollback":
            raise AssertionError(
                "no approval may be minted for a violation the service does "
                "not actually have"
            )
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    mock_run_agent = _adk(_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 502, r.text
    assert "no CONFIRMED hard contract violation" in r.text


def test_rollback_still_proposed_when_observed_env_corroborates_the_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The demo baseline must be unaffected: PAYMENT_MODE drifted, the
    operator-toggleable FEATURE_NEW_CHECKOUT at its contract value. This is the
    exact live shape the autonomous self-heal runs against, and ds-2f5 is the
    reminder of what a gate that over-refuses costs (a genuine violation was
    rejected 3/3 on prod, and the self-heal was silently dead)."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = _adk(_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
        patch("agent.main.worker_client.call_execute") as m_execute,
    ):
        m_call.side_effect = _make_dispatch(
            live_env={"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        )
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    assert r.json()["action"] == "rollback"
    assert r.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW
    m_execute.assert_not_called()  # still HITL — nothing executed


def test_an_ungrounded_recheck_cannot_reuse_a_grounded_decision_from_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached decision is returned BEFORE validate() runs, so the event key
    is what actually keeps an ungrounded run away from a grounded one's approval.

    The collision this pins is not exotic. The model has already seen the
    reader's result, so an accurate report reconstructs the exact dict the
    coordinator would have read — same content, same hash, same key. The key is
    deliberately left alone; what is guarded is the RETURN. Without that guard
    the second call here is served the first call's rollback approval and
    validate()'s refusal never gets to speak.

    Run 1: reader OK, rollback proposed and cached.
    Run 2: reader down, model reports the SAME env → identical reconstruction.
           Must 502, not hand back run 1's approval."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    grounded_env = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}

    def failing_reader(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            raise RuntimeError("reader worker unreachable")
        if worker == "rollback":
            raise AssertionError("must not mint a second approval")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    # The proposal's diffs reconstruct exactly `grounded_env` (PAYMENT_MODE=live
    # is reported; FEATURE_NEW_CHECKOUT is what makes them differ, so report it
    # too — this is the WORST case for the cache, i.e. a perfectly accurate model).
    proposal = _rollback_proposal()
    proposal.env_diffs.append(
        EnvDiff(
            name="FEATURE_NEW_CHECKOUT",
            expected="false",
            live="false",
            contract_status=ContractStatus.MATCH,
            debug_config_value=None,
            recent_pr_match=None,
        )
    )

    with (
        patch("agent.main._run_adk_agent", _adk(proposal)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _make_dispatch(live_env=grounded_env)
        r1 = TestClient(app).post("/recheck")
    assert r1.status_code == 200, r1.text
    assert r1.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW

    with (
        patch("agent.main._run_adk_agent", _adk(proposal)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = failing_reader
        r2 = TestClient(app).post("/recheck")

    assert r2.status_code == 502, r2.text
    assert "no observed live env" in r2.text
    assert _APPROVAL_TOKEN not in r2.text


def test_classifier_path_502s_on_a_malformed_reader_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-ADK path has no reconstruction to fall back on, so a payload it
    cannot read is an upstream failure — the same 502 a transport error gets,
    not a silent empty env fed to the classifier."""
    monkeypatch.setenv("USE_ADK", "false")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return {"service": "payment-demo", "env": ["PAYMENT_MODE=live"]}
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with patch("agent.main.worker_client.call") as m_call:
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502, r.text
    assert "malformed env payload" in r.text


def test_a_well_formed_empty_env_is_treated_as_an_observation_not_a_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service really can have no env block, so `env: {}` from a well-formed
    payload is something we OBSERVED — not a failure to observe.

    Both outcomes are a refusal, which is why this needs its own test: the
    difference is only visible in WHICH refusal. An observed empty env refuses
    for "no hard contract violation" (we looked, and there is nothing to
    revert); a failed read refuses for "no observed live env" (we could not
    look). Collapsing the two — `if not live_env` instead of `if live_env is
    None` at the call site — would also discard a real observation and swap in
    the model's reconstruction, so the cached-rollback guard would start
    refusing requests that did in fact observe the service."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({})
        if worker == "rollback":
            raise AssertionError("nothing observed justifies a rollback")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502, r.text
    assert "no CONFIRMED hard contract violation" in r.text
    assert "no observed live env" not in r.text


def test_a_non_rollback_action_keeps_ONE_idempotency_slot_across_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provenance namespace must not reach non-rollback actions.

    A first draft closed the rollback hole by splitting the EVENT KEY on env
    provenance, which gave docs_pr and drift_issue two cache slots — one per
    provenance. A grounded first attempt followed by a retry that hit a reader
    blip would then MISS the cache and run `_perform_action` again, opening a
    SECOND GitHub issue, breaking the duplicate-suppression the `record_event`
    claim is built on. Guarding the cache READ instead leaves every key
    untouched, so this case is simply unaffected.

    Caught by Codex — the fix for one lane quietly regressing another.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    env = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
    proposal = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="live",
                contract_status=ContractStatus.PRESENT_DISALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            ),
            EnvDiff(
                name="FEATURE_NEW_CHECKOUT",
                expected="false",
                live="false",
                contract_status=ContractStatus.MATCH,
                debug_config_value=None,
                recent_pr_match=None,
            ),
        ],
        target_docs_file=None,
        target_docs_section=None,
        target_revision=None,
        rationale="PAYMENT_MODE drifted; filing an issue for the operator.",
        confidence=0.9,
        requires_human_review=True,
    )

    side_effects: list[str] = []

    def _perform(*args: Any, **kwargs: Any) -> dict:
        side_effects.append("issue")
        return {"url": "https://github.com/x/y/issues/1", "action": "drift_issue"}

    def grounded(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(env)
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    def reader_down(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            raise RuntimeError("reader worker unreachable")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    for dispatch in (grounded, reader_down):
        with (
            patch("agent.main._run_adk_agent", _adk(proposal)),
            patch("agent.main._perform_action", _perform),
            patch("agent.main.worker_client.call") as m_call,
        ):
            m_call.side_effect = dispatch
            r = TestClient(app).post("/recheck")
        assert r.status_code == 200, r.text

    # ONE issue, not two: the retry hit the first run's cached decision even
    # though its env provenance differed.
    assert side_effects == ["issue"]


def test_an_ungrounded_run_cannot_be_served_a_cached_rollback_VIA_ANOTHER_ACTION(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard has to judge the CACHED decision's action, not the incoming
    proposal's — and getting that backwards is a live bypass, not a nicety.

    A first fix split the event key by provenance and scoped the split to
    proposals whose action was ROLLBACK. That still let this through:

      1. grounded run proposes ROLLBACK, cached under key K;
      2. ungrounded run reconstructs the identical env, but the model happens
         to propose DRIFT_ISSUE;
      3. the new proposal is non-rollback, so the key is still K;
      4. the cache returns the ROLLBACK approval, before validate() runs.

    What is being handed back is what has to be judged. Caught by Codex on the
    fourth pass.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    env = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
    rollback = _rollback_proposal()
    rollback.env_diffs.append(
        EnvDiff(
            name="FEATURE_NEW_CHECKOUT",
            expected="false",
            live="false",
            contract_status=ContractStatus.MATCH,
            debug_config_value=None,
            recent_pr_match=None,
        )
    )
    # Same env_diffs → same reconstruction → same event key as the run above.
    issue = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
        env_diffs=list(rollback.env_diffs),
        target_docs_file=None,
        target_docs_section=None,
        target_revision=None,
        rationale="Filing an issue instead.",
        confidence=0.9,
        requires_human_review=True,
    )

    with (
        patch("agent.main._run_adk_agent", _adk(rollback)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _make_dispatch(live_env=env)
        r1 = TestClient(app).post("/recheck")
    assert r1.status_code == 200, r1.text
    assert r1.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW

    def reader_down(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            raise RuntimeError("reader worker unreachable")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent", _adk(issue)),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = reader_down
        r2 = TestClient(app).post("/recheck")

    assert r2.status_code == 502, r2.text
    assert _APPROVAL_TOKEN not in r2.text


def test_a_claim_LOSER_is_not_handed_a_concurrent_runs_cached_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third cached-rollback return site: the record_event claim-loser.

    Only a NON-rollback proposal reaches that code (a rollback branches out at
    _do_rollback well above), so it reads as unrelated to the rollback gate —
    which is exactly why it was the one of three that got missed. The race:

      1. an ungrounded DRIFT_ISSUE request misses the cache on its first lookup;
      2. a concurrent GROUNDED run records a rollback under the same key;
      3. the ungrounded request loses record_event();
      4. its re-read finds that rollback and returns the approval — before any
         gate has spoken for this request.

    ds-q38 CLOSED THE ROUTE rather than the guard. An ungrounded ADK run is now
    refused before it can claim anything, because a decision nothing observed
    must not be persisted under any key. So this asserts both halves and the
    distinction matters: the request is refused AND never shown the approval,
    and ``_cached_rollback_needs_ground_truth`` — still consulted at that site
    as defense in depth — is pinned directly, so closing the route does not
    quietly retire the guard that would catch a future route to it.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    proposal = DecisionProposal(
        action=DecisionAction.DRIFT_ISSUE,
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
        target_revision=None,
        rationale="Filing an issue.",
        confidence=0.9,
        requires_human_review=True,
    )

    cached_rollback = {
        "decision_id": "d-concurrent",
        "action": "rollback",
        "approval": {"approval_url": _APPROVAL_URL, "expires_at": _EXPIRES_AT_ISO},
    }

    lookups: list[int] = []

    # Captured BEFORE the patch, or the delegate below recurses into itself.
    real_state = agent_main.get_state()

    class _RacingState:
        """First lookup empty (so we get past the cache check), claim refused,
        second lookup finds the concurrent run's rollback."""

        def __getattr__(self, name):  # noqa: ANN001 — delegate everything else
            return getattr(real_state, name)

        def find_decision_for_event(self, key):  # noqa: ANN001
            lookups.append(1)
            return None if len(lookups) == 1 else cached_rollback

        def record_event(self, *a: Any, **k: Any) -> bool:
            return False  # lost the claim

    def reader_down(worker: str, payload: dict, *a: Any, **k: Any) -> Any:
        if worker == "reader":
            raise RuntimeError("reader worker unreachable")
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker call: {worker!r}")

    with (
        patch("agent.main._run_adk_agent", _adk(proposal)),
        patch("agent.main.get_state", lambda: _RacingState()),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = reader_down
        r = TestClient(app).post("/recheck")

    # Refused before the claim: nothing ungrounded gets that far any more.
    assert r.status_code == 409, r.text
    assert _APPROVAL_TOKEN not in r.text
    assert _APPROVAL_URL not in r.text

    # The site's own guard is unchanged and still refuses, so a future path
    # that DOES reach it with no ground truth is still caught.
    assert agent_main._cached_rollback_needs_ground_truth(cached_rollback, None) is True
    assert (
        agent_main._cached_rollback_needs_ground_truth(
            cached_rollback, {"PAYMENT_MODE": "live"}
        )
        is False
    )


# --------------------------------------------------------------------------- #
# ds-hdt ordering invariants: the decision row is written BEFORE the
# notification, and only the notification is allowed to fail softly.
# --------------------------------------------------------------------------- #


def _rollback_dispatch(notifier: Callable[[], Any]) -> Callable[..., Any]:
    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return _propose_envelope()
        if worker == "notifier":
            return notifier()
        raise AssertionError(f"unexpected worker {worker!r}")

    return dispatch


def test_the_decision_is_durable_before_the_notification_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row must already be readable at the moment notify runs.

    This is the ds-hdt ordering, pinned directly rather than inferred from a
    happy path. It matters because /eventarc fast-acks (#268) and runs the
    audit in a background task, so nothing re-delivers a coordinator that dies
    mid-notify: a row written afterwards would simply be lost, stranding the
    approval with no surface AND no retry.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    seen_at_notify_time: list[Any] = []

    def notifier() -> Any:
        # Read the store from INSIDE the notifier call. DEEP copy: the
        # in-memory store hands back live references, so the outcome patch that
        # runs after this returns would otherwise rewrite what we "observed"
        # mid-flight and the assertion below would read the final state.
        seen_at_notify_time.append(
            copy.deepcopy(list(agent_main.get_state().list_decisions(limit=10)))
        )
        return _notifier_envelope()

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _rollback_dispatch(notifier)
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert len(seen_at_notify_time) == 1
    rows = seen_at_notify_time[0]
    assert len(rows) == 1, "the decision must be durable before notify runs"
    assert rows[0]["approval"]["approval_url"] == _APPROVAL_URL_ROW
    # Mid-flight the delivery outcome is genuinely not known yet — "pending",
    # never a premature "delivered".
    assert rows[0]["notify"] == {"state": "pending"}
    # ...and it settles to delivered once the call returns.
    assert r.json()["notify"] == {"state": "delivered"}


def test_a_failed_decision_write_suppresses_the_notification_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No durable surface ⇒ do not notify.

    Delivering a link to an approval that appears nowhere is the ds-hdt failure
    wearing a different hat: the operator gets a URL, the desk shows nothing,
    and the approval dies at its 15-min expiry. Release the claim so a retry
    can mint a fresh one, and 502.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def notifier() -> Any:
        raise AssertionError("must not notify when the decision was not recorded")

    state = agent_main.get_state()
    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        patch.object(
            type(state), "record_decision", side_effect=RuntimeError("firestore down")
        ),
    ):
        m_call.side_effect = _rollback_dispatch(notifier)
        r = TestClient(app).post("/recheck")

    assert r.status_code == 502
    assert "could not be recorded" in r.json()["detail"]
    # Nothing was persisted, and the claim was released so a retry can
    # re-propose rather than meeting a 409 from an orphan claim.
    assert agent_main.get_state().list_decisions(limit=10) == []


def test_an_unexpected_notifier_exception_is_contained_like_a_worker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-WorkerClientError escape must not strand the approval either.

    ``except WorkerClientError`` alone would have left a bug in the client, or
    any transport surprise, re-opening exactly the hole this closes — so the
    handler is deliberately broad and classifies rather than propagates.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def notifier() -> Any:
        raise ValueError("not a WorkerClientError at all")

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = _rollback_dispatch(notifier)
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"]["approval_url"] == _APPROVAL_URL_ROW
    assert body["notify"] == {"state": "failed", "error_code": "internal_error"}
    # No status_code (there was no HTTP exchange) and, as ever, no message text.
    assert "status_code" not in body["notify"]
    assert "not a WorkerClientError" not in json.dumps(body["notify"])


def test_a_failed_outcome_patch_never_fails_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is already durable and actionable; losing the delivery
    annotation must not 500 the audit. It stays ``pending``, which is honest —
    we genuinely no longer know what became of the delivery."""
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    state = agent_main.get_state()
    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        patch.object(
            type(state),
            "set_decision_notify_outcome",
            side_effect=RuntimeError("firestore blip"),
        ),
    ):
        m_call.side_effect = _rollback_dispatch(_notifier_envelope)
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert r.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW
    stored = agent_main.get_state().list_decisions(limit=10)[0]
    assert stored["notify"] == {"state": "pending"}


# --------------------------------------------------------------------------- #
# ds-hdt: the recorded approval url is canonicalized to same-origin.
#
# _approval_url_matches deliberately accepts ANY http(s) origin (see its
# docstring and ds-x5l): the worker derives the url from its own
# COORDINATOR_URL, that value has drifted from the coordinator's before, and
# rejecting a mismatch would turn routine config drift into a total rollback
# outage. The residual was accepted on the grounds that the SPA declines the
# CTA but the chat reply and the webhook still carry the raw string.
#
# That justification does not survive making the row the operator's only
# surface: the autonomous lane has no chat reply, and the webhook is the thing
# that is down. So the row is rebuilt from the two parts already exact-verified
# (path == /approvals/{id}, exactly one well-shaped t=) with no host at all.
# --------------------------------------------------------------------------- #


def _propose_envelope_on(origin: str) -> dict[str, Any]:
    """A propose response whose url is valid in every way EXCEPT its host."""
    return {
        "approval_id": _APPROVAL_ID,
        "approval_token": _APPROVAL_TOKEN,
        "approval_url": f"{origin}/approvals/{_APPROVAL_ID}?t={_APPROVAL_TOKEN}",
        "expires_at": _EXPIRES_AT_ISO,
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://coordinator.example",  # the ordinary case
        "https://evil.example",  # hostile host, otherwise perfectly shaped
        "http://driftscribe-agent-OLD-hash-an.a.run.app",  # drifted COORDINATOR_URL
    ],
)
def test_the_recorded_approval_url_is_always_same_origin(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    """Whatever host the worker names, the persisted row carries none.

    The hostile-host case passes _validated_approval on purpose — id, token,
    expiry, path and the single query pair all correlate, so shape validation
    has nothing to object to. The existing malformed-response test does NOT
    cover it: its foreign-origin url names ``/approvals/x``, so it dies on id
    correlation long before the origin would matter.

    Note we do NOT reject: a drifted-but-honest COORDINATOR_URL must keep
    working, which is why the third case is here beside the second.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return _propose_envelope_on(origin)
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    recorded = r.json()["approval"]["approval_url"]
    # Host-less, and exactly the id+token the worker minted.
    assert recorded == _APPROVAL_URL_ROW
    parsed = urllib.parse.urlsplit(recorded)
    assert parsed.scheme == "" and parsed.netloc == ""
    # The property the desk actually needs (safeApprovalHref keeps only
    # pathname + search, and requires the /approvals/ prefix).
    assert parsed.path == f"/approvals/{_APPROVAL_ID}"
    assert urllib.parse.parse_qs(parsed.query)["t"] == [_APPROVAL_TOKEN]
    # No trace of a foreign host in the persisted APPROVAL BLOCK. Deliberately
    # not the whole row: ``rendered_body`` is the notification text and keeps
    # the worker's absolute url by design, so a whole-row assertion here would
    # be asserting the opposite of
    # test_the_notification_still_carries_an_absolute_url.
    stored = agent_main.get_state().list_decisions(limit=10)[0]
    assert stored["approval"]["approval_url"] == _APPROVAL_URL_ROW
    assert "evil.example" not in json.dumps(stored["approval"])
    # The canonical form must still satisfy the validator it came from, so a
    # round-trip through it is stable.
    from agent.adk_tools import _approval_url_matches

    assert _approval_url_matches(recorded, _APPROVAL_ID)


def test_the_notification_still_carries_an_absolute_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonicalizing the ROW must not relativize the NOTIFICATION.

    A relative link in a Discord/Slack message is unusable — there is no page
    to resolve it against. The row and the notification legitimately differ,
    so pin the pair together or a later tidy-up will collapse them.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    sent: list[dict[str, Any]] = []

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return _propose_envelope()
        if worker == "notifier":
            sent.append(payload)
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker {worker!r}")

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        r = TestClient(app).post("/recheck")

    assert r.status_code == 200, r.text
    assert len(sent) == 1
    assert _APPROVAL_URL in sent[0]["body"], "notification lost its absolute url"
    assert r.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW


def test_an_unexpected_notifier_error_never_logs_its_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The exception-text ban covers Cloud Logging, not just the decision row.

    An earlier draft used ``log.exception`` here and justified it as "Cloud
    Logging only, never the body". That is not something the raising code
    guarantees: a serialization error is exactly the class that quotes the
    payload it choked on, and the payload is the rendered body carrying the
    tokened approval url. Cloud Logging is access-controlled, not a safe home
    for a live credential.

    So this pins THREE things about the log record: no ``exc_info`` (a
    traceback would carry frame locals holding the body), the raised message
    absent, and the token absent. Asserting only "state == failed" would pass
    against the version this exists to prevent.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    # A message shaped like the leak we are guarding against: a serializer
    # quoting the payload it could not encode.
    secret_bearing_message = (
        f"cannot serialize field 'body': <{_APPROVAL_URL}> is not JSON"
    )

    def exploding_notifier() -> Any:
        raise TypeError(secret_bearing_message)

    with (
        patch("agent.main._run_adk_agent", _adk(_rollback_proposal())),
        patch("agent.main.worker_client.call") as m_call,
        caplog.at_level("DEBUG"),
    ):
        m_call.side_effect = _rollback_dispatch(exploding_notifier)
        r = TestClient(app).post("/recheck")

    # Contained: the approval is still recorded and actionable.
    assert r.status_code == 200, r.text
    assert r.json()["notify"] == {
        "state": "failed",
        "error_code": "internal_error",
    }
    assert r.json()["approval"]["approval_url"] == _APPROVAL_URL_ROW

    records = [rec for rec in caplog.records if "notify" in rec.getMessage()]
    assert records, "the unexpected-notifier failure was not logged at all"
    for rec in records:
        assert rec.exc_info is None, (
            "a traceback was attached; frame locals can carry the rendered body"
        )
        formatted = rec.getMessage() + repr(getattr(rec, "__dict__", {}))
        assert secret_bearing_message not in formatted
        assert _APPROVAL_TOKEN not in formatted
        # The useful classification IS present — this must not be "log nothing".
        assert getattr(rec, "exc_type", None) == "TypeError"
