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

from agent import cf_access
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

    async def _stub_do_recheck(trigger, force=False, *, workload="drift"):
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
    """``/eventarc`` is exempt from the ``X-DriftScribe-Token`` guard — it
    auths via Google-signed ID tokens from Eventarc (Phase 14.2).

    The handler enforces ``Authorization: Bearer <id-token>`` instead, so a
    POST without that header returns 401 from the Eventarc-auth path, not
    from the X-DriftScribe-Token guard. We pin that the 401 is specifically
    about Authorization (Bearer flow) rather than the operator token.
    """
    _set_token(monkeypatch, "test-token-value-123")
    # EVENTARC_AUDIENCE must be set for the handler to reach the
    # Authorization-header check (empty audience → 503 fail-closed).
    monkeypatch.setenv("EVENTARC_AUDIENCE", "https://example.a.run.app")
    get_settings.cache_clear()
    client = TestClient(app)
    r = client.post("/eventarc")
    assert r.status_code == 401
    detail = r.json()["detail"].lower()
    assert "authorization" in detail
    # Not the operator-token guard's 401 (different header name).
    assert "x-driftscribe-token" not in detail


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


# --- Phase 21: Cloudflare Access integration --------------------------------
# The verify_token dependency accepts EITHER X-DriftScribe-Token OR a valid
# Cf-Access-Jwt-Assertion (when CF Access settings are configured). These
# tests pin the combined behavior. The CF JWT verification itself is tested
# in tests/unit/test_cf_access.py — here we just stub verify_cf_access_jwt
# and assert verify_token honors the two-credential contract.


def _configure_cf_access(monkeypatch, team="adp-app.cloudflareaccess.com", aud="aud-tag-123"):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", team)
    monkeypatch.setenv("CF_ACCESS_AUD_TAG", aud)
    get_settings.cache_clear()


def _stub_do_recheck_sentinel():
    sentinel = {"_token_guard_passed": True, "action": "no_op"}

    async def _stub(trigger, force=False, *, workload="drift"):
        return sentinel

    return sentinel, _stub


def test_recheck_accepts_valid_cf_access_jwt_without_token(monkeypatch):
    """Signed-in CF Access user — no X-DriftScribe-Token needed."""
    _set_token(monkeypatch, "test-token-value-123")
    _configure_cf_access(monkeypatch)
    sentinel, stub = _stub_do_recheck_sentinel()

    with patch("agent.auth.verify_cf_access_jwt", return_value={"email": "ok@example.com"}) as v, \
         patch("agent.main._do_recheck", side_effect=stub):
        client = TestClient(app)
        r = client.post("/recheck", headers={"Cf-Access-Jwt-Assertion": "valid.jwt.value"})

    assert r.status_code == 200
    assert r.json() == sentinel
    v.assert_called_once()


def test_recheck_rejects_invalid_cf_jwt_when_no_token_present(monkeypatch):
    """Bad CF JWT alone → falls back to token check → 401 (no header)."""
    _set_token(monkeypatch, "test-token-value-123")
    _configure_cf_access(monkeypatch)

    with patch(
        "agent.auth.verify_cf_access_jwt",
        side_effect=cf_access.CfAccessJwtError("expired"),
    ):
        client = TestClient(app)
        r = client.post("/recheck", headers={"Cf-Access-Jwt-Assertion": "stale.jwt"})

    assert r.status_code == 401
    assert "X-DriftScribe-Token" in r.json()["detail"]


def test_recheck_falls_back_when_cf_jwt_invalid_but_token_valid(monkeypatch):
    """Bad CF JWT + valid X-DriftScribe-Token → succeed via token path.

    Codex review: silent fallback is desirable so a stale CF cookie can't
    poison a request that ALSO carries the strictly-stronger shared token.
    """
    _set_token(monkeypatch, "test-token-value-123")
    _configure_cf_access(monkeypatch)
    sentinel, stub = _stub_do_recheck_sentinel()

    with patch(
        "agent.auth.verify_cf_access_jwt",
        side_effect=cf_access.CfAccessJwtError("kid not found"),
    ), patch("agent.main._do_recheck", side_effect=stub):
        client = TestClient(app)
        r = client.post(
            "/recheck",
            headers={
                "Cf-Access-Jwt-Assertion": "bad.jwt",
                "X-DriftScribe-Token": "test-token-value-123",
            },
        )

    assert r.status_code == 200
    assert r.json() == sentinel


def test_cf_access_path_disabled_when_settings_empty(monkeypatch):
    """Empty CF Access config → the JWT header is ignored entirely.

    Verifies the on/off semantics: if the operator hasn't set CF_ACCESS_*
    env vars, sending a Cf-Access-Jwt-Assertion header should not be a
    backdoor — the request must still satisfy the token check.
    """
    _set_token(monkeypatch, "test-token-value-123")
    # NOTE: CF Access settings deliberately NOT set here.
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD_TAG", raising=False)
    get_settings.cache_clear()

    with patch("agent.auth.verify_cf_access_jwt") as v:
        client = TestClient(app)
        r = client.post("/recheck", headers={"Cf-Access-Jwt-Assertion": "would.have.worked"})

    assert r.status_code == 401  # token missing → reject; CF JWT was ignored
    v.assert_not_called()  # critical: never even reached the verifier


def test_cf_access_jwt_failure_is_logged(monkeypatch, caplog):
    """Codex review: bad/stale JWT should produce an INFO log line so an
    operator can distinguish 'JWT rejected' from 'no JWT sent' when
    debugging 401s.
    """
    import logging
    _set_token(monkeypatch, "test-token-value-123")
    _configure_cf_access(monkeypatch)
    caplog.set_level(logging.INFO, logger="driftscribe.agent.auth")

    with patch(
        "agent.auth.verify_cf_access_jwt",
        side_effect=cf_access.CfAccessJwtError("kid-X not in JWKS"),
    ):
        client = TestClient(app)
        client.post("/recheck", headers={"Cf-Access-Jwt-Assertion": "stale"})

    rejected = [r for r in caplog.records if r.message == "cf_access_jwt_rejected"]
    assert rejected, "expected a cf_access_jwt_rejected log line"
    # Reason is in the extra dict; pin the field for downstream log-search.
    assert "kid-X" in getattr(rejected[0], "reason", "")


def test_constant_time_compare_is_used(monkeypatch):
    """Lock in that auth uses ``secrets.compare_digest`` instead of ``==``.

    Does NOT prove constant-time behavior itself (that's CPython's guarantee for
    ``compare_digest``, not something this test can observe). Catches a future
    refactor that quietly swaps in ``==``.
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
