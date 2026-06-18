"""Unit tests for ``driftscribe_lib.auth.verify_oidc_caller``.

A reusable OIDC entry-guard for machine callers (Cloud Scheduler → the
pre-warm endpoint), modelled on the ``/eventarc`` handler's contract:

* 401 — Authorization header missing / not Bearer-shaped / empty token / the
  token fails verification (bad signature, wrong audience, expired, issuer
  mismatch, JWKS transport error). All collapse to 401 so a probe can't tell
  which check failed.
* 403 — token verifies but the ``email`` claim isn't in the allowlist (or is a
  non-string off-spec claim — must 403, never 500 via a TypeError into
  compare_digest).
* returns the verified claims dict on success.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from google.auth import exceptions as google_auth_exceptions

import driftscribe_lib.auth as auth_mod
from driftscribe_lib.auth import verify_oidc_caller

_AUD = "https://coord.example/internal/infra-graph/refresh"
_ALLOWED = {"infra-prewarm-sa@proj.iam.gserviceaccount.com"}
_TRANSPORT = object()  # opaque — verify_oauth2_token is patched, never really called


def _req(authorization: str | None):
    headers = {} if authorization is None else {"Authorization": authorization}
    return SimpleNamespace(headers=headers)


def _patch_verify(monkeypatch, *, returns=None, raises=None):
    def fake(token, transport, audience):
        assert audience == _AUD
        assert transport is _TRANSPORT
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(auth_mod.id_token, "verify_oauth2_token", fake)


def _call(req):
    return verify_oidc_caller(
        req, audience=_AUD, allowed_emails=_ALLOWED, transport=_TRANSPORT
    )


def test_happy_path_returns_claims(monkeypatch):
    claims = {"email": "infra-prewarm-sa@proj.iam.gserviceaccount.com", "sub": "123"}
    _patch_verify(monkeypatch, returns=claims)
    assert _call(_req("Bearer good.token")) == claims


def test_missing_authorization_header_401(monkeypatch):
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))})
    with pytest.raises(HTTPException) as ei:
        _call(_req(None))
    assert ei.value.status_code == 401


def test_non_bearer_header_401(monkeypatch):
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Basic abc"))
    assert ei.value.status_code == 401


def test_empty_bearer_token_401(monkeypatch):
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer    "))
    assert ei.value.status_code == 401


def test_verify_value_error_401(monkeypatch):
    _patch_verify(monkeypatch, raises=ValueError("wrong audience"))
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer bad.aud"))
    assert ei.value.status_code == 401


def test_verify_google_auth_error_401(monkeypatch):
    _patch_verify(monkeypatch, raises=google_auth_exceptions.TransportError("jwks down"))
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer x"))
    assert ei.value.status_code == 401


def test_email_not_in_allowlist_403(monkeypatch):
    _patch_verify(monkeypatch, returns={"email": "someone-else@proj.iam.gserviceaccount.com"})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer ok"))
    assert ei.value.status_code == 403


def test_non_string_email_claim_403_not_500(monkeypatch):
    # Off-spec but signed token whose email claim is a non-str must 403, not
    # raise TypeError into compare_digest (→ 500).
    _patch_verify(monkeypatch, returns={"email": ["a", "list"]})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer ok"))
    assert ei.value.status_code == 403


def test_does_not_echo_presented_email(monkeypatch):
    _patch_verify(monkeypatch, returns={"email": "attacker@evil.example"})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer ok"))
    assert "attacker@evil.example" not in str(ei.value.detail)
