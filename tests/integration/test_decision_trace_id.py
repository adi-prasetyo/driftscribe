"""Integration tests for Task 19.A.4 — ``trace_id`` on every decision.

The past-decisions left rail in the Phase 19 UI (19.B.6) needs to deep-link
from each persisted decision back to its trace document at
``/trace/{trace_id}``. That requires every decision the coordinator writes
to Firestore to carry the request's ``trace_id`` as a top-level field.

These tests pin two properties:

1. **Round-trip from header to response body.** An inbound ``X-Trace-Id``
   header on ``/recheck`` arrives in the response JSON's ``trace_id`` key
   verbatim. Covers the non-rollback decision-recording path
   (``_do_recheck`` → ``record_decision``).

2. **Same property for the rollback path.** ``_do_rollback`` writes a
   distinct response shape (``approval`` block in place of ``github``), so
   it has its own response-dict literal — a future refactor could land
   ``trace_id`` on one path but miss the other. Pin both.

The cached-from-Firestore property is also asserted: ``find_decision_for_event``
returns the persisted dict, so reading it back must surface the same
``trace_id`` the response carried. This guards against a refactor that
adds ``trace_id`` to the HTTP response only (and forgets the persisted doc).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import _reset_state_for_tests, app, get_state
from agent.models import (
    ContractStatus,
    DecisionAction,
    DecisionProposal,
    EnvDiff,
)


# --------------------------------------------------------------------------- #
# Shared fixtures — canonical proposals + reader envelope
# --------------------------------------------------------------------------- #


def _drift_issue_proposal() -> DecisionProposal:
    """A canonical drift_issue ``DecisionProposal`` the validator accepts.

    Mirrors the helper in ``test_recheck_use_adk_path.py``; kept local so
    a refactor of one file doesn't silently move the other.
    """
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
        rationale=(
            "PAYMENT_MODE drifted from 'mock' to 'live'; contract marks the "
            "var allow_manual_change=false."
        ),
        confidence=0.9,
        requires_human_review=True,
    )


def _rollback_proposal() -> DecisionProposal:
    """A canonical rollback ``DecisionProposal`` the validator accepts."""
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
        target_revision="payment-demo-00041-xyz",
        rationale=(
            "PAYMENT_MODE drifted; rolling back to last contract-compliant "
            "revision (operator approval required)."
        ),
        confidence=0.9,
        requires_human_review=True,
    )


def _reader_envelope(env: dict[str, str]) -> dict[str, Any]:
    """Reader Worker /read response shape (mirrors other integration tests)."""
    return {
        "service": "payment-demo",
        "region": "asia-northeast1",
        "project": "test-project",
        "env": env,
        "revision": "payment-demo-00042-cur",
    }


_APPROVAL_ID = "abc-uuid-1234-5678-9012-345678901234"
_APPROVAL_TOKEN = "tok-xyz-43chars-aaaaaaaaaaaaaaaaaaaaaaaaa"
_APPROVAL_URL = (
    f"https://coordinator.example/approvals/{_APPROVAL_ID}?t={_APPROVAL_TOKEN}"
)
_EXPIRES_AT_ISO = "2099-01-01T00:00:00+00:00"


def _propose_envelope() -> dict[str, Any]:
    return {
        "approval_id": _APPROVAL_ID,
        "approval_token": _APPROVAL_TOKEN,
        "approval_url": _APPROVAL_URL,
        "expires_at": _EXPIRES_AT_ISO,
    }


def _notifier_envelope() -> dict[str, Any]:
    return {
        "status": "ok",
        "channel": "approval",
        "severity": "high",
        "downstream_status": 200,
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_recheck_decision_carries_inbound_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inbound ``X-Trace-Id`` on ``/recheck`` flows through to the
    decision response's ``trace_id`` field (and therefore into the
    Firestore decision document, since ``record_decision`` persists the
    same dict that's returned).

    Pins the non-rollback ``_do_recheck`` path.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    fixed_trace = "a" * 32
    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.return_value = _reader_envelope(
            {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        )
        client = TestClient(app)
        resp = client.post("/recheck", headers={"X-Trace-Id": fixed_trace})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The header round-trips into the response body verbatim.
    assert body.get("trace_id") == fixed_trace
    # And the coordinator's middleware echoes the same value on the
    # response header (sanity — pinned by test_trace_propagation.py).
    assert resp.headers["X-Trace-Id"] == fixed_trace
    # The persisted decision (read back via the in-memory state store)
    # carries the same trace_id — the past-decisions UI reads this dict.
    state = get_state()
    cached = state.find_decision_for_event(body["event_key"])
    assert cached is not None
    assert cached["trace_id"] == fixed_trace


def test_recheck_decision_carries_minted_trace_id_when_header_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no ``X-Trace-Id`` header is sent, the decision still carries
    a freshly-minted hex32 trace_id (never empty). The minted value also
    matches the value the middleware echoes back on the response header.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    mock_run_agent = AsyncMock(return_value=_drift_issue_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.return_value = _reader_envelope({"PAYMENT_MODE": "live"})
        client = TestClient(app)
        resp = client.post("/recheck")  # no X-Trace-Id

    assert resp.status_code == 200, resp.text
    body = resp.json()
    minted = body.get("trace_id")
    assert isinstance(minted, str) and len(minted) == 32
    assert all(c in "0123456789abcdef" for c in minted)
    # Same value as the middleware-echoed header — both come from the
    # ContextVar bound for this request.
    assert resp.headers["X-Trace-Id"] == minted


def test_rollback_decision_carries_inbound_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inbound ``X-Trace-Id`` on ``/recheck`` that produces a rollback
    proposal lands in the rollback decision document. ``_do_rollback``
    builds its own response-dict literal (distinct schema with the
    ``approval`` block in place of ``github``), so the trace_id binding
    has to be done independently of the non-rollback path — pin it.
    """
    monkeypatch.setenv("USE_ADK", "true")
    get_settings.cache_clear()
    _reset_state_for_tests()

    fixed_trace = "b" * 32

    def dispatch(worker: str, payload: dict, *args: Any, **kwargs: Any) -> Any:
        if worker == "reader":
            return _reader_envelope(
                {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
            )
        if worker == "rollback":
            return _propose_envelope()
        if worker == "notifier":
            return _notifier_envelope()
        raise AssertionError(f"unexpected worker: {worker!r}")

    mock_run_agent = AsyncMock(return_value=_rollback_proposal())
    with (
        patch("agent.main._run_adk_agent", mock_run_agent),
        patch("agent.main.worker_client.call") as m_call,
    ):
        m_call.side_effect = dispatch
        client = TestClient(app)
        resp = client.post("/recheck", headers={"X-Trace-Id": fixed_trace})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "rollback"
    # Round-trip on the HTTP response.
    assert body.get("trace_id") == fixed_trace
    # And the persisted decision document (the past-decisions UI's source
    # of truth) carries the same trace_id.
    state = get_state()
    cached = state.find_decision_for_event(body["event_key"])
    assert cached is not None
    assert cached["trace_id"] == fixed_trace
    # Sanity: the approval block is intact (we didn't break the rollback
    # response schema while adding the trace_id field).
    assert cached["approval"]["approval_url"] == _APPROVAL_URL
