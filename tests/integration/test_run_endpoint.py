"""Integration tests for ``GET /runs/{decision_id}``.

This endpoint is intentionally UNAUTHENTICATED (a read-only state lookup — see
``test_token_guard.py``), which makes the raw-rationale scrub (PR 2) especially
important here: a secret quoted in an LLM rationale must not be readable by
anyone who can guess/observe a decision_id.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.main import app, get_state


def test_runs_endpoint_scrubs_secret_in_rationale():
    state = get_state()
    secret = "sk-RUN-5555"
    state.record_decision(
        "dec-run",
        "ev-run",
        {
            "decision_id": "dec-run",
            "action": "drift_issue",
            "trace_id": "d" * 32,
            "rationale": f"TOKEN set to {secret}.",
            "diffs": [
                {"name": "TOKEN", "live": secret,
                 "contract_status": "present_disallow_manual"}
            ],
        },
    )
    resp = TestClient(app).get("/runs/dec-run")
    assert resp.status_code == 200
    body = resp.json()
    # Rationale prose is scrubbed (PR 2)...
    assert secret not in body["rationale"]
    assert "TOKEN" in body["rationale"]
    # ...diffs[] stay raw by design (frontend redacts at display — PR 1).
    assert body["diffs"][0]["live"] == secret


def test_runs_endpoint_404_unchanged():
    assert TestClient(app).get("/runs/does-not-exist").status_code == 404
