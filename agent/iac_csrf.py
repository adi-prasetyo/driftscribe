"""Signed, artifact-bound CSRF form token for the C5e ``/iac-approvals`` route.

The GET ``/iac-approvals/{pr_number}`` page mints this token and embeds it in a
hidden form field; the C5e-3 POST verifies it before doing anything. The token
serves two roles at once (Codex blocker #1):

1. **CSRF token** — a stateless HMAC signature an attacker's cross-site POST
   cannot forge (CF Access does NOT stop CSRF; the static-token-derived key does).
2. **Artifact pin** — the payload encodes the EXACT artifact identity the GET
   rendered (head_sha, the metadata URI + generation, both sha256s, comment id).
   The POST re-fetches and re-verifies *that* artifact, so a C2 re-run between the
   operator's GET and their POST cannot silently swap the plan under them.

**Key derivation.** The signing key is derived from the coordinator's static
``driftscribe_token`` so that secret never leaves the server:

    key = HMAC(driftscribe_token, b"iac-csrf-key", sha256)

The domain-separation tag (``b"iac-csrf-key"``) keeps this key distinct from any
other use of the static token.

**Fail-closed.** ``require_cf_operator`` does not require ``driftscribe_token``,
so this module is the route's own guard for it: if the secret is unset, mint and
verify both raise :class:`IacCsrfError` (the GET route catches it and suppresses
Approve with an "approvals not configured" reason; the POST route maps it to 503).

**Wire form.** ``base64url(canonical_json).rstrip("=") + "." + hex(sig)`` where
``sig = HMAC(key, canonical_json_bytes, sha256)``. The signature covers the
canonical JSON BYTES, not the base64 — so two equivalent encodings cannot both
verify, and we never trust attacker-supplied base64 padding.

Pure stdlib (``hmac``, ``hashlib``, ``base64``, ``json``, ``time``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.config import Settings

_KEY_TAG = b"iac-csrf-key"
_DEFAULT_TTL_SECONDS = 1800  # 30 minutes

# Exact payload schema. ``verify_form_token`` rejects a payload whose key set
# differs, or whose field types are wrong, BEFORE returning it — so the C5e-3
# POST can treat the returned dict as a trusted, well-formed artifact pin
# (Codex C5e-2 review nit). A signed token already implies these hold for tokens
# WE minted; this is belt-and-suspenders against a future mint-side change.
_PAYLOAD_KEYS = frozenset(
    {
        "pr",
        "head_sha",
        "artifact_uri_metadata",
        "generation_metadata",
        "plan_sha256",
        "plan_json_sha256",
        "comment_id",
        # C6: the iac-tree sidecar identity the GET rendered — pinned so the operator
        # can't approve a page whose sidecar was swapped under them (the worker still
        # re-derives + cross-checks the real sidecar; this is operator-review
        # integrity, not a worker security input). Always present as strings (empty
        # for a pre-C6 / no-sidecar comment).
        "generation_iac_tree",
        "iac_tree_hash",
        "exp",
    }
)
_STR_FIELDS = (
    "head_sha",
    "artifact_uri_metadata",
    "generation_metadata",
    "plan_sha256",
    "plan_json_sha256",
    "generation_iac_tree",
    "iac_tree_hash",
)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _payload_well_formed(payload: dict[str, Any]) -> bool:
    """True iff ``payload`` has exactly the expected keys and field types."""
    if set(payload.keys()) != _PAYLOAD_KEYS:
        return False
    if not _is_int(payload["pr"]) or not _is_int(payload["exp"]):
        return False
    if not (payload["comment_id"] is None or _is_int(payload["comment_id"])):
        return False
    return all(isinstance(payload[k], str) for k in _STR_FIELDS)


class IacCsrfError(Exception):
    """The server cannot mint/verify a form token (``driftscribe_token`` unset).

    Distinct from a forged/expired/wrong token — those return ``None`` (a
    client error). This signals a SERVER misconfiguration: the route maps the
    GET to "approvals not configured" and the POST to 503.
    """


def _derive_key(settings: "Settings") -> bytes:
    """Derive the HMAC key from the static token, fail-closed on an empty secret."""
    secret = settings.driftscribe_token or ""
    if not secret:
        raise IacCsrfError("driftscribe_token is unset; cannot sign form tokens")
    return hmac.new(secret.encode("utf-8"), _KEY_TAG, hashlib.sha256).digest()


def _canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON encoding: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    # Restore padding (urlsafe_b64decode requires a length multiple of 4).
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_form_token(
    settings: "Settings",
    *,
    pr_number: int,
    head_sha: str,
    artifact_uri_metadata: str,
    generation_metadata: str,
    plan_sha256: str,
    plan_json_sha256: str,
    comment_id: int | None,
    generation_iac_tree: str = "",
    iac_tree_hash: str = "",
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a signed, artifact-bound form token.

    ``now`` is injectable for tests (defaults to ``time.time()``); the token
    expires at ``int(now) + ttl_seconds``. Raises :class:`IacCsrfError` if
    ``settings.driftscribe_token`` is empty (the route's own guard for that
    secret, which ``require_cf_operator`` does not require).
    """
    key = _derive_key(settings)
    if now is None:
        now = time.time()
    payload = {
        "pr": pr_number,
        "head_sha": head_sha,
        "artifact_uri_metadata": artifact_uri_metadata,
        "generation_metadata": generation_metadata,
        "plan_sha256": plan_sha256,
        "plan_json_sha256": plan_json_sha256,
        "comment_id": comment_id,
        "generation_iac_tree": generation_iac_tree,
        "iac_tree_hash": iac_tree_hash,
        "exp": int(now) + ttl_seconds,
    }
    canonical = _canonical_json(payload)
    sig = hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return _b64url_nopad(canonical.encode("utf-8")) + "." + sig


def verify_form_token(
    settings: "Settings",
    token: str,
    *,
    pr_number: int,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Verify a form token; return its payload dict, or ``None`` on any failure.

    Recomputes the signature over the DECODED canonical JSON bytes and compares
    it constant-time to the supplied signature, validates the payload has exactly
    the expected keys + field types, then checks ``payload["pr"] == pr_number``
    and ``payload["exp"] > now``. Any malformed / forged / expired / wrong-PR /
    wrong-shape token returns ``None`` — never raises, EXCEPT :class:`IacCsrfError`
    when ``settings.driftscribe_token`` is unset (the server can't verify
    without its key).
    """
    key = _derive_key(settings)  # raises IacCsrfError if the secret is unset
    if now is None:
        now = time.time()

    if not isinstance(token, str):
        return None
    payload_b64, sep, sig_hex = token.partition(".")
    if not sep or not payload_b64 or not sig_hex:
        return None

    try:
        canonical = _b64url_decode(payload_b64)
    except (ValueError, TypeError):
        return None

    expected_sig = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_hex):
        return None

    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not _payload_well_formed(payload):
        return None

    if payload["pr"] != pr_number:
        return None
    if not payload["exp"] > now:
        return None

    return payload
