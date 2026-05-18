import os
os.environ["DRY_RUN"] = "true"
os.environ["GCP_PROJECT"] = "test-proj"
os.environ["CONTRACT_PATH"] = "demo/ops-contract.yaml"

from fastapi.testclient import TestClient
from unittest.mock import patch
from agent.main import app


def test_recheck_renders_drift_issue_when_live_violates_contract():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "drift_issue"
    assert "PAYMENT_MODE" in body["rendered_body"]
    assert body["dry_run"] is True


def test_recheck_no_op_when_live_matches_contract():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.json()["action"] == "no_op"


def test_recheck_escalation_for_unknown_var():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false", "NEW_THING": "x"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.json()["action"] == "escalation"


def test_recheck_returns_502_on_cloud_run_read_failure():
    with patch("agent.main.read_live_env") as m:
        m.side_effect = RuntimeError("permission denied")
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 502
    assert "cloud run read failed" in r.json()["detail"]


def test_healthz_returns_ok():
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
