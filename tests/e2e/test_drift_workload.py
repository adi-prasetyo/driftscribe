"""E2E: drift workload — action via /recheck, reasoning via /chat.

Env-var choice: the ops contract declares PAYMENT_MODE='mock' and
FEATURE_NEW_CHECKOUT='false'. Mutating PAYMENT_MODE='live' is the canonical
drift signal (matches scripts/demo.sh beat-b). Mutating an unknown var
(e.g. FEATURE_FLAG_NEW_PAYMENT) does NOT reliably yield drift_issue — the
contract only checks declared vars.
"""
import pytest

from tests.e2e._helpers import wait_for_trace_complete


def _track_decision(body: dict, tracker: dict) -> None:
    """Append the decision_id from a /recheck response to the cleanup tracker.

    The DecisionProposal returned by /recheck carries decision_id (the Firestore
    doc ID). If a future contract change drops it, the helper silently no-ops —
    we don't want a cleanup-bookkeeping miss to fail the actual test.
    """
    decision_id = body.get("decision_id")
    if decision_id:
        tracker["decisions"].append(decision_id)


@pytest.mark.e2e
def test_baseline_recheck_returns_no_op(coordinator_client, drift_e2e_target, _firestore_cleanup_tracker):
    """payment-demo-e2e env matches contract → /recheck returns no_op."""
    assert drift_e2e_target.is_at_baseline(), (
        "test pre-condition: payment-demo-e2e must start at baseline"
    )
    resp = coordinator_client.post("/recheck", json={"workload": "drift"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _track_decision(body, _firestore_cleanup_tracker)
    assert body["action"] == "no_op", f"expected no_op, got {body['action']}"


@pytest.mark.e2e
def test_drift_recheck_returns_drift_issue(coordinator_client, drift_e2e_target, _firestore_cleanup_tracker):
    """PAYMENT_MODE=live (drift) → /recheck returns drift_issue."""
    drift_e2e_target.set_env("PAYMENT_MODE", "live")
    resp = coordinator_client.post("/recheck?force=true", json={"workload": "drift"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _track_decision(body, _firestore_cleanup_tracker)
    assert body["action"] == "drift_issue", \
        f"expected drift_issue after PAYMENT_MODE drift, got {body['action']}"


@pytest.mark.e2e
def test_chat_drift_returns_reply_and_tool_calls(coordinator_client):
    """/chat workload=drift returns the documented free-form shape."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "drift", "prompt": "Check payment-demo-e2e for drift",
              "ephemeral": True},  # read-only probe: don't litter the rail
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body.get("reply"), str) and body["reply"]
    assert isinstance(body.get("tool_calls"), list)
    assert body["tool_calls"], "expected at least one tool call from the ADK runner"
    # /chat doesn't return a decision_id (per Phase 17 contract — /chat is
    # free-form, /recheck is the structured surface). No tracker append needed.


@pytest.mark.e2e
def test_chat_trace_id_round_trips_via_events(coordinator_client, _firestore_cleanup_tracker):
    """X-Trace-Id header round-trips via /trace/{id}; response shape is {events, complete, ...}."""
    resp = coordinator_client.post(
        "/chat",
        json={"workload": "drift", "prompt": "Check payment-demo-e2e for drift"},
    )
    assert resp.status_code == 200
    trace_id = resp.headers.get("X-Trace-Id")
    assert trace_id, "X-Trace-Id header missing from /chat response"

    trace = wait_for_trace_complete(coordinator_client, trace_id)
    assert trace["complete"] is True
    # /trace returns events (UI groups them by reading event metadata).
    assert isinstance(trace.get("events"), list)
    assert trace["events"], "expected at least one redacted event in the timeline"
    # The trace may carry an associated decision document if the ADK reasoning
    # loop happened to record one (drift /chat itself does not always persist
    # a DecisionProposal — /recheck is the structured surface). Track if present.
    decision = trace.get("decision") or {}
    if decision.get("decision_id"):
        _firestore_cleanup_tracker["decisions"].append(decision["decision_id"])
