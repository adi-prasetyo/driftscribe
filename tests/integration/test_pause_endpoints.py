"""Integration tests for GET /pause and POST /pause endpoints (Task 1).

Follows the conftest autouse ``_agent_settings`` pattern (auth bypassed by
default; ``no_auth_override`` marker for the real-auth cases). TestClient usage
mirrors ``test_capabilities_endpoint.py`` and ``test_token_guard.py``.

Coverage required by the spec:
- GET default: paused=false, read_error=false.
- POST pause → GET paused round-trip: reason, actor="operator-token", non-null ISO updated_at.
- POST resume.
- POST with ONLY a CF Access JWT records the canonical email as actor.
- 422 on unknown body field (extra="forbid").
- 422 on non-bool paused value.
- 422 on reason >500 chars.
- Cache-Control: no-store on both GET and POST.
- 401 without token (no_auth_override).
- GET returns fail-closed view when store's get_pause raises (monkeypatch).
- POST returns 502 when store's set_pause raises (monkeypatch).
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


def test_get_pause_default_returns_running():
    """Fresh store → paused=false, read_error=false, no-store header."""
    client = TestClient(app)
    r = client.get("/pause")
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is False
    assert body["read_error"] is False
    assert r.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# POST pause → round-trip via GET
# --------------------------------------------------------------------------- #


def test_post_pause_and_get_round_trip():
    """Toggle pause via POST, verify GET reflects it with all fields populated."""
    client = TestClient(app)

    r = client.post("/pause", json={"paused": True, "reason": "incident drill"})
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is True
    assert body["reason"] == "incident drill"
    # Actor attribution falls back to "operator-token" when no CF Access JWT.
    assert body["actor"] == "operator-token"
    # updated_at must be a non-null ISO string.
    assert body["updated_at"] is not None
    assert isinstance(body["updated_at"], str)
    assert body["read_error"] is False

    # GET must reflect the persisted state.
    r2 = client.get("/pause")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["paused"] is True
    assert b2["reason"] == "incident drill"
    assert b2["actor"] == "operator-token"
    assert b2["updated_at"] is not None
    assert b2["read_error"] is False


def test_post_resume_clears_paused_flag():
    """POST {paused:false} after a pause toggles the system back to running."""
    client = TestClient(app)
    client.post("/pause", json={"paused": True, "reason": "test pause"})
    r = client.post("/pause", json={"paused": False})
    assert r.status_code == 200
    assert r.json()["paused"] is False

    r2 = client.get("/pause")
    assert r2.json()["paused"] is False


def test_post_pause_without_reason_stores_none():
    """reason is optional; omitting it stores None (not the empty string)."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True})
    assert r.status_code == 200
    assert r.json()["reason"] is None


def test_post_pause_strips_whitespace_only_reason_to_none():
    """A reason string of only whitespace must be stored as None."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True, "reason": "   "})
    assert r.status_code == 200
    assert r.json()["reason"] is None


def test_post_pause_cache_control_no_store():
    """POST responses carry no-store too — the body IS pause status, and a
    cached copy could mislead the operator about the current safety state."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True})
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"


# --------------------------------------------------------------------------- #
# CF Access JWT actor attribution
# --------------------------------------------------------------------------- #


def _configure_cf_access(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "adp-app.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", "aud-tag-123")
    get_settings.cache_clear()


def test_post_pause_with_cf_jwt_records_canonical_email(monkeypatch):
    """Valid CF Access JWT → actor is the canonical email, not 'operator-token'."""
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
            "/pause",
            json={"paused": True, "reason": "cf jwt test"},
            headers={"Cf-Access-Jwt-Assertion": "valid.jwt.token"},
        )

    assert r.status_code == 200
    assert r.json()["actor"] == "ops@example.com"


def test_post_pause_invalid_cf_jwt_falls_back_to_operator_token(monkeypatch):
    """Invalid CF JWT → silent fallback to 'operator-token' (no 4xx)."""
    _configure_cf_access(monkeypatch)

    with patch(
        "agent.main.verify_cf_access_jwt",
        side_effect=CfAccessJwtError("expired"),
    ):
        client = TestClient(app)
        r = client.post(
            "/pause",
            json={"paused": True},
            headers={"Cf-Access-Jwt-Assertion": "stale.jwt"},
        )

    assert r.status_code == 200
    assert r.json()["actor"] == "operator-token"


def test_post_pause_cf_not_configured_ignores_jwt(monkeypatch):
    """When CF Access is not configured, the JWT header must be ignored (no error)."""
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD_TAG", raising=False)
    get_settings.cache_clear()

    with patch("agent.main.verify_cf_access_jwt") as v:
        client = TestClient(app)
        r = client.post(
            "/pause",
            json={"paused": True},
            headers={"Cf-Access-Jwt-Assertion": "irrelevant.jwt"},
        )

    assert r.status_code == 200
    assert r.json()["actor"] == "operator-token"
    # verify_cf_access_jwt must NOT be called when CF Access is unconfigured.
    v.assert_not_called()


# --------------------------------------------------------------------------- #
# Request validation — 422 cases
# --------------------------------------------------------------------------- #


def test_post_pause_unknown_field_returns_422():
    """extra='forbid' — unknown fields must surface as 422, not be silently dropped."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True, "unknown_field": "oops"})
    assert r.status_code == 422


def test_post_pause_non_bool_paused_returns_422():
    """paused must be a bool; a string should yield 422."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": "yes"})
    assert r.status_code == 422


def test_post_pause_reason_too_long_returns_422():
    """reason is capped at 500 chars; exceeding it yields 422."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True, "reason": "x" * 501})
    assert r.status_code == 422


def test_post_pause_reason_at_limit_is_ok():
    """Exactly 500 chars is within the limit and must succeed."""
    client = TestClient(app)
    r = client.post("/pause", json={"paused": True, "reason": "x" * 500})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Auth guard (real verify_token via no_auth_override)
# --------------------------------------------------------------------------- #


@pytest.mark.no_auth_override
class TestPauseTokenGuard:
    def _set_token(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
        get_settings.cache_clear()

    def test_get_pause_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-pause-test-123")
        client = TestClient(app)
        assert client.get("/pause").status_code == 401

    def test_post_pause_without_token_returns_401(self, monkeypatch):
        self._set_token(monkeypatch, "tok-pause-test-123")
        client = TestClient(app)
        r = client.post("/pause", json={"paused": True})
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Fail-closed: GET returns fail-closed view when store raises
# --------------------------------------------------------------------------- #


def test_get_pause_returns_fail_closed_when_store_raises():
    """Storage exception on get_pause → 200 with paused=True, read_error=True.

    A flag-read failure IS the fail-closed system state (the mutation gates
    refuse while the flag is unreadable), so this is not an error response.
    """
    state = get_state()
    with patch.object(state, "get_pause", side_effect=RuntimeError("Firestore down")):
        client = TestClient(app)
        r = client.get("/pause")

    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is True
    assert body["read_error"] is True


# --------------------------------------------------------------------------- #
# 502 when set_pause raises: the operator must know the toggle didn't take
# --------------------------------------------------------------------------- #


def test_post_pause_returns_502_when_set_pause_raises():
    """Storage exception on set_pause → 502. The toggle did NOT take effect."""
    state = get_state()
    with patch.object(state, "set_pause", side_effect=RuntimeError("Firestore down")):
        client = TestClient(app)
        r = client.post("/pause", json={"paused": True})

    assert r.status_code == 502
    # Detail must acknowledge the failure so the operator doesn't assume success.
    assert r.json()["detail"]
