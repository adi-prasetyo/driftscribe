"""Token guard tests for operator-facing endpoints (Phase 11.1).

The X-DriftScribe-Token header guards human-driven endpoints (currently
``/recheck``; ``/chat`` lands in Phase 11.7). Distinct from the
coordinator-to-worker auth layer, which uses audience-bound Google ID tokens
(proved in spike 11.0, wired in Phase 11.3+).

Status-code contract:
- 503 if the server didn't load DRIFTSCRIBE_TOKEN (fail closed; a deploy
  that forgot --set-secrets shouldn't silently expose /recheck).
- 401 if the header is absent.
- 403 if the header is present but doesn't match.
- Constant-time compare via ``secrets.compare_digest`` so the response time
  doesn't leak how many leading bytes matched.

Note: ``pytestmark = pytest.mark.no_auth_override`` keeps the real
``verify_token`` dependency wired for this module — the conftest's autouse
fixture stubs it out for every OTHER integration test (so the existing
/recheck tests don't need to know about the token guard).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent.config import get_settings
from agent.main import app

pytestmark = pytest.mark.no_auth_override


def _set_token(monkeypatch, value: str) -> None:
    """Set DRIFTSCRIBE_TOKEN and bust the Settings cache.

    The autouse conftest fixture sets a handful of env vars but doesn't touch
    DRIFTSCRIBE_TOKEN — each test that needs a configured token sets it here.
    The conftest still owns the teardown via monkeypatch.
    """
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", value)
    get_settings.cache_clear()


def test_recheck_without_token_returns_401(monkeypatch):
    _set_token(monkeypatch, "test-token-value-123")
    client = TestClient(app)
    r = client.post("/recheck")
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "X-DriftScribe-Token" in detail
    # Don't leak whether a token was provided or not — message just says missing.
    assert "missing" in detail.lower()


def test_recheck_wrong_token_returns_403(monkeypatch):
    _set_token(monkeypatch, "test-token-value-123")
    client = TestClient(app)
    r = client.post("/recheck", headers={"X-DriftScribe-Token": "wrong-value"})
    assert r.status_code == 403
    detail = r.json()["detail"]
    # Must NOT echo the bad token back — that's an info leak (CWE-209 lite).
    assert "wrong-value" not in detail
    assert "invalid" in detail.lower()


def test_recheck_correct_token_succeeds(monkeypatch):
    _set_token(monkeypatch, "test-token-value-123")
    # Stub the downstream so we're really only asserting the guard passed.
    # If it returns this sentinel, the request reached the handler.
    sentinel = {"_token_guard_passed": True, "action": "no_op"}

    async def _stub_do_recheck(trigger, force=False):
        return sentinel

    with patch("agent.main._do_recheck", side_effect=_stub_do_recheck):
        client = TestClient(app)
        r = client.post(
            "/recheck",
            headers={"X-DriftScribe-Token": "test-token-value-123"},
        )
    assert r.status_code == 200
    assert r.json() == sentinel


def test_missing_token_config_returns_503(monkeypatch):
    """Fail-closed: empty DRIFTSCRIBE_TOKEN means a deploy forgot --set-secrets.

    The guard must refuse all requests in this state rather than silently
    accept everything. Tested at the 'with a header' branch — but the guard
    should short-circuit on missing config before checking the header, so a
    request without a header should also 503 here.
    """
    _set_token(monkeypatch, "")
    client = TestClient(app)
    # With a header → still 503 because server-side config is missing.
    r = client.post("/recheck", headers={"X-DriftScribe-Token": "anything"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()
    # Without a header → also 503 (the config check fires first).
    r2 = client.post("/recheck")
    assert r2.status_code == 503


def test_eventarc_does_not_require_token(monkeypatch):
    """``/eventarc`` is exempt — it'll be auth'd via Google-signed ID tokens
    from Eventarc in Phase 14. Currently returns 501.
    """
    _set_token(monkeypatch, "test-token-value-123")
    client = TestClient(app)
    r = client.post("/eventarc")
    assert r.status_code == 501
    assert r.status_code not in (401, 403)


def test_runs_endpoint_does_not_require_token(monkeypatch):
    """``/runs/{id}`` is a read-only state lookup — left unguarded for the
    demo (per Phase 11.1 plan: only /recheck and future /chat are guarded).
    """
    _set_token(monkeypatch, "test-token-value-123")
    client = TestClient(app)
    r = client.get("/runs/no-such-id")
    assert r.status_code == 404
    assert r.status_code not in (401, 403)


def test_healthz_does_not_require_token(monkeypatch):
    """Cloud Run health probes hit ``/healthz`` without our header."""
    _set_token(monkeypatch, "test-token-value-123")
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200


def test_constant_time_compare_is_used(monkeypatch):
    """Prove we use ``secrets.compare_digest`` (constant-time) and not ``==``.

    Mechanical: patch the compare_digest symbol that ``agent.auth`` imported
    and assert it was called on the wrong-token path.
    """
    _set_token(monkeypatch, "test-token-value-123")
    with patch("agent.auth.secrets.compare_digest", return_value=False) as cmp_:
        client = TestClient(app)
        r = client.post(
            "/recheck",
            headers={"X-DriftScribe-Token": "anything"},
        )
    assert r.status_code == 403
    assert cmp_.called, "auth must use secrets.compare_digest, not =="
    # And the args were bytes (consistent types, per compare_digest contract).
    args, _kwargs = cmp_.call_args
    assert all(isinstance(a, bytes) for a in args), (
        "compare_digest args must be bytes for the constant-time guarantee"
    )
