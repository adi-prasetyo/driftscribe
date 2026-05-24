"""Cloudflare Access JWT verification for the coordinator's outer auth.

Sits alongside :mod:`agent.auth` as a *second accepted credential* on
operator-facing routes: a request that carries a valid
``Cf-Access-Jwt-Assertion`` header (minted by Cloudflare Access after the
user signs in via the team's identity provider) is treated as authenticated,
so a signed-in browser session doesn't also have to paste
``X-DriftScribe-Token``.

The CF-Access path only activates when BOTH ``cf_access_team_domain`` and
``cf_access_aud_tag`` settings are non-empty. Local dev and the unit-test
boot path leave them empty, so the existing token-based behavior is
unchanged.

JWT trust model (verified per CF docs at
https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/):
- Algorithm pinned to ``RS256`` (CF Access mints only RS256; never trust
  the ``alg`` from the token header).
- ``aud`` claim equals the Access Application's AUD tag (one per app).
- ``iss`` claim equals ``https://<team_domain>``.
- ``exp``/``iat``/``nbf`` validated by PyJWT (required).
- Email allowlisting is intentionally NOT enforced here — the CF Access
  Policy on the Application already restricts which emails can get a JWT
  in the first place. Duplicating that list in the coordinator's config
  invites drift; the trust boundary is "CF Access issued a valid JWT for
  this Application's AUD" + the policy's own enforcement.

JWKS caching: per-team_domain in-memory dict, TTL ~1h. On a ``kid`` miss
we refresh once (Cloudflare rotates keys periodically) and raise if the
key still isn't there. Tests cover both the cache hit and refresh-on-miss
paths.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx
import jwt

_log = logging.getLogger("driftscribe.agent.cf_access")

# Cloudflare Access mints RS256 only; never trust the ``alg`` from the token
# header (otherwise a forged JWT with ``alg=none`` or HS256-with-pubkey-as-secret
# would bypass the signature check).
_ALLOWED_ALGS = ["RS256"]

# JWKS fetch timeout — short on purpose. JWKS endpoint is CDN-fronted and
# normally <100ms; a slow response is more likely a network issue than a
# legitimate fetch we should wait for.
_JWKS_TIMEOUT_SEC = 5.0

# Cache TTL. Cloudflare's signing keys rotate roughly every ~6 weeks per their
# docs; an hour cap balances "tolerate clock skew on key rotation" against
# "don't hammer the JWKS endpoint per-request".
_JWKS_CACHE_TTL_SEC = 3600.0

# team_domain shape pin: a hostname (RFC 1123-ish), no scheme/path/query/port.
# We strip leading/trailing whitespace before this check. The match is
# intentionally loose — Cloudflare's actual team domains are always of the
# form ``<team>.cloudflareaccess.com``, but we don't pin the suffix here so
# the test suite can use a different host without monkeypatching this regex.
# Use ``\Z`` (not ``$``) so a trailing newline doesn't sneak through —
# Python's default ``$`` matches just before a final \n.
_TEAM_DOMAIN_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


class CfAccessJwtError(Exception):
    """Raised on any failure during CF Access JWT verification.

    Callers should treat this as a single rejection signal and not
    differentiate on the specific cause (e.g. the auth shim in
    :func:`agent.auth.verify_token` falls back to token auth on any
    ``CfAccessJwtError``).
    """


@dataclass
class _JwksCacheEntry:
    keys: dict[str, "jwt.PyJWK"]
    fetched_at: float


# Module-level cache. Keyed by team_domain. Tests reset this via
# :func:`_reset_cache_for_tests` (no public reset — production never needs
# to evict; a process restart suffices for cache invalidation).
_JWKS_CACHE: dict[str, _JwksCacheEntry] = {}


def _validate_team_domain(team_domain: str) -> str:
    """Reject team_domain values that look like a URL fragment.

    Codex review M-4: ``team_domain`` is used to build BOTH the JWKS URL
    and the expected ``iss`` claim. If someone configures
    ``https://adp-app.cloudflareaccess.com/foo`` instead of
    ``adp-app.cloudflareaccess.com``, the JWKS URL becomes nonsense and
    the iss check silently passes against a bogus expected-iss. Catch the
    bad shape at the boundary rather than producing a confusing 403.
    """
    # Don't strip() — silent whitespace-trimming would mask a config error
    # (e.g. a trailing newline in the env value). Fail loud.
    td = team_domain or ""
    if not td or not _TEAM_DOMAIN_RE.match(td):
        raise CfAccessJwtError(
            f"invalid cf_access_team_domain (must be a bare hostname, got {team_domain!r})"
        )
    return td


def _fetch_jwks(team_domain: str) -> dict[str, "jwt.PyJWK"]:
    """Fetch + parse the JWKS for ``team_domain``. Raises on any failure."""
    url = f"https://{team_domain}/cdn-cgi/access/certs"
    try:
        resp = httpx.get(url, timeout=_JWKS_TIMEOUT_SEC)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CfAccessJwtError(f"JWKS fetch failed for {url}: {exc}") from exc

    keys: dict[str, jwt.PyJWK] = {}
    for key_dict in body.get("keys") or []:
        kid = key_dict.get("kid")
        if not kid:
            continue
        try:
            keys[kid] = jwt.PyJWK.from_dict(key_dict)
        except Exception:  # noqa: BLE001 — PyJWT raises a variety of subclasses
            # Skip a single malformed key; don't kill the whole JWKS over it.
            continue
    if not keys:
        raise CfAccessJwtError(f"JWKS at {url} contained no usable keys")
    return keys


def _get_signing_key(team_domain: str, kid: str) -> "jwt.PyJWK":
    """Return the JWK for ``kid``, fetching/refreshing the cache as needed.

    Refresh policy: cache hit by team_domain + kid → return. Otherwise (kid
    miss or expired TTL) → refetch once; if still missing, raise.
    """
    now = time.monotonic()
    cached = _JWKS_CACHE.get(team_domain)
    if cached and now - cached.fetched_at < _JWKS_CACHE_TTL_SEC:
        key = cached.keys.get(kid)
        if key is not None:
            return key
        # kid miss — fall through to refresh.
    keys = _fetch_jwks(team_domain)
    _JWKS_CACHE[team_domain] = _JwksCacheEntry(keys=keys, fetched_at=now)
    key = keys.get(kid)
    if key is None:
        raise CfAccessJwtError(
            f"kid {kid!r} not found in JWKS for {team_domain} (after refresh)"
        )
    return key


def verify_cf_access_jwt(
    token: str, team_domain: str, aud_tag: str
) -> dict:
    """Verify a CF Access JWT. Returns claims dict on success; raises on failure.

    :param token: the raw value of the ``Cf-Access-Jwt-Assertion`` header.
    :param team_domain: the bare team hostname (e.g. ``adp-app.cloudflareaccess.com``).
    :param aud_tag: the per-Application AUD value (one per Access App).
    """
    if not token:
        raise CfAccessJwtError("empty JWT")
    td = _validate_team_domain(team_domain)
    if not aud_tag:
        raise CfAccessJwtError("empty cf_access_aud_tag")

    # Extract the kid from the header WITHOUT trusting the alg field.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise CfAccessJwtError(f"malformed JWT header: {exc}") from exc
    kid = unverified_header.get("kid")
    if not kid:
        raise CfAccessJwtError("JWT header missing kid")

    signing_key = _get_signing_key(td, kid)

    try:
        claims = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=_ALLOWED_ALGS,
            audience=aud_tag,
            issuer=f"https://{td}",
            options={"require": ["exp", "iat", "nbf", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise CfAccessJwtError(f"JWT verification failed: {exc}") from exc
    return claims


def _reset_cache_for_tests() -> None:
    """Test-only hook to clear the module-level JWKS cache.

    Production never needs to evict the cache — a process restart (and thus
    Cloud Run cold start) is the cache lifecycle. Tests use this between
    cases to avoid cross-test contamination.
    """
    _JWKS_CACHE.clear()
