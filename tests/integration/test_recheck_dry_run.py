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
    body = r.json()
    assert body["action"] == "no_op"
    assert body["github"]["action"] == "no_op"


def test_recheck_escalation_for_unknown_var():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "false", "NEW_THING": "x"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.json()["action"] == "escalation"


def test_recheck_dry_run_returns_github_preview_for_docs_pr():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "true"}
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["action"] == "docs_pr"
    assert body["github"]["dry_run"] is True
    # Preview must reflect the patched runbook content (FEATURE_NEW_CHECKOUT=true)
    assert "FEATURE_NEW_CHECKOUT=true" in body["github"]["preview"]
    # Branch includes slug + timestamp + random suffix; assert the slug prefix.
    assert body["github"]["branch"].startswith("driftscribe/feature_new_checkout-")


def test_recheck_returns_500_when_docs_root_missing_runbook(monkeypatch):
    # Deploy misconfig: contract points at demo/docs/runbook.md but DOCS_ROOT
    # is set to a directory that doesn't contain it. Must refuse (500), NOT
    # silently overwrite with a stub.
    monkeypatch.setenv("DOCS_ROOT", "/tmp/does-not-exist-driftscribe")
    from agent.config import get_settings
    get_settings.cache_clear()
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "mock", "FEATURE_NEW_CHECKOUT": "true"}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 500
    assert "runbook not found" in r.json()["detail"]


def test_branch_slug_sanitizes_unsafe_chars():
    # Defensive: branch slug must reject git-refspec-forbidden chars
    from agent.main import _branch_slug
    assert _branch_slug("PAYMENT_MODE") == "payment_mode"
    assert _branch_slug("Has/Slash") == "has-slash"
    assert _branch_slug("..bad..") == "bad"
    assert _branch_slug("@{weird}") == "weird"
    assert _branch_slug("---") == "var"


def test_recheck_dry_run_returns_github_result_for_drift_issue():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["action"] == "drift_issue"
    assert body["github"]["dry_run"] is True
    assert body["github"]["url"] is None
    assert "Drift:" in body["github"]["title"]


def test_recheck_dry_run_returns_github_result_for_escalation():
    with patch("agent.main.read_live_env") as m:
        m.return_value = {
            "PAYMENT_MODE": "mock",
            "FEATURE_NEW_CHECKOUT": "false",
            "NEW_THING": "x",
        }
        client = TestClient(app)
        r = client.post("/recheck")
    body = r.json()
    assert body["action"] == "escalation"
    assert body["github"]["dry_run"] is True
    assert "Review:" in body["github"]["title"]


def test_recheck_returns_502_on_cloud_run_read_failure():
    with patch("agent.main.read_live_env") as m:
        m.side_effect = RuntimeError("permission denied")
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 502
    assert "cloud run read failed" in r.json()["detail"]


def test_recheck_returns_500_on_contract_load_failure(monkeypatch):
    # Point at a non-existent contract — should surface as 500, not 502
    monkeypatch.setenv("CONTRACT_PATH", "demo/does-not-exist.yaml")
    from agent.config import get_settings
    get_settings.cache_clear()
    with patch("agent.main.read_live_env") as m:
        m.return_value = {}
        client = TestClient(app)
        r = client.post("/recheck")
    assert r.status_code == 500
    assert "contract load failed" in r.json()["detail"]


def test_healthz_returns_ok():
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_recheck_with_changed_live_env_returns_fresh_decision():
    """Demo Beat B (PAYMENT_MODE=live) and Beat C (NEW_THING=x) must not collide."""
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
    with patch("agent.main.read_live_env") as m:
        m.return_value = {
            "PAYMENT_MODE": "mock",
            "FEATURE_NEW_CHECKOUT": "false",
            "NEW_THING": "x",
        }
        r2 = client.post("/recheck").json()
    assert r1["action"] == "drift_issue"
    assert r2["action"] == "escalation"
    assert r1["decision_id"] != r2["decision_id"]
    assert r1["event_key"] != r2["event_key"]


def test_recheck_same_live_env_returns_cached_decision():
    """Same live state on second call -> cache hit returns the SAME decision_id."""
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
        r2 = client.post("/recheck").json()
    assert r1["decision_id"] == r2["decision_id"]
    assert r1["event_key"] == r2["event_key"]


def test_runs_endpoint_returns_recorded_decision():
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
    r2 = client.get(f"/runs/{r1['decision_id']}")
    assert r2.status_code == 200
    assert r2.json()["action"] == "drift_issue"
    assert r2.json()["decision_id"] == r1["decision_id"]


def test_runs_endpoint_returns_404_for_unknown_decision():
    client = TestClient(app)
    r = client.get("/runs/no-such-id")
    assert r.status_code == 404


def test_force_param_bypasses_idempotency_cache():
    client = TestClient(app)
    with patch("agent.main.read_live_env") as m:
        m.return_value = {"PAYMENT_MODE": "live", "FEATURE_NEW_CHECKOUT": "false"}
        r1 = client.post("/recheck").json()
        r2 = client.post("/recheck?force=true").json()
    # Same live state -> cache hit on r1; force=true -> fresh decision_id + event_key
    assert r1["decision_id"] != r2["decision_id"]
    assert r1["event_key"] != r2["event_key"]
    assert r1["action"] == "drift_issue" and r2["action"] == "drift_issue"
