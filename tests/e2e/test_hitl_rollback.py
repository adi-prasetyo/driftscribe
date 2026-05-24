"""E2E: HITL rollback — explicit revision, form-POST, 403 on replay."""
import httpx
import pytest

from tests.e2e._helpers import (
    find_approval_url_in_trace_events,
    wait_for,
    wait_for_trace_complete,
)


@pytest.mark.e2e
def test_rollback_mints_approval_url_and_is_single_use(
    coordinator_client, drift_e2e_target, e2e_base_url, _firestore_cleanup_tracker
):
    """Force rollback via /chat naming an explicit baseline revision."""
    baseline_revision = drift_e2e_target.baseline_revision()
    assert baseline_revision, "baseline revision must be captured pre-test"

    # Drift the payment-demo-e2e env so a rollback is meaningful.
    drift_e2e_target.set_env("PAYMENT_MODE", "live")

    resp = coordinator_client.post(
        "/chat",
        json={
            "workload": "drift",
            "prompt": (
                f"payment mode drifted. roll us back to revision "
                f"{baseline_revision}."
            ),
        },
        timeout=180.0,
    )
    assert resp.status_code == 200, resp.text
    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id, "X-Trace-Id required to locate approval URL in events"

    trace = wait_for_trace_complete(coordinator_client, trace_id, timeout=180.0)
    found = find_approval_url_in_trace_events(trace["events"])
    assert found is not None, (
        f"no approval URL found in /trace events; "
        f"reply preview={resp.json().get('reply','')[:300]!r}"
    )
    full_url, approval_id, token = found
    # Track the approval doc for session-scoped Firestore cleanup.
    _firestore_cleanup_tracker["approvals"].append(approval_id)
    # The /chat that minted the rollback also writes a decision; track it too.
    decision = trace.get("decision") or {}
    if decision.get("decision_id"):
        _firestore_cleanup_tracker["decisions"].append(decision["decision_id"])

    # Approve via form-POST — NO X-DriftScribe-Token (the token IS the auth).
    plain = httpx.Client(base_url=e2e_base_url, timeout=30.0)
    try:
        approve_1 = plain.post(
            f"/approvals/{approval_id}",
            data={"t": token, "decision": "approve"},
        )
        assert approve_1.status_code == 200, \
            f"first approve should succeed, got {approve_1.status_code}: {approve_1.text[:300]}"

        wait_for(
            lambda: drift_e2e_target.is_at_baseline(),
            timeout=180.0,
            description="rollback to restore baseline env",
        )

        # Replay: 403 (NOT 410).
        approve_2 = plain.post(
            f"/approvals/{approval_id}",
            data={"t": token, "decision": "approve"},
        )
        assert approve_2.status_code == 403, \
            f"single-use violated: replay returned {approve_2.status_code}, want 403"
    finally:
        plain.close()


@pytest.mark.e2e
def test_tampered_token_returns_403(e2e_base_url):
    """Tampered token + nonexistent approval id → 403 (collapsed catch-all)."""
    plain = httpx.Client(base_url=e2e_base_url, timeout=30.0)
    try:
        resp = plain.post(
            "/approvals/nonexistent_id",
            data={"t": "totally-fake-token", "decision": "approve"},
        )
        assert resp.status_code == 403
    finally:
        plain.close()
