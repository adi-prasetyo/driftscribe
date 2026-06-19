"""Unit tests for ``driftscribe_lib.auth.verify_caller``.

The coordinator→worker inter-service guard used by 9 workers via a thin
``_verify_caller_dep``. Hardened (2026-06-19) to match its sibling
``verify_oidc_caller`` — no info disclosure in error details:

* **401** — Authorization header missing / not Bearer-shaped / empty token / the
  token fails verification (``ValueError`` *or* ``GoogleAuthError`` — a JWKS
  transport failure collapses to 401, not a 500). The detail never echoes the
  verification exception.
* **403** — token verifies but the ``email`` claim isn't in ``allowed_callers``
  (or is a non-string off-spec claim — 403, never a 500 via ``compare_digest``).
  The detail never echoes the presented email.
* returns the verified caller email **string** on success.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as gar

import driftscribe_lib.auth as auth_mod
from driftscribe_lib.auth import verify_caller

_OWN_URL = "https://reader.example.com"
_ALLOWED = {"coordinator@proj.iam.gserviceaccount.com"}


def _req(authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    return SimpleNamespace(headers=headers)


def _patch_verify(monkeypatch, *, returns=None, raises=None, calls=None):
    def fake(token, transport, audience):
        if calls is not None:
            calls.append(
                {"token": token, "transport": transport, "audience": audience}
            )
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(auth_mod.id_token, "verify_oauth2_token", fake)


def _call(req):
    return verify_caller(req, own_url=_OWN_URL, allowed_callers=_ALLOWED)


def test_happy_path_returns_email(monkeypatch):
    email = next(iter(_ALLOWED))
    _patch_verify(monkeypatch, returns={"email": email, "sub": "123"})
    assert _call(_req("Bearer good.token")) == email


def test_missing_authorization_header_401_and_verify_not_called(monkeypatch):
    calls = []
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))}, calls=calls)
    with pytest.raises(HTTPException) as ei:
        _call(_req(None))
    assert ei.value.status_code == 401
    assert calls == []  # short-circuit before touching the verifier


def test_non_bearer_header_401_and_verify_not_called(monkeypatch):
    calls = []
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))}, calls=calls)
    with pytest.raises(HTTPException) as ei:
        _call(_req("Basic abc"))
    assert ei.value.status_code == 401
    assert calls == []


def test_empty_bearer_token_401_and_verify_not_called(monkeypatch):
    calls = []
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))}, calls=calls)
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer    "))
    assert ei.value.status_code == 401
    assert calls == []  # never hand an empty string to the verifier


def test_verify_value_error_401(monkeypatch):
    _patch_verify(monkeypatch, raises=ValueError("audience mismatch"))
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer bad.aud"))
    assert ei.value.status_code == 401


def test_verify_google_auth_error_401(monkeypatch):
    # A JWKS fetch failure (GoogleAuthError, not ValueError) must collapse to a
    # uniform 401, not propagate as a 500.
    _patch_verify(
        monkeypatch, raises=google_auth_exceptions.TransportError("jwks down")
    )
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer x"))
    assert ei.value.status_code == 401


def test_caller_not_in_allowlist_403(monkeypatch):
    _patch_verify(
        monkeypatch, returns={"email": "intruder@proj.iam.gserviceaccount.com"}
    )
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


def test_403_does_not_echo_presented_email(monkeypatch):
    # THE declared follow-up: the 403 detail must not leak the caller's email.
    presented = "attacker@evil.example"
    _patch_verify(monkeypatch, returns={"email": presented})
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer ok"))
    assert ei.value.status_code == 403
    assert presented not in str(ei.value.detail)


def test_401_does_not_echo_exception_text(monkeypatch):
    # The 401 detail must not leak which Google-auth check failed.
    _patch_verify(monkeypatch, raises=ValueError("audience mismatch: expected-XYZ"))
    with pytest.raises(HTTPException) as ei:
        _call(_req("Bearer bad"))
    assert ei.value.status_code == 401
    assert "audience mismatch" not in str(ei.value.detail)
    assert "expected-XYZ" not in str(ei.value.detail)


def test_verify_receives_own_url_audience_and_gar_request_transport(monkeypatch):
    # Preserve the original call contract: audience=own_url, transport=gar.Request().
    calls = []
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))}, calls=calls)
    _call(_req("Bearer good"))
    assert len(calls) == 1
    assert calls[0]["audience"] == _OWN_URL
    assert isinstance(calls[0]["transport"], gar.Request)


def test_allowlist_check_uses_compare_digest(monkeypatch):
    # Prove the allowlist match goes through constant-time compare_digest.
    used = {"n": 0}
    real = auth_mod.hmac.compare_digest

    def spy(a, b):
        used["n"] += 1
        return real(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", spy)
    _patch_verify(monkeypatch, returns={"email": next(iter(_ALLOWED))})
    _call(_req("Bearer good"))
    assert used["n"] >= 1
