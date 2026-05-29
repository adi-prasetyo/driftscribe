"""Unit tests for ``driftscribe_lib.cf_access.verify_cf_access_jwt``.

Generates a real RSA keypair, mints JWTs with PyJWT, mocks the CF JWKS
endpoint via respx (already a test dep). Covers the trust-boundary cases
documented in the module docstring: valid path, wrong audience, wrong
issuer, expired, bad signature, kid not in JWKS (with one refresh), and
the team_domain shape pin.
"""
from __future__ import annotations

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from driftscribe_lib.cf_access import (
    CfAccessJwtError,
    _reset_cache_for_tests,
    canonical_operator_email,
    verify_cf_access_jwt,
)

TEAM = "test-team.cloudflareaccess.com"
AUD = "test-aud-tag-deadbeef"
JWKS_URL = f"https://{TEAM}/cdn-cgi/access/certs"


def _b64url_uint(i: int) -> str:
    import base64
    b = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _new_keypair() -> tuple[rsa.RSAPrivateKey, dict]:
    """Generate a fresh RSA keypair; return (private_key, public-JWK-fragment).

    The returned dict has ``kty``/``n``/``e`` only — callers add ``kid``
    via :func:`_make_jwks` so a single keypair can be served under
    different kids across rotation-test cases.
    """
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = priv.public_key().public_numbers()
    return priv, {"kty": "RSA", "n": _b64url_uint(nums.n), "e": _b64url_uint(nums.e)}


def _make_jwks(jwk_pub: dict, kid: str) -> dict:
    return {"keys": [{**jwk_pub, "kid": kid, "alg": "RS256", "use": "sig"}]}


def _mint(priv, kid: str, *, aud=AUD, iss=f"https://{TEAM}", exp_offset=300, nbf_offset=-5):
    now = int(time.time())
    return jwt.encode(
        {
            "aud": aud,
            "iss": iss,
            "iat": now,
            "nbf": now + nbf_offset,
            "exp": now + exp_offset,
            "email": "user@example.com",
            "sub": "subject-123",
        },
        priv,
        algorithm="RS256",
        headers={"kid": kid},
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


@respx.mock
def test_happy_path_returns_claims():
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid)))

    token = _mint(priv, kid)
    claims = verify_cf_access_jwt(token, TEAM, AUD)

    assert claims["aud"] == AUD
    assert claims["iss"] == f"https://{TEAM}"
    assert claims["email"] == "user@example.com"


@respx.mock
def test_wrong_audience_raises():
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid)))

    token = _mint(priv, kid, aud="other-aud-tag")
    with pytest.raises(CfAccessJwtError, match="JWT verification failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


@respx.mock
def test_wrong_issuer_raises():
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid)))

    token = _mint(priv, kid, iss="https://other-team.cloudflareaccess.com")
    with pytest.raises(CfAccessJwtError, match="JWT verification failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


@respx.mock
def test_expired_raises():
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid)))

    token = _mint(priv, kid, exp_offset=-60)  # expired 1min ago
    with pytest.raises(CfAccessJwtError, match="JWT verification failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


@respx.mock
def test_bad_signature_raises():
    # Mint with one keypair, present the JWKS with a DIFFERENT keypair's public key.
    real_priv, _ = _new_keypair()
    _decoy_priv, decoy_jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(decoy_jwk_pub, kid)))

    token = _mint(real_priv, kid)
    with pytest.raises(CfAccessJwtError, match="JWT verification failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


@respx.mock
def test_kid_miss_refreshes_jwks_once_then_raises():
    """A kid we've never seen forces ONE refresh; if still missing, raise.

    Cloudflare's JWKS endpoint is the source of truth for valid keys. A kid
    that doesn't appear after a fresh fetch means the JWT was signed with a
    key we don't know about — reject rather than retry indefinitely.
    """
    _priv, jwk_pub = _new_keypair()
    # JWKS always returns kid-A; we'll mint a token with kid-B.
    route = respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_pub, "kid-A")))
    other_priv, _ = _new_keypair()
    token = _mint(other_priv, "kid-B")

    with pytest.raises(CfAccessJwtError, match="not found in JWKS"):
        verify_cf_access_jwt(token, TEAM, AUD)
    # We should have hit the JWKS endpoint exactly once (the refresh on miss
    # — the initial cache was empty so the FIRST fetch + the "miss after
    # refresh" path together still produce a single network call).
    assert route.call_count == 1


@respx.mock
def test_kid_miss_then_refresh_finds_new_key():
    """A kid added by Cloudflare's key rotation is picked up on refresh."""
    priv_old, jwk_old = _new_keypair()
    priv_new, jwk_new = _new_keypair()

    # First call: cache miss + initial fetch returns only old key. Token
    # uses old kid → works.
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_old, "kid-old")))
    token_old = _mint(priv_old, "kid-old")
    verify_cf_access_jwt(token_old, TEAM, AUD)  # populates cache

    # Now Cloudflare rotates: JWKS endpoint returns only the new key.
    # A token signed with the new kid forces a refresh.
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, json=_make_jwks(jwk_new, "kid-new")))
    token_new = _mint(priv_new, "kid-new")
    verify_cf_access_jwt(token_new, TEAM, AUD)  # should succeed after refresh


@respx.mock
def test_jwks_fetch_500_raises_jwt_error():
    respx.get(JWKS_URL).mock(return_value=httpx.Response(500, text="upstream down"))
    priv, _ = _new_keypair()
    token = _mint(priv, "any-kid")
    with pytest.raises(CfAccessJwtError, match="JWKS fetch failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


@respx.mock
def test_jwks_invalid_json_raises():
    respx.get(JWKS_URL).mock(return_value=httpx.Response(200, text="not json"))
    priv, _ = _new_keypair()
    token = _mint(priv, "any-kid")
    with pytest.raises(CfAccessJwtError, match="JWKS fetch failed"):
        verify_cf_access_jwt(token, TEAM, AUD)


def test_empty_token_raises():
    with pytest.raises(CfAccessJwtError, match="empty JWT"):
        verify_cf_access_jwt("", TEAM, AUD)


def test_empty_aud_raises():
    with pytest.raises(CfAccessJwtError, match="empty cf_access_aud_tag"):
        verify_cf_access_jwt("anything", TEAM, "")


@pytest.mark.parametrize("bad_domain", [
    "",
    "https://team.cloudflareaccess.com",
    "team.cloudflareaccess.com/path",
    "team.cloudflareaccess.com?q=1",
    "team.cloudflareaccess.com:443",
    " team.cloudflareaccess.com",
    "team.cloudflareaccess.com\n",
    "-startswithdash.cloudflareaccess.com",
])
def test_malformed_team_domain_raises(bad_domain):
    """Codex review M-4: reject URL-shaped (scheme/path/query/port) and
    obvious-typo values before they get spliced into the JWKS URL or the
    expected ``iss``. Per-label RFC1035 hostname strictness is not the
    threat model — config typos and stray whitespace/URLs are.
    """
    with pytest.raises(CfAccessJwtError, match="invalid cf_access_team_domain"):
        verify_cf_access_jwt("anything", bad_domain, AUD)


def test_malformed_jwt_header_raises():
    with pytest.raises(CfAccessJwtError, match="malformed JWT header"):
        verify_cf_access_jwt("not.a.jwt", TEAM, AUD)


def test_jwt_header_missing_kid_raises():
    # Mint a JWT without setting headers={"kid": ...}; PyJWT won't add one.
    priv, _ = _new_keypair()
    bad_token = jwt.encode({"aud": AUD, "iss": f"https://{TEAM}"}, priv, algorithm="RS256")
    with pytest.raises(CfAccessJwtError, match="missing kid"):
        verify_cf_access_jwt(bad_token, TEAM, AUD)


# --- Phase C5b-1: canonical_operator_email -----------------------------------
# THE single canonicalization used at both sign-time (coordinator) and
# compare-time (the C5b-2 worker), so it must be byte-identical on both sides
# and fail closed on any non-canonical input.


def test_canonical_operator_email_lowercases_and_strips():
    assert canonical_operator_email({"email": "  Op@Example.COM "}) == "op@example.com"


def test_canonical_operator_email_missing_key_raises():
    with pytest.raises(CfAccessJwtError, match="email"):
        canonical_operator_email({"sub": "subject-123"})


def test_canonical_operator_email_non_str_raises():
    with pytest.raises(CfAccessJwtError, match="email"):
        canonical_operator_email({"email": 12345})


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_canonical_operator_email_empty_or_whitespace_raises(blank):
    with pytest.raises(CfAccessJwtError, match="email"):
        canonical_operator_email({"email": blank})


def test_canonical_operator_email_too_long_raises():
    # 321 chars (one over the RFC forward-path max of 320).
    too_long = "a" * 314 + "@ex.com"  # 314 + len("@ex.com")==7 == 321
    assert len(too_long) == 321
    with pytest.raises(CfAccessJwtError, match="320"):
        canonical_operator_email({"email": too_long})


# --- Phase C5b-1: require_cf_operator dependency -----------------------------
# The CF-Access-MANDATORY coordinator dependency (NO X-DriftScribe-Token
# fallback) — the operator-identity gate for the C5e infra-approval POST. We
# exercise it end-to-end through a throwaway FastAPI route (mirroring how
# test_token_guard.py drives verify_token), reusing the JWT-mint + respx JWKS
# helpers above. Settings come from the real Settings() via env vars + a cache
# clear, so we test the actual get_settings() path the dependency takes.

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent.auth import require_cf_operator  # noqa: E402
from agent.config import get_settings  # noqa: E402


def _cf_operator_app() -> FastAPI:
    """A throwaway app whose one route returns the dependency's email verbatim."""
    app = FastAPI()

    @app.get("/whoami")
    def _whoami(email: str = Depends(require_cf_operator)) -> dict:
        return {"email": email}

    return app


@pytest.fixture
def _cf_settings(monkeypatch):
    """Configure CF Access via env + bust the Settings cache; reset on teardown.

    Yields a no-arg helper to (re)configure or unconfigure CF Access mid-test.
    """
    def _configure(*, team=TEAM, aud=AUD):
        if team is None:
            monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
        else:
            monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", team)
        if aud is None:
            monkeypatch.delenv("CF_ACCESS_AUD_TAG", raising=False)
        else:
            monkeypatch.setenv("CF_ACCESS_AUD_TAG", aud)
        get_settings.cache_clear()

    yield _configure
    get_settings.cache_clear()


def test_require_cf_operator_unconfigured_returns_503(_cf_settings):
    """No CF Access config → 503 fail-closed (never falls back to a token)."""
    _cf_settings(team=None, aud=None)
    client = TestClient(_cf_operator_app())
    r = client.get("/whoami", headers={"Cf-Access-Jwt-Assertion": "anything"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


@respx.mock
def test_require_cf_operator_missing_header_returns_401(_cf_settings):
    _cf_settings()
    client = TestClient(_cf_operator_app())
    r = client.get("/whoami")  # no Cf-Access-Jwt-Assertion header
    assert r.status_code == 401
    assert "Cf-Access-Jwt-Assertion" in r.json()["detail"]


@respx.mock
def test_require_cf_operator_invalid_jwt_returns_403(_cf_settings):
    """A forged/bad-signature JWT → 403, and the response must NOT echo the
    verifier's exception detail."""
    _cf_settings()
    _real_priv, _ = _new_keypair()
    _decoy_priv, decoy_jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=_make_jwks(decoy_jwk_pub, kid))
    )
    token = _mint(_real_priv, kid)  # signed by a key NOT in the served JWKS

    client = TestClient(_cf_operator_app())
    r = client.get("/whoami", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail == "CF Access verification failed"
    # Do not leak the underlying PyJWT/CfAccessJwtError message.
    assert "signature" not in detail.lower()


@respx.mock
def test_require_cf_operator_valid_jwt_returns_canonical_email(_cf_settings):
    _cf_settings()
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid))
    )
    # _mint sets email="user@example.com" (already lowercase) — assert the
    # dependency returns the canonical form.
    token = _mint(priv, kid)

    client = TestClient(_cf_operator_app())
    r = client.get("/whoami", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 200
    assert r.json() == {"email": "user@example.com"}


@respx.mock
def test_require_cf_operator_valid_jwt_without_email_returns_403(_cf_settings):
    """A valid JWT whose claims lack an ``email`` → 403 (canonicalization fails
    closed)."""
    _cf_settings()
    priv, jwk_pub = _new_keypair()
    kid = "kid-1"
    respx.get(JWKS_URL).mock(
        return_value=httpx.Response(200, json=_make_jwks(jwk_pub, kid))
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": AUD,
            "iss": f"https://{TEAM}",
            "iat": now,
            "nbf": now - 5,
            "exp": now + 300,
            "sub": "subject-123",  # NOTE: no "email" claim
        },
        priv,
        algorithm="RS256",
        headers={"kid": kid},
    )

    client = TestClient(_cf_operator_app())
    r = client.get("/whoami", headers={"Cf-Access-Jwt-Assertion": token})
    assert r.status_code == 403
    assert r.json()["detail"] == "CF Access verification failed"
