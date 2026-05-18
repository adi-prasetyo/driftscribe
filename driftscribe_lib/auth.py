"""Inter-service auth helpers — Cloud Run to Cloud Run.

Distinct from ``agent.auth`` (operator-facing X-DriftScribe-Token guard).
This module provides the *coordinator → worker* direction: minting and
verifying audience-bound Google ID tokens. Proven in spike 11.0.

See ``docs/architecture/multi-agent-design.md`` for how the two layers
fit together.
"""
from typing import Iterable

from fastapi import HTTPException, Request, status
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

    - 401 if Authorization header missing / malformed / wrong audience / expired.
    - 403 if token is valid but the caller's email isn't in ``allowed_callers``.

    Returns the verified caller email on success.

    ``own_url`` must be the worker's own root URL (no trailing slash, no path).
    ``allowed_callers`` is the set of service-account emails the worker accepts —
    typically just ``{coordinator-sa@<project>.iam.gserviceaccount.com}``.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = id_token.verify_oauth2_token(token, gar.Request(), audience=own_url)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {e}",
        )
    email = claims.get("email")
    if email not in set(allowed_callers):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"caller {email!r} not in allowed_callers",
        )
    return email
