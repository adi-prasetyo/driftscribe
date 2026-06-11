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
import json
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
