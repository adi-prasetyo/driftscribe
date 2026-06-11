"""Integration tests for GET /autonomy and POST /autonomy endpoints (Task 4).

Models each test on its test_pause_endpoints.py counterpart: conftest autouse
``_agent_settings`` bypasses auth by default; ``no_auth_override`` marker for
the real-auth cases. TestClient usage mirrors test_pause_endpoints.py.

Coverage:
- GET default: mode=propose_apply, read_error=false, no-store header.
- POST mode → GET round-trip: reason, actor="operator-token", ISO updated_at.
- POST with ONLY a CF Access JWT records the canonical email as actor.
- 422 on unknown mode (Literal-constrained request model).
- 422 on unknown body field (extra="forbid").
- 422 on reason >500 chars.
- Cache-Control: no-store on both GET and POST.
- 401 without token (no_auth_override).
- GET returns fail-closed view (observe/read_error) when store raises.
- POST returns 502 when set_autonomy raises.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app, get_state
from driftscribe_lib.cf_access import CfAccessJwtError


# --------------------------------------------------------------------------- #
# Happy path — GET default state
# --------------------------------------------------------------------------- #


def test_get_autonomy_defaults_to_propose_apply():
    """Fresh store → mode=propose_apply (the pre-dial default), read_error=false."""
    client = TestClient(app)
    r = client.get("/autonomy")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "propose_apply"
    assert body["reason"] is None
    assert body["actor"] is None
    assert body["updated_at"] is None
    assert body["read_error"] is False
    assert r.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# POST mode → round-trip via GET
# --------------------------------------------------------------------------- #


def test_post_then_get_round_trip():
    client = TestClient(app)

    r = client.post("/autonomy", json={"mode": "observe", "reason": "new adopter"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "observe"
    assert body["reason"] == "new adopter"
    assert body["actor"] == "operator-token"
    assert body["updated_at"] is not None
    assert isinstance(body["updated_at"], str)
    assert body["read_error"] is False

    r2 = client.get("/autonomy")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["mode"] == "observe"
    assert b2["reason"] == "new adopter"
    assert b2["actor"] == "operator-token"
    assert b2["updated_at"] is not None
    assert b2["read_error"] is False


def test_post_each_mode_round_trips():
    client = TestClient(app)
    for mode in ("observe", "propose", "propose_apply"):
        r = client.post("/autonomy", json={"mode": mode})
        assert r.status_code == 200
        assert r.json()["mode"] == mode
        assert client.get("/autonomy").json()["mode"] == mode


def test_post_without_reason_stores_none():
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "propose"})
    assert r.status_code == 200
    assert r.json()["reason"] is None


def test_post_strips_whitespace_only_reason_to_none():
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "propose", "reason": "   "})
    assert r.status_code == 200
    assert r.json()["reason"] is None


def test_post_cache_control_no_store():
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "observe"})
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# CF Access JWT actor attribution (shared helper with pause endpoints)
# --------------------------------------------------------------------------- #


def _configure_cf_access(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "adp-app.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag-123")
    get_settings.cache_clear()


def test_post_with_cf_jwt_records_canonical_email(monkeypatch):
    _configure_cf_access(monkeypatch)

    with patch(
        "agent.main.verify_cf_access_jwt",
        return_value={"email": "ops@example.com"},
    ), patch(
        "agent.main.canonical_operator_email",
        return_value="ops@example.com",
    ):
        client = TestClient(app)
        r = client.post(
            "/autonomy",
            json={"mode": "observe", "reason": "cf jwt test"},
            headers={"Cf-Access-Jwt-Assertion": "valid.jwt.token"},
        )

    assert r.status_code == 200
    assert r.json()["actor"] == "ops@example.com"


def test_post_invalid_cf_jwt_falls_back_to_operator_token(monkeypatch):
    _configure_cf_access(monkeypatch)

    with patch(
        "agent.main.verify_cf_access_jwt",
        side_effect=CfAccessJwtError("expired"),
    ):
        client = TestClient(app)
        r = client.post(
            "/autonomy",
            json={"mode": "observe"},
            headers={"Cf-Access-Jwt-Assertion": "stale.jwt"},
        )

    assert r.status_code == 200
    assert r.json()["actor"] == "operator-token"


# --------------------------------------------------------------------------- #
# Request validation — 422 cases
# --------------------------------------------------------------------------- #


def test_post_rejects_unknown_mode():
    """mode is Literal-constrained — an unknown mode is a 422 at the edge."""
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "yolo"})
    assert r.status_code == 422


def test_post_rejects_extra_fields():
    """extra='forbid' — unknown fields (e.g. a stray 'paused') surface as 422."""
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "observe", "paused": True})
    assert r.status_code == 422


def test_post_rejects_reason_too_long():
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "observe", "reason": "x" * 501})
    assert r.status_code == 422


def test_post_reason_at_limit_is_ok():
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "observe", "reason": "x" * 500})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Auth guard (real verify_token via no_auth_override)
# --------------------------------------------------------------------------- #


@pytest.mark.no_auth_override
class TestAutonomyTokenGuard:
    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_get_autonomy_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-autonomy-test-123")
        client = TestClient(app)
        assert client.get("/autonomy").status_code == 401

    def test_post_autonomy_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-autonomy-test-123")
        client = TestClient(app)
        r = client.post("/autonomy", json={"mode": "observe"})
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Fail-closed: GET returns observe/read_error when store raises
# --------------------------------------------------------------------------- #


def test_get_fail_closed_on_store_error():
    """Storage exception on get_autonomy → 200 mode=observe, read_error=True.

    The fail-closed state IS the system's effective state.
    """
    state = get_state()
    with patch.object(state, "get_autonomy", side_effect=RuntimeError("Firestore down")):
        client = TestClient(app)
        r = client.get("/autonomy")

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "observe"
    assert body["read_error"] is True


def test_get_fail_closed_when_get_state_itself_raises(monkeypatch):
    def _boom():
        raise RuntimeError("firestore client construction failed")

    monkeypatch.setattr("agent.main.get_state", _boom)
    client = TestClient(app)
    r = client.get("/autonomy")

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "observe"
    assert body["read_error"] is True


# --------------------------------------------------------------------------- #
# 502 when set_autonomy raises: the operator must know the toggle didn't take
# --------------------------------------------------------------------------- #


def test_post_storage_failure_is_502():
    state = get_state()
    with patch.object(state, "set_autonomy", side_effect=RuntimeError("Firestore down")):
        client = TestClient(app)
        r = client.post("/autonomy", json={"mode": "observe"})

    assert r.status_code == 502
    assert "did NOT take effect" in r.json()["detail"]


def test_post_502_when_get_state_itself_raises(monkeypatch):
    def _boom():
        raise RuntimeError("firestore client construction failed")

    monkeypatch.setattr("agent.main.get_state", _boom)
    client = TestClient(app)
    r = client.post("/autonomy", json={"mode": "observe"})

    assert r.status_code == 502
    assert "did NOT take effect" in r.json()["detail"]
