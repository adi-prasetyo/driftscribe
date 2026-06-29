"""Integration tests for the autonomy-dial gates (ClickOps item 11).

Part 1 (this commit, Task 6): Observe-mode SUPPRESSION in the drift pipeline —
the pipeline still runs and RECORDS a "would have" decision, but the GitHub
action / rollback worker calls are suppressed. Modeled on
``tests/integration/test_pause_gates.py`` (same TestClient + in-memory store +
real POST-to-toggle pattern) and ``test_recheck_use_adk_path.py`` /
``test_rollback_e2e.py`` (mocked ``_run_adk_agent`` returning canned proposals).

The dial is toggled through the REAL ``POST /autonomy`` endpoint so the gates
read the same in-memory StateStore singleton via ``get_state()``.

Part 2 (apply gates) lives below, added in Task 7.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import agent.main as main_mod
from agent import approvals as approval_helpers
from agent import iac_csrf, worker_client
from agent.auth import require_cf_operator
from agent.config import get_settings
from agent.autonomy import AutonomyState, autonomy_apply_blocked_detail
from agent.iac_artifacts import C2CommentRef, IacPlanView
from agent.main import _reset_state_for_tests, app, get_state
from agent.models import ContractStatus, DecisionAction, DecisionProposal, EnvDiff
from agent.pause import PAUSED_DETAIL
from driftscribe_lib.approvals import Approval


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _set_mode(client: TestClient, mode: str, reason: str = "test") -> None:
    """Toggle the autonomy dial via the real POST /autonomy endpoint."""
    r = client.post("/autonomy", json={"mode": mode, "reason": reason})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == mode


def _reader_envelope(env: dict[str, str]) -> dict:
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00001-abc",
    }


def _drift_issue_proposal() -> DecisionProposal:
    return DecisionProposal(
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
        rationale="PAYMENT_MODE drifted; policy violation, not a docs update.",
        confidence=0.92,
        requires_human_review=True,
    )


def _no_op_proposal() -> DecisionProposal:
    return DecisionProposal(
        action=DecisionAction.NO_OP,
        env_diffs=[
            EnvDiff(
                name="PAYMENT_MODE",
                expected="mock",
                live="mock",
                contract_status=ContractStatus.PRESENT_ALLOW_MANUAL,
                debug_config_value=None,
                recent_pr_match=None,
            )
        ],
        target_docs_file=None,
        target_docs_section=None,
        rationale="contract matches live",
        confidence=1.0,
        requires_human_review=False,
    )


def _rollback_proposal() -> DecisionProposal:
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
        target_revision="payment-demo-00002-bbb",
        rationale="PAYMENT_MODE drifted; rollback with operator approval.",
        confidence=0.9,
        requires_human_review=True,
    )


@pytest.fixture
def _live_github(monkeypatch):
    """DRY_RUN=false + empty GCP_PROJECT → InMemory store but real (mockable)
    GitHub action path, so suppression is observable as "helper NOT called".
    """
    monkeypatch.setenv("USE_ADK", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GCP_PROJECT", "")
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    get_settings.cache_clear()
    _reset_state_for_tests()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Part 1: drift pipeline suppression (Task 6)
# --------------------------------------------------------------------------- #


def test_recheck_observe_records_suppressed_decision_without_github_call(
    _live_github, monkeypatch
):
    """Observe + a drift_issue proposal → 200; the decision is RECORDED with
    the suppression markers; the GitHub open_drift_issue helper is NOT called;
    the event is claimed (a second identical recheck returns the cached row)."""
    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    issue_calls: list = []
    monkeypatch.setattr(
        main_mod, "get_repo", lambda token, repo: object()
    )
    monkeypatch.setattr(
        main_mod, "open_drift_issue",
        lambda **k: issue_calls.append(k) or {"url": "x", "action": "drift_issue"},
    )
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": "live"})
        client = TestClient(app)
        _set_mode(client, "observe")
        r = client.post("/recheck")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["autonomy_mode"] == "observe"
        assert body["suppressed_by_autonomy"] is True
        assert body["github"] == {
            "suppressed_by_autonomy": "observe",
            "url": None,
            "action": "drift_issue",
        }
        # GitHub helper NEVER called.
        assert issue_calls == []
        # Decision persisted and served via GET /decisions with both new fields.
        decisions = client.get("/decisions").json()["decisions"]
        matched = [d for d in decisions if d.get("decision_id") == body["decision_id"]]
        assert matched, "suppressed decision must be in GET /decisions"
        assert matched[0]["suppressed_by_autonomy"] is True
        assert matched[0]["autonomy_mode"] == "observe"

        # Event claimed: a second identical recheck returns the cached decision.
        r2 = client.post("/recheck")
        assert r2.status_code == 200
        assert r2.json()["decision_id"] == body["decision_id"]
        # Still exactly one (suppressed) — the GitHub helper was never reached.
        assert issue_calls == []


def test_recheck_observe_no_op_unchanged(_live_github, monkeypatch):
    """A no_op proposal in Observe is NOT suppressed (nothing to suppress): the
    github preview keeps its no_op shape; suppressed_by_autonomy is absent; but
    autonomy_mode IS still stamped (every new decision carries it)."""
    mock_run_agent = AsyncMock(return_value=_no_op_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": "mock"})
        client = TestClient(app)
        _set_mode(client, "observe")
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "no_op"
    assert body["github"]["action"] == "no_op"
    assert "suppressed_by_autonomy" not in body["github"]
    assert body.get("suppressed_by_autonomy") in (None, False)
    assert body["autonomy_mode"] == "observe"


def test_recheck_propose_executes_actions_normally(_live_github, monkeypatch):
    """Propose → identical to today's behavior for drift_issue: the GitHub
    helper IS called; no suppression markers beyond autonomy_mode."""
    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    issue_calls: list = []
    monkeypatch.setattr(main_mod, "get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod, "open_drift_issue",
        lambda **k: issue_calls.append(k) or {
            "url": "https://github.com/x/issues/1", "action": "drift_issue",
        },
    )
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": "live"})
        client = TestClient(app)
        _set_mode(client, "propose")
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert len(issue_calls) == 1  # GitHub helper WAS called
    assert body["autonomy_mode"] == "propose"
    assert body.get("suppressed_by_autonomy") in (None, False)
    assert "suppressed_by_autonomy" not in body["github"]


def test_eventarc_observe_still_processes(monkeypatch):
    """Contrast pin with pause: an in-scope eventarc event in Observe is NOT
    dropped (no {"ignored":"paused"}). _do_recheck runs and records the
    suppressed-decision shape (vs pause's 200 {"ignored":"paused"} drop).

    Uses the conftest defaults (DRY_RUN=true, GCP_PROJECT=test-proj) so the
    eventarc auth's SA-email derivation works; the suppressed-github shape is
    the proof of suppression. The "helper NOT called" assertion is covered by
    the dedicated _live_github recheck test above.
    """
    monkeypatch.setenv("USE_ADK", "true")
    monkeypatch.setenv("EVENTARC_AUDIENCE", "https://driftscribe-agent-xyz.a.run.app/eventarc")
    get_settings.cache_clear()
    _reset_state_for_tests()
    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    audit_body = {
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "service_name": "payment-demo",
                "location": "asia-northeast1",
                "project_id": "test-proj",
            },
        },
    }
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
        patch("agent.main.verify_oauth2_token") as m_verify,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": "live"})
        m_verify.return_value = {
            "email": "eventarc-trigger-sa@test-proj.iam.gserviceaccount.com",
            "aud": "https://driftscribe-agent-xyz.a.run.app/eventarc",
        }
        client = TestClient(app)
        _set_mode(client, "observe")
        r = client.post(
            "/eventarc",
            json=audit_body,
            headers={"Authorization": "Bearer fake-token"},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    # NOT the pause drop shape.
    assert body.get("ignored") != "paused"
    assert body["suppressed_by_autonomy"] is True
    assert body["github"]["suppressed_by_autonomy"] == "observe"


def test_rollback_observe_suppresses_worker_calls(_live_github, monkeypatch):
    """A ROLLBACK proposal in Observe: NO worker calls beyond the reader live
    env read — no rollback /propose, no notifier. The decision is recorded with
    action=rollback, suppressed markers, NO 'approval' key, and the event is
    claimed (idempotent)."""
    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    worker_calls: list = []

    def _dispatch(worker, payload, *a, **k):
        worker_calls.append(worker)
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        raise AssertionError(f"no worker call expected in Observe rollback: {worker}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call", side_effect=_dispatch),
    ):
        client = TestClient(app)
        _set_mode(client, "observe")
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "rollback"
    assert body["suppressed_by_autonomy"] is True
    assert body["autonomy_mode"] == "observe"
    assert "approval" not in body
    assert "dry_run_effective" not in body
    assert body["requires_human_review"] is True
    assert "observe" in body["rendered_body"].lower()
    # Only the reader was called (for the idempotency-hash live env); never
    # rollback or notifier.
    assert "rollback" not in worker_calls
    assert "notifier" not in worker_calls

    # Event claimed — a second recheck returns the cached suppressed decision.
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call", side_effect=_dispatch),
    ):
        r2 = client.post("/recheck")
    assert r2.status_code == 200
    assert r2.json()["decision_id"] == body["decision_id"]


def test_rollback_propose_proposes_normally(_live_github, monkeypatch):
    """Propose → _do_rollback behaves exactly as today: rollback /propose +
    notifier called, an approval key is present, autonomy_mode='propose'."""
    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    workers: list = []

    def _dispatch(worker, payload, *a, **k):
        workers.append(worker)
        if worker == "reader":
            return _reader_envelope({"PAYMENT_MODE": "live"})
        if worker == "rollback":
            return {
                "approval_id": "ap-1",
                "approval_token": "tok",
                "approval_url": "https://x/approvals/ap-1?t=tok",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        if worker == "notifier":
            return {"status": "ok"}
        raise AssertionError(f"unexpected worker: {worker}")

    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call", side_effect=_dispatch),
    ):
        client = TestClient(app)
        _set_mode(client, "propose")
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "rollback"
    assert "approval" in body
    assert body["approval"]["approval_id"] == "ap-1"
    assert "rollback" in workers
    assert "notifier" in workers


def test_recheck_fail_closed_read_suppresses(_live_github, monkeypatch):
    """Direct pin of the fail-closed wiring at the pipeline seam: patch
    _autonomy_state_fail_closed to the fail-closed observe state and assert the
    drift_issue action is suppressed."""
    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    issue_calls: list = []
    monkeypatch.setattr(main_mod, "get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod, "open_drift_issue",
        lambda **k: issue_calls.append(k) or {"url": "x", "action": "drift_issue"},
    )
    monkeypatch.setattr(
        main_mod, "_autonomy_state_fail_closed",
        lambda: AutonomyState(mode="observe", read_error=True),
    )
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_env,
    ):
        m_env.return_value = _reader_envelope({"PAYMENT_MODE": "live"})
        client = TestClient(app)
        r = client.post("/recheck")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suppressed_by_autonomy"] is True
    assert issue_calls == []


# --------------------------------------------------------------------------- #
# Part 2: apply gates (Task 7) — IaC approvals + rollback approvals
# --------------------------------------------------------------------------- #
#
# Modeled on test_pause_gates.py's iac-approval + rollback-approval suites
# (same _FakeApprovalStore / CSRF / Origin / token arrangements), toggling the
# dial via _set_mode instead of _pause.


class _FakeApprovalStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def create_pending(self, *, target_revision: str, reason: str) -> Approval:
        approval_id = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        data = {
            "status": "pending",
            "target_revision": target_revision,
            "reason": reason,
            "token_hmac": "fake-hmac",
            "expires_at": now + dt.timedelta(minutes=15),
            "created_at": now,
            "created_by": "coordinator@test",
        }
        self.docs[approval_id] = data
        return Approval(approval_id=approval_id, **data)

    def get(self, approval_id: str) -> Approval | None:
        if approval_id not in self.docs:
            return None
        return Approval(approval_id=approval_id, **self.docs[approval_id])

    def claim_denied(self, approval_id: str) -> Approval | None:
        data = self.docs.get(approval_id)
        if not data or data.get("status") != "pending":
            return None
        data["status"] = "denied"
        return Approval(approval_id=approval_id, **data)


@pytest.fixture
def _rollback_store(monkeypatch):
    s = _FakeApprovalStore()
    monkeypatch.setattr(approval_helpers, "get_approval_store", lambda: s)
    return s


# ---- POST /approvals/{id} (rollback HITL) ---------------------------------- #


def test_rollback_approval_approve_refused_in_observe_and_propose(
    _rollback_store, monkeypatch
):
    """Approve below Propose+Apply → 409 with the dial detail; call_execute is
    never invoked. Both observe and propose."""
    for mode in ("observe", "propose"):
        calls: list = []
        monkeypatch.setattr(
            worker_client, "call_execute",
            lambda aid, tok: calls.append((aid, tok)) or {"status": "executed"},
        )
        approval = _rollback_store.create_pending(
            target_revision="payment-demo-00002-bbb", reason="r"
        )
        client = TestClient(app)
        _set_mode(client, mode)
        r = client.post(
            f"/approvals/{approval.approval_id}",
            data={"t": "raw-token-abc", "decision": "approve"},
        )
        assert r.status_code == 409, (mode, r.text)
        assert r.json()["detail"] == autonomy_apply_blocked_detail(mode)
        assert calls == []


def test_rollback_approval_reject_allowed_in_observe(_rollback_store, monkeypatch):
    """Reject below Propose+Apply IS allowed (safety direction) — call_deny IS
    invoked and the page re-renders 200."""
    deny_calls: list = []

    def fake_deny(aid, tok):
        deny_calls.append((aid, tok))
        _rollback_store.claim_denied(aid)
        return {"approval_id": aid, "status": "denied"}

    monkeypatch.setattr(worker_client, "call_deny", fake_deny)
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _set_mode(client, "observe")
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "reject"},
    )
    assert r.status_code == 200, r.text
    assert deny_calls == [(approval.approval_id, "raw-token-abc")]


def test_rollback_approval_open_in_propose_apply(_rollback_store, monkeypatch):
    """Propose+Apply → the gate is invisible: approve passes through to the
    worker exactly as today."""
    calls: list = []
    monkeypatch.setattr(
        worker_client, "call_execute",
        lambda aid, tok: calls.append((aid, tok)) or {"status": "executed"},
    )
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _set_mode(client, "propose_apply")
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "approve"},
    )
    assert r.status_code == 200, r.text
    assert calls == [(approval.approval_id, "raw-token-abc")]


def test_rollback_approval_ordering_pause_outranks_dial(_rollback_store, monkeypatch):
    """With BOTH paused and mode=observe, the 423 pause response wins (the dial
    gate sits after the pause gate)."""
    monkeypatch.setattr(
        worker_client, "call_execute", lambda aid, tok: {"status": "executed"}
    )
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _set_mode(client, "observe")
    client.post("/pause", json={"paused": True, "reason": "both"})
    r = client.post(
        f"/approvals/{approval.approval_id}",
        data={"t": "raw-token-abc", "decision": "approve"},
    )
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL


def test_rollback_approval_get_shows_dial_note(_rollback_store):
    """GET while in observe → calm autonomy note above the form, Approve
    disabled, Reject stays active."""
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    client = TestClient(app)
    _set_mode(client, "observe")
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="autonomy-note"' in body
    approve = re.search(r'data-testid="approve-button"[^>]*>', body)
    reject = re.search(r'data-testid="reject-button"[^>]*>', body)
    assert approve and "disabled" in approve.group(0)
    assert reject and "disabled" not in reject.group(0)


def test_rollback_approval_get_fail_closed_read(_rollback_store, monkeypatch):
    """A fail-closed dial read on the GET → Approve disabled + the note mentions
    the read failure (read_error variant, not 'the operator set it')."""
    approval = _rollback_store.create_pending(
        target_revision="payment-demo-00002-bbb", reason="r"
    )
    monkeypatch.setattr(
        main_mod, "_autonomy_state_fail_closed",
        lambda: AutonomyState(mode="observe", read_error=True),
    )
    client = TestClient(app)
    r = client.get(f"/approvals/{approval.approval_id}?t=tok")
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="autonomy-note"' in body
    assert "could not be read" in body.lower()
    approve = re.search(r'data-testid="approve-button"[^>]*>', body)
    assert approve and "disabled" in approve.group(0)


# ---- IaC approvals (POST + GET /iac-approvals/{pr}) ------------------------ #

_HEAD = "a" * 40
_PLAN_SHA = "b" * 64
_PLAN_JSON_SHA = "c" * 64
_BUCKET = "test-proj-tofu-artifacts"
_PREFIX = f"gs://{_BUCKET}/pr-42/{_HEAD}/run-7-1/"
_META_URI = _PREFIX + "metadata.json"
_GEN_META = "1700000000000003"
_ORIGIN = "https://driftscribe.adp-app.com"
_OPERATOR = "operator@example.com"
_JWT = "raw-cf-access-jwt-value"


def _iac_metadata() -> dict:
    return {
        "schema_version": "c2.v1",
        "repo": "theghostsquad00/driftscribe",
        "pr_number": 42,
        "head_sha": _HEAD,
        "base_sha": "d" * 40,
        "workflow_run_id": "7700000001",
        "workflow_run_attempt": "1",
        "artifact_uri_plan": _PREFIX + "plan.tfplan",
        "artifact_uri_json": _PREFIX + "plan.json",
        "generation_plan": "1700000000000001",
        "generation_json": "1700000000000002",
        "plan_sha256": _PLAN_SHA,
        "plan_json_sha256": _PLAN_JSON_SHA,
        "opentofu_version": "1.12.0",
        "provider_lockfile_sha256": "e" * 64,
    }


def _iac_ref() -> C2CommentRef:
    return C2CommentRef(
        head_sha=_HEAD,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        generation_plan="1700000000000001",
        generation_json="1700000000000002",
        generation_metadata=_GEN_META,
        artifact_uri_plan=_PREFIX + "plan.tfplan",
        artifact_uri_json=_PREFIX + "plan.json",
        artifact_uri_metadata=_META_URI,
        opentofu_version="1.12.0",
        comment_id=556677,
        tofu_show_text="~ image = old -> new",
    )


def _iac_view(**overrides) -> IacPlanView:
    base = dict(
        metadata=_iac_metadata(),
        tofu_show_text=_iac_ref().tofu_show_text,
        integrity_ok=True,
        denylist_violations=[],
        unverifiable=False,
        _artifact_uri_metadata=_META_URI,
        _generation_metadata=_GEN_META,
        _plan_json={
            "resource_changes": [
                {"address": "google_cloud_run_v2_service.x",
                 "type": "google_cloud_run_v2_service",
                 "change": {"actions": ["update"]}}
            ]
        },
    )
    base.update(overrides)
    return IacPlanView(**base)


@pytest.fixture
def _iac_configured(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "theghostsquad00/driftscribe")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "static-server-token")
    monkeypatch.setenv("TOFU_ARTIFACTS_BUCKET", _BUCKET)
    monkeypatch.setenv("COORDINATOR_ORIGIN", _ORIGIN)
    monkeypatch.setenv("IAC_REQUIRED_CHECKS", "tofu,static-gate")
    monkeypatch.setenv("IAC_MERGE_METHOD", "squash")
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("GCP_PROJECT", "")
    get_settings.cache_clear()
    _reset_state_for_tests()
    app.dependency_overrides[require_cf_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.pop(require_cf_operator, None)
    get_settings.cache_clear()


def _iac_patch_resolve(monkeypatch):
    ref, view = _iac_ref(), _iac_view()
    monkeypatch.setattr(main_mod, "_resolve_iac_plan", lambda s, pr: (ref, view))


def _iac_patch_get_resolve(monkeypatch):
    monkeypatch.setattr("agent.main.get_repo", lambda token, repo: object())
    monkeypatch.setattr(
        main_mod.iac_artifacts, "find_latest_c2_comment", lambda repo, pr: _iac_ref()
    )
    monkeypatch.setattr(
        main_mod.iac_artifacts,
        "load_plan_view",
        lambda r, *, bucket_name, client=None, expected_repo=None: _iac_view(),
    )


def _iac_mint():
    return iac_csrf.mint_form_token(
        get_settings(),
        pr_number=42,
        head_sha=_HEAD,
        artifact_uri_metadata=_META_URI,
        generation_metadata=_GEN_META,
        plan_sha256=_PLAN_SHA,
        plan_json_sha256=_PLAN_JSON_SHA,
        comment_id=556677,
    )


def test_iac_approval_post_refused_in_observe_and_propose(_iac_configured, monkeypatch):
    """Approve below Propose+Apply → 409 with the dial detail (in the dry-run
    precedent slot, after Origin+CSRF, before _resolve_iac_plan). No worker
    propose/apply calls."""
    _iac_patch_resolve(monkeypatch)
    for mode in ("observe", "propose"):
        propose_calls: list = []
        monkeypatch.setattr(
            main_mod.worker_client, "call_propose",
            lambda *a, **k: propose_calls.append(a) or {"approval_id": "x", "approval_token": "y"},
        )
        token = _iac_mint()
        client = TestClient(app)
        _set_mode(client, mode)
        r = client.post(
            "/iac-approvals/42",
            data={"form_token": token, "decision": "approve"},
            headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
        )
        assert r.status_code == 409, (mode, r.text)
        assert r.json()["detail"] == autonomy_apply_blocked_detail(mode)
        assert propose_calls == []


def test_iac_approval_post_ordering_pause_outranks_dial(_iac_configured, monkeypatch):
    """BOTH paused and mode=observe → 423 pause wins (dial gate sits after)."""
    _iac_patch_resolve(monkeypatch)
    token = _iac_mint()
    client = TestClient(app)
    _set_mode(client, "observe")
    client.post("/pause", json={"paused": True, "reason": "both"})
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 423
    assert r.json()["detail"] == PAUSED_DETAIL


def test_iac_approval_reject_allowed_in_observe(_iac_configured, monkeypatch):
    """Reject below Propose+Apply stays a 200 audit no-op (mutates nothing)."""
    _iac_patch_resolve(monkeypatch)
    client = TestClient(app)
    _set_mode(client, "observe")
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": _iac_mint(), "decision": "reject"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 200
    assert "reject" in r.text.lower()


def test_iac_approval_post_bad_origin_still_403_in_observe(_iac_configured, monkeypatch):
    """The dial gate sits AFTER Origin/CSRF, so a cross-site probe still gets
    403 (not a dial hint) even in observe."""
    _iac_patch_resolve(monkeypatch)
    token = _iac_mint()
    client = TestClient(app)
    _set_mode(client, "observe")
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": "https://evil.example.com", "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 403


def test_iac_approval_get_shows_dial_note(_iac_configured, monkeypatch):
    """GET in propose → 200; Approve suppressed (no form token); the calm
    approve-pending note carries the dial copy (severity pending, not error).

    The GET carries a Cf-Access-Jwt-Assertion header (presence-only at the
    GET; _iac_configured sets the CF env): this pins the OPERATOR view — a
    JWT-less GET now renders the anonymous operator-only note instead, which
    outranks the dial rung (see test_iac_approval_get.py)."""
    _iac_patch_get_resolve(monkeypatch)
    client = TestClient(app)
    _set_mode(client, "propose")
    r = client.get("/iac-approvals/42", headers={"Cf-Access-Jwt-Assertion": _JWT})
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="approve-pending"' in body
    assert "autonomy is set to" in body.lower()
    # Calm, not the red error box.
    assert 'data-testid="approve-blocked"' not in body
    assert 'name="form_token"' not in body


def test_iac_approval_get_fail_closed_read(_iac_configured, monkeypatch):
    """A fail-closed dial read on the GET → Approve suppressed and the note
    mentions the read failure (read_error variant). Operator view (JWT header
    present) — see test_iac_approval_get_shows_dial_note."""
    _iac_patch_get_resolve(monkeypatch)
    monkeypatch.setattr(
        main_mod, "_autonomy_state_fail_closed",
        lambda: AutonomyState(mode="observe", read_error=True),
    )
    client = TestClient(app)
    r = client.get("/iac-approvals/42", headers={"Cf-Access-Jwt-Assertion": _JWT})
    assert r.status_code == 200
    body = r.text
    assert 'data-testid="approve-pending"' in body
    assert "could not be read" in body.lower()
    assert 'name="form_token"' not in body


def test_iac_approval_post_apply_gate_fail_closed_read(_iac_configured, monkeypatch):
    """A fail-closed dial read on the POST approve → 409 (the fail-closed mode
    is observe, which is below Propose+Apply)."""
    _iac_patch_resolve(monkeypatch)
    propose_calls: list = []
    monkeypatch.setattr(
        main_mod.worker_client, "call_propose",
        lambda *a, **k: propose_calls.append(a) or {"approval_id": "x", "approval_token": "y"},
    )
    monkeypatch.setattr(
        main_mod, "_autonomy_state_fail_closed",
        lambda: AutonomyState(mode="observe", read_error=True),
    )
    token = _iac_mint()
    client = TestClient(app)
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 409
    assert propose_calls == []


def test_existing_decision_branches_gated(_iac_configured, monkeypatch):
    """Codex must-fix 4: the dial gate must fire BEFORE
    _handle_existing_iac_decision can route a waiting_for_rebake re-POST into a
    merge/apply. Arrange a pending waiting_for_rebake decision, set mode=propose,
    re-POST → 409; github.merge_pr_at_sha + the apply worker NOT invoked."""
    _iac_patch_resolve(monkeypatch)

    # Arrange a pending waiting_for_rebake decision under the POST's event key.
    state = get_state()
    view = _iac_view()
    event_key = main_mod._iac_event_key(
        "theghostsquad00/driftscribe", 42, view.head_sha, view.generation_metadata
    )
    state.record_event(event_key, {"trigger": "iac_apply"})
    state.record_decision(
        "dec-wfr",
        event_key,
        {
            "decision_id": "dec-wfr",
            "event_key": event_key,
            "apply_status": "waiting_for_rebake",
            "merge_state": "merged",
            "pr_number": 42,
        },
    )

    merge_calls: list = []
    apply_calls: list = []
    monkeypatch.setattr(
        main_mod.github, "merge_pr_at_sha",
        lambda *a, **k: merge_calls.append(a) or {"merged": True},
    )
    monkeypatch.setattr(
        main_mod.worker_client, "call_apply",
        lambda *a, **k: apply_calls.append(a) or {"status": "applied"},
    )
    monkeypatch.setattr(
        main_mod.worker_client, "call_propose",
        lambda *a, **k: {"approval_id": "x", "approval_token": "y"},
    )

    token = _iac_mint()
    client = TestClient(app)
    _set_mode(client, "propose")
    r = client.post(
        "/iac-approvals/42",
        data={"form_token": token, "decision": "approve"},
        headers={"Origin": _ORIGIN, "Cf-Access-Jwt-Assertion": _JWT},
    )
    assert r.status_code == 409, r.text
    assert merge_calls == []
    assert apply_calls == []
