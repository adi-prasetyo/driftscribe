"""Operator-facing token auth for the coordinator's public endpoints.

This is the *outer* auth boundary: humans (and the demo's curl commands)
present ``X-DriftScribe-Token`` when calling endpoints like ``/recheck`` and
the future ``/chat``. It is deliberately separate from the
Cloud-Run-to-Cloud-Run auth layer used by workers, which relies on
audience-bound Google ID tokens (proved in spike 11.0, wired in Phase 11.3+).
See ``docs/architecture/multi-agent-design.md`` for how the two layers compose.

Status-code semantics (HTTP-conformant):
- 503 if ``DRIFTSCRIBE_TOKEN`` is unset — fail closed so a deploy that forgot
  ``--set-secrets`` cannot silently expose the route.
- 401 if the ``X-DriftScribe-Token`` header is absent.
- 403 if the header is present but doesn't match.

Constant-time comparison via ``secrets.compare_digest`` so the response time
doesn't leak how many leading bytes of the supplied token matched. Realistically
the demo is single-user behind one shared token, but doing it right is cheap
and the test enforces it so a future refactor can't quietly regress to ``==``.
"""

import secrets

from fastapi import Header, HTTPException, status

from agent.config import get_settings


def verify_token(x_driftscribe_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency that enforces the ``X-DriftScribe-Token`` header.

    Returns ``None`` on success; raises ``HTTPException`` on failure.

    Wire on a route via ``Depends(verify_token)``. Future endpoints (``/chat``
    in Phase 11.7) can opt in with zero changes to this module.
    """
    expected = get_settings().driftscribe_token
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
