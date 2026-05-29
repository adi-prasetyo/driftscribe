"""Unit tests for the signed, artifact-bound CSRF form token (Phase C5e-2).

The token is BOTH a CSRF token and a pin of the exact C2 artifact the GET page
rendered. The C5e-3 POST will verify it. These tests cover the mint→verify
roundtrip and every fail-closed path:

- wrong pr_number → None,
- expired token → None,
- tampered signature → None,
- tampered payload (re-base64 a changed dict with the original sig) → None,
- malformed wire forms (no dot / bad base64 / bad json) → None,
- unset ``driftscribe_token`` → ``IacCsrfError`` on BOTH mint and verify.
"""
from __future__ import annotations

import base64
import json

import pytest

from agent.config import Settings
from agent.iac_csrf import IacCsrfError, mint_form_token, verify_form_token

# A realistic artifact identity for the token payload.
_ARGS = dict(
    pr_number=42,
    head_sha="a" * 40,
    artifact_uri_metadata="gs://test-proj-tofu-artifacts/pr-42/"
    + "a" * 40
    + "/run-7-1/metadata.json",
    generation_metadata="1700000000000003",
    plan_sha256="b" * 64,
    plan_json_sha256="c" * 64,
    comment_id=99887766,
)


def _settings(token: str = "super-secret-static-token") -> Settings:
    return Settings(driftscribe_token=token)


def test_mint_verify_roundtrip_returns_payload() -> None:
    s = _settings()
    token = mint_form_token(s, now=1000.0, ttl_seconds=1800, **_ARGS)
    payload = verify_form_token(s, token, pr_number=42, now=1000.0)
    assert payload is not None
    assert payload["pr"] == 42
    assert payload["head_sha"] == _ARGS["head_sha"]
    assert payload["artifact_uri_metadata"] == _ARGS["artifact_uri_metadata"]
    assert payload["generation_metadata"] == _ARGS["generation_metadata"]
    assert payload["plan_sha256"] == _ARGS["plan_sha256"]
    assert payload["plan_json_sha256"] == _ARGS["plan_json_sha256"]
    assert payload["comment_id"] == _ARGS["comment_id"]
    assert payload["exp"] == 1000 + 1800


def test_verify_wrong_pr_number_returns_none() -> None:
    s = _settings()
    token = mint_form_token(s, now=1000.0, **_ARGS)
    assert verify_form_token(s, token, pr_number=43, now=1000.0) is None


def test_verify_expired_returns_none() -> None:
    s = _settings()
    token = mint_form_token(s, now=1000.0, ttl_seconds=10, **_ARGS)
    # exp = 1010; now strictly past it → expired.
    assert verify_form_token(s, token, pr_number=42, now=1011.0) is None
    # boundary: exp is NOT > now when now == exp → expired.
    assert verify_form_token(s, token, pr_number=42, now=1010.0) is None
    # still valid one second before.
    assert verify_form_token(s, token, pr_number=42, now=1009.0) is not None


def test_verify_tampered_signature_returns_none() -> None:
    s = _settings()
    token = mint_form_token(s, now=1000.0, **_ARGS)
    payload_b64, _, _sig = token.partition(".")
    forged = payload_b64 + "." + ("0" * 64)
    assert verify_form_token(s, forged, pr_number=42, now=1000.0) is None


def test_verify_tampered_payload_returns_none() -> None:
    """Re-base64 a mutated payload but keep the original signature → reject."""
    s = _settings()
    token = mint_form_token(s, now=1000.0, **_ARGS)
    payload_b64, _, sig = token.partition(".")
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    mutated = json.loads(raw)
    mutated["head_sha"] = "f" * 40  # swap the artifact identity
    new_json = json.dumps(mutated, sort_keys=True, separators=(",", ":"))
    new_b64 = base64.urlsafe_b64encode(new_json.encode()).decode().rstrip("=")
    forged = new_b64 + "." + sig
    assert verify_form_token(s, forged, pr_number=42, now=1000.0) is None


def test_verify_malformed_no_dot_returns_none() -> None:
    s = _settings()
    assert verify_form_token(s, "no-dot-here", pr_number=42, now=1000.0) is None


def test_verify_malformed_bad_base64_returns_none() -> None:
    s = _settings()
    assert verify_form_token(s, "@@@not-base64@@@.deadbeef", pr_number=42, now=1.0) is None


def test_verify_malformed_bad_json_returns_none() -> None:
    s = _settings()
    not_json = base64.urlsafe_b64encode(b"this is not json").decode().rstrip("=")
    # Compute a VALID signature over the (non-json) bytes so we get past the sig
    # check and exercise the json-parse failure path specifically.
    import hashlib
    import hmac as _hmac

    key = _hmac.new(
        s.driftscribe_token.encode("utf-8"), b"iac-csrf-key", hashlib.sha256
    ).digest()
    sig = _hmac.new(key, b"this is not json", hashlib.sha256).hexdigest()
    token = not_json + "." + sig
    assert verify_form_token(s, token, pr_number=42, now=1.0) is None


def test_empty_token_raises_on_mint() -> None:
    s = _settings("")
    with pytest.raises(IacCsrfError):
        mint_form_token(s, now=1000.0, **_ARGS)


def test_empty_token_raises_on_verify() -> None:
    # A token minted with a real secret, then verified against an empty-secret
    # settings → IacCsrfError (the server can't verify without its key).
    real = mint_form_token(_settings(), now=1000.0, **_ARGS)
    with pytest.raises(IacCsrfError):
        verify_form_token(_settings(""), real, pr_number=42, now=1000.0)


def test_different_secret_rejects() -> None:
    token = mint_form_token(_settings("key-a"), now=1000.0, **_ARGS)
    assert verify_form_token(_settings("key-b"), token, pr_number=42, now=1000.0) is None


def _sign(s: Settings, payload: dict) -> str:
    """Mint a token over an ARBITRARY payload (bypassing mint's fixed schema)."""
    import hashlib
    import hmac as _hmac

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    key = _hmac.new(
        s.driftscribe_token.encode("utf-8"), b"iac-csrf-key", hashlib.sha256
    ).digest()
    sig = _hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(canonical.encode()).decode().rstrip("=")
    return b64 + "." + sig


def test_verify_rejects_extra_key() -> None:
    s = _settings()
    payload = {
        "pr": 42,
        "head_sha": "a" * 40,
        "artifact_uri_metadata": _ARGS["artifact_uri_metadata"],
        "generation_metadata": "1700000000000003",
        "plan_sha256": "b" * 64,
        "plan_json_sha256": "c" * 64,
        "comment_id": 1,
        "exp": 99999999999,
        "rogue": "extra",  # well-signed, but not in the schema
    }
    assert verify_form_token(s, _sign(s, payload), pr_number=42, now=1000.0) is None


def test_verify_rejects_missing_key() -> None:
    s = _settings()
    payload = {  # missing artifact_uri_metadata
        "pr": 42,
        "head_sha": "a" * 40,
        "generation_metadata": "1700000000000003",
        "plan_sha256": "b" * 64,
        "plan_json_sha256": "c" * 64,
        "comment_id": 1,
        "exp": 99999999999,
    }
    assert verify_form_token(s, _sign(s, payload), pr_number=42, now=1000.0) is None


def test_verify_rejects_wrong_field_type() -> None:
    s = _settings()
    payload = {
        "pr": 42,
        "head_sha": 12345,  # should be str
        "artifact_uri_metadata": _ARGS["artifact_uri_metadata"],
        "generation_metadata": "1700000000000003",
        "plan_sha256": "b" * 64,
        "plan_json_sha256": "c" * 64,
        "comment_id": 1,
        "exp": 99999999999,
    }
    assert verify_form_token(s, _sign(s, payload), pr_number=42, now=1000.0) is None


def test_verify_rejects_bool_pr() -> None:
    s = _settings()
    payload = {
        "pr": True,  # bool must not pass the int check (or the == 1 confusion)
        "head_sha": "a" * 40,
        "artifact_uri_metadata": _ARGS["artifact_uri_metadata"],
        "generation_metadata": "1700000000000003",
        "plan_sha256": "b" * 64,
        "plan_json_sha256": "c" * 64,
        "comment_id": 1,
        "exp": 99999999999,
    }
    assert verify_form_token(s, _sign(s, payload), pr_number=1, now=1000.0) is None


def test_comment_id_none_roundtrips() -> None:
    s = _settings()
    args = dict(_ARGS)
    args["comment_id"] = None
    token = mint_form_token(s, now=1000.0, **args)
    payload = verify_form_token(s, token, pr_number=42, now=1000.0)
    assert payload is not None
    assert payload["comment_id"] is None
