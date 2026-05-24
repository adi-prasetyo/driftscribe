"""Operator-facing token auth for the coordinator's public endpoints.

This is the *outer* auth boundary: humans (and the demo's curl commands)
present ``X-DriftScribe-Token`` when calling endpoints like ``/recheck`` and
``/chat``. It is deliberately separate from the Cloud-Run-to-Cloud-Run auth
layer used by workers, which relies on audience-bound Google ID tokens
(proved in spike 11.0, wired in Phase 11.3+). See
``docs/architecture/multi-agent-design.md`` for how the two layers compose.

Status-code semantics (HTTP-conformant) for the token path:
- 503 if ``DRIFTSCRIBE_TOKEN`` is unset — fail closed so a deploy that forgot
  ``--set-secrets`` cannot silently expose the route.
- 401 if neither credential is present.
- 403 if a credential is present but invalid.

Constant-time comparison via ``secrets.compare_digest`` so the response time
doesn't leak how many leading bytes of the supplied token matched.

Phase 21 — Cloudflare Access integration: when both ``cf_access_team_domain``
and ``cf_access_aud_tag`` are configured, a valid ``Cf-Access-Jwt-Assertion``
header is ALSO accepted as proof of authentication. The two credentials are
checked in order:

  1. If a CF Access JWT is present AND CF Access is configured, verify it.
     On success, allow the request (no token required).
     On failure, fall back silently to the token check below. Rationale:
     a stale CF cookie or rotated key shouldn't poison a request that ALSO
     carries a valid X-DriftScribe-Token.
  2. Existing X-DriftScribe-Token check (unchanged from pre-Phase-21).

The fallback failure is logged at INFO (one line per request, no token
contents) so an operator can tell "CF JWT rejected" from "no CF JWT sent"
when debugging.
"""

import logging
import secrets

from fastapi import Header, HTTPException, status

from agent.cf_access import CfAccessJwtError, verify_cf_access_jwt
from agent.config import get_settings

_log = logging.getLogger("driftscribe.agent.auth")


def verify_token(
    x_driftscribe_token: str | None = Header(default=None),
    cf_access_jwt: str | None = Header(default=None, alias="Cf-Access-Jwt-Assertion"),
) -> None:
    """FastAPI dependency that enforces either credential for the route.

    Returns ``None`` on success; raises ``HTTPException`` on failure.

    Wire on a route via ``Depends(verify_token)``.
    """
    settings = get_settings()

    # 1. Cloudflare Access JWT path — only active when both config values set.
    if cf_access_jwt and settings.cf_access_team_domain and settings.cf_access_aud_tag:
        try:
            verify_cf_access_jwt(
                cf_access_jwt,
                settings.cf_access_team_domain,
                settings.cf_access_aud_tag,
            )
            return
        except CfAccessJwtError as exc:
            # Single INFO line per rejected JWT — gives operators a signal
            # that's distinguishable from "no CF JWT sent" without leaking
            # the token bytes. Falls through to the token check below.
            _log.info("cf_access_jwt_rejected", extra={"reason": str(exc)})

    # 2. X-DriftScribe-Token path — unchanged from pre-Phase-21.
    expected = settings.driftscribe_token
    if not expected:
        # Fail-closed canary: if the server didn't load the token, refuse
        # *every* request rather than silently accepting all.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="auth not configured: DRIFTSCRIBE_TOKEN unset",
        )
    if x_driftscribe_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-DriftScribe-Token header",
        )
    # Both sides as bytes — compare_digest requires consistent types and
    # raises on mismatch. Encoding here is explicit so the test can assert
    # we hand bytes to the comparator.
    if not secrets.compare_digest(
        x_driftscribe_token.encode("utf-8"), expected.encode("utf-8")
    ):
        # Do NOT echo the provided value — that's a needless info leak even
        # if the user already knows what they typed.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid X-DriftScribe-Token",
        )
