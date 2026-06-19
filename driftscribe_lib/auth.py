"""Inter-service auth helpers — Cloud Run to Cloud Run.

Distinct from ``agent.auth`` (operator-facing X-DriftScribe-Token guard).
This module provides the *coordinator → worker* direction: minting and
verifying audience-bound Google ID tokens. Proven in spike 11.0.

See ``docs/architecture/multi-agent-design.md`` for how the two layers
fit together.
"""
import hmac
from typing import Any, Iterable

from fastapi import HTTPException, Request, status
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport import requests as gar
from google.oauth2 import id_token


def mint_id_token(audience_url: str) -> str:
    """Mint a Google-signed ID token bound to ``audience_url`` (the callee
    service's root URL — strip any path component first).

    Uses Application Default Credentials via the Cloud Run metadata server.
    The auth library caches tokens per-audience, so callers don't need
    their own cache layer. First call from a cold instance is ~50-100 ms;
    subsequent calls are negligible.
    """
    return id_token.fetch_id_token(gar.Request(), audience_url)


def verify_caller(
    request: Request,
    *,
    own_url: str,
    allowed_callers: Iterable[str],
) -> str:
    """FastAPI dependency-style helper that verifies the inbound Bearer token.

    - **401** if the Authorization header is missing / not Bearer-shaped / empty,
      or the token fails verification (wrong audience, expired, bad signature, or
      a JWKS ``GoogleAuthError`` — all collapse to a uniform detail so a probe
      can't tell which check failed; the detail never echoes the exception).
    - **403** if the token is valid but the caller's email isn't in
      ``allowed_callers`` (the detail never echoes the presented email; an
      off-spec non-string email claim is a 403, not a 500).

    Returns the verified caller email on success.

    ``own_url`` must be the worker's own root URL (no trailing slash, no path).
    ``allowed_callers`` is the set of service-account emails the worker accepts —
    typically just ``{coordinator-sa@<project>.iam.gserviceaccount.com}``.

    Hardened to mirror :func:`verify_oidc_caller`; the only intended difference is
    the return type — this returns the caller email ``str`` (the 9 worker
    ``_verify_caller_dep`` wrappers depend on that), not the claims dict.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )
    try:
        claims = id_token.verify_oauth2_token(token, gar.Request(), audience=own_url)
    except (ValueError, google_auth_exceptions.GoogleAuthError):
        # Uniform 401 — don't disclose which check failed (mirrors verify_oidc_caller).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    allowed = set(allowed_callers)
    email = claims.get("email")
    # isinstance check BEFORE compare_digest: an off-spec non-str email would
    # raise TypeError in compare_digest (→ 500). The correct outcome is 403.
    # The detail never echoes ``email`` (no caller-identity disclosure).
    if not isinstance(email, str) or not any(
        hmac.compare_digest(email, a) for a in allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )
    return email


def verify_oidc_caller(
    request: Request,
    *,
    audience: str,
    allowed_emails: Iterable[str],
    transport: Any,
) -> dict[str, Any]:
    """Verify an inbound machine caller's Google-signed OIDC token.

    A reusable entry-guard for callee endpoints invoked by a Google identity
    (Cloud Scheduler → the pre-warm endpoint), mirroring the ``/eventarc``
    handler's contract. Distinct from :func:`verify_caller`: that one is for the
    coordinator→worker direction where the *coordinator* mints the token; here
    the coordinator is the callee and verifies the caller.

    - **401** — Authorization header missing / not Bearer-shaped / empty token,
      or ``verify_oauth2_token`` raises (bad signature, wrong audience, expired,
      issuer mismatch, or a JWKS ``TransportError``). All collapse to 401 so a
      probe cannot distinguish which check failed.
    - **403** — token verifies but the ``email`` claim isn't in ``allowed_emails``
      (the ``isinstance(str)`` guard before ``compare_digest`` keeps an off-spec
      non-string claim a 403, not a 500). The detail never echoes the presented
      email.

    Returns the verified claims dict on success. ``audience`` must match exactly
    what the caller stamped (for Cloud Scheduler, ``--oidc-token-audience`` — the
    full endpoint URL). ``transport`` is a ``google.auth.transport`` Request used
    to fetch Google's JWKS (pass a shared module-level instance to avoid
    allocating a session per call).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )
    try:
        claims = id_token.verify_oauth2_token(token, transport, audience=audience)
    except (ValueError, google_auth_exceptions.GoogleAuthError):
        # Uniform 401 — don't disclose which check failed (mirrors /eventarc).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    allowed = set(allowed_emails)
    presented = claims.get("email")
    # isinstance check BEFORE compare_digest: an off-spec non-str email would
    # raise TypeError in compare_digest (→ 500). The correct outcome is 403.
    if not isinstance(presented, str) or not any(
        hmac.compare_digest(presented, a) for a in allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller service account not allowed",
        )
    return claims
