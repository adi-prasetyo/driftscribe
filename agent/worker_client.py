"""Coordinator → worker HTTP client (Phase 11.7).

This module is the coordinator's *only* outbound mutation seam. Every ADK
tool that needs to change a system (Cloud Run env, docs PRs, rollback
proposals, notifications) routes through here; the legacy direct-GCP /
direct-GitHub code paths in :mod:`agent.adk_tools` are gone in 11.7.

Three jobs:

1. **Mint an audience-bound Google ID token** via
   :func:`driftscribe_lib.auth.mint_id_token`. The audience MUST be the
   worker's root URL (no trailing slash, no endpoint path) — Cloud Run
   validates the audience claim against the receiving service's URL,
   not the URL the client *called*. Mixing this up silently breaks
   inter-service auth on custom domains.

2. **POST JSON to the worker's canonical endpoint.** Worker endpoints
   are intentionally hardcoded in :data:`WORKER_ENDPOINTS` rather than
   caller-supplied — exposing a "call arbitrary worker endpoint" tool
   to the LLM would be a Layer 0 violation.

3. **Surface errors as a structured :class:`WorkerClientError`.** Status
   code is preserved (4xx vs 5xx vs the synthetic 503 we return for
   transport / config failures), and the body is truncated to bound
   what the chat handler echoes back to the operator.

Worker URLs are read lazily via :func:`_worker_url` rather than at
import time. Codex review of the 11.7 plan flagged that module-level
env reads make pytest order-sensitive (a test that monkeypatches
``READER_URL`` *after* this module has already cached the value gets
silently ignored). Lazy lookup keeps tests predictable while costing
one ``os.environ.get`` per call — cheap.
"""
from __future__ import annotations

import os
from typing import Final

import httpx

from driftscribe_lib.auth import mint_id_token
from driftscribe_lib.logging import current_trace_id_or_new


# Per-worker env var name → fixed at boot for the deployed service via
# cloudbuild.yaml's two-step OWN_URL pattern (deploy with placeholder, then
# gcloud services update with the real URL after every worker is up).
_WORKER_URL_ENV: Final[dict[str, str]] = {
    "reader": "READER_URL",
    "docs": "DOCS_URL",
    "rollback": "ROLLBACK_URL",
    "notifier": "NOTIFIER_URL",
}


# Each worker has exactly ONE coordinator-facing endpoint. The /execute
# special-case for rollback is wrapped in :func:`call_execute` below —
# we never let the caller (and especially never let the LLM) pick the
# endpoint path freely, which would be a Layer 0 capability escape.
WORKER_ENDPOINTS: Final[dict[str, str]] = {
    "reader": "/read",
    "docs": "/patch",
    "rollback": "/propose",
    "notifier": "/notify",
}


# Bound the body we surface back to the chat caller. Worker responses
# may contain stack traces, internal URLs, or PII during failures —
# truncate to a sane length so a single 502 doesn't echo 50KB of detail
# into the operator's chat reply.
_ERROR_BODY_TRUNCATE: Final[int] = 500


# Outbound HTTP timeout. Cloud Run cold starts on workers can take a
# couple of seconds, and the docs worker's PR creation hits the GitHub
# API, but 30s is plenty headroom — anything past that is almost
# certainly a hang we'd rather fail fast on.
_HTTPX_TIMEOUT: Final[float] = 30.0


class WorkerClientError(Exception):
    """Structured error for any worker-side or transport-side failure.

    Carries enough context for the caller (``/chat`` handler, approval
    POST handler) to decide whether to surface the failure to the
    operator and what status code to map it to. We deliberately do NOT
    raise :class:`fastapi.HTTPException` here — the client module is
    framework-agnostic; the handler maps to HTTPException at the
    boundary.

    Attributes:
        status_code: the HTTP status from the worker, or 503 for
            transport / config failures the client manufactured.
        body: the response body (truncated). Empty string when no
            response was received.
        worker: the worker name (``"reader"`` etc.) — useful for logs.
    """

    def __init__(self, status_code: int, body: str, worker: str):
        truncated = (body or "")[:_ERROR_BODY_TRUNCATE]
        super().__init__(f"{worker} returned {status_code}: {truncated}")
        self.status_code = status_code
        self.body = truncated
        self.worker = worker


def _worker_url(worker: str) -> str:
    """Resolve the worker's base URL from env. Raises if unset.

    Lazy lookup (not module-level) so tests can monkeypatch env per-test
    without import-order side effects. The Cloud Run deployment sets
    these via the post-deploy step in cloudbuild.yaml; missing config at
    runtime is a deploy bug, not a per-request error.
    """
    env_name = _WORKER_URL_ENV.get(worker)
    if env_name is None:
        raise WorkerClientError(503, f"unknown worker {worker!r}", worker)
    url = os.environ.get(env_name, "").rstrip("/")
    if not url:
        raise WorkerClientError(
            503,
            f"worker {worker!r} URL not configured ({env_name} unset/empty)",
            worker,
        )
    return url


def call(worker: str, payload: dict, *, endpoint: str | None = None) -> dict:
    """POST ``payload`` to the named worker. Return parsed JSON response.

    Audience binding: the ID token's ``aud`` claim is the worker's root
    URL (``base``), not the full endpoint URL. Cloud Run validates the
    audience against the receiving service's URL — feeding the endpoint
    URL here would silently work today (Cloud Run strips the path for
    the audience check) but breaks if we ever move to custom domains.

    Args:
        worker: one of ``"reader" | "docs" | "rollback" | "notifier"``.
        payload: JSON-serializable dict matching the worker's request
            schema. The worker's pydantic model enforces
            ``extra="forbid"`` so a typo here surfaces as a 422.
        endpoint: override the default endpoint. Only used internally
            by :func:`call_execute` to reach the rollback worker's
            ``/execute`` route. NOT exposed to ADK tools.

    Raises:
        WorkerClientError: with status_code preserved from the worker
        on non-2xx, or 503 for transport / config failures.
    """
    base = _worker_url(worker)
    path = endpoint or WORKER_ENDPOINTS[worker]
    # Audience is the *root* URL, not base+path — see the docstring.
    token = mint_id_token(base)
    # Phase 15.2: propagate the coordinator's per-request trace id to
    # the worker so a single trace id correlates logs across the call
    # chain. The ContextVar is set by the trace middleware in
    # ``driftscribe_lib.logging``; on the rare path where worker_client
    # is invoked outside a request scope (e.g. a CLI smoke test) the
    # ContextVar is empty — ``current_trace_id_or_new`` mints a fresh
    # one. It also validates the ContextVar value matches our 32-char
    # hex format, so a stray ``set_trace_id("not-a-uuid")`` somewhere
    # in the codebase cannot leak a malformed id downstream.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Trace-Id": current_trace_id_or_new(),
    }
    try:
        with httpx.Client(timeout=_HTTPX_TIMEOUT) as client:
            r = client.post(f"{base}{path}", json=payload, headers=headers)
    except httpx.RequestError as e:
        # Connection refused, DNS failure, timeout — anything that
        # prevented a response. The synthetic 503 disambiguates this
        # from a real worker-returned 5xx in the caller's error path.
        raise WorkerClientError(
            503, f"{worker} unreachable: {type(e).__name__}: {e}", worker
        ) from e

    if not 200 <= r.status_code < 300:
        raise WorkerClientError(r.status_code, r.text, worker)

    # Defensive: workers always return JSON on 2xx, but a misconfigured
    # proxy or future cache layer could insert HTML. Surface a 503-ish
    # error rather than crash the chat handler.
    try:
        return r.json()
    except ValueError as e:
        raise WorkerClientError(
            502, f"{worker} returned non-JSON body: {e}", worker
        ) from e


def call_execute(approval_id: str, approval_token: str) -> dict:
    """Special-case wrapper for the rollback worker's ``/execute`` endpoint.

    Kept as a separate function (rather than letting a tool call
    ``call("rollback", ..., endpoint="/execute")``) so the approve-path
    code in :mod:`agent.main` reads as a single named operation, and
    the LLM-facing tools never get the option to hit /execute directly.
    The LLM only ever calls /propose; the operator's button press is
    what triggers /execute via the coordinator's approval POST handler.
    """
    return call(
        "rollback",
        {"approval_id": approval_id, "approval_token": approval_token},
        endpoint="/execute",
    )


def call_deny(approval_id: str, approval_token: str) -> dict:
    """Special-case wrapper for the rollback worker's ``/deny`` endpoint.

    Phase 11.9 (Codex review of 11.7, critical finding #1): the
    coordinator no longer transactionally flips ``pending → denied``
    itself, because doing so required no token validation and turned the
    reject button into a HITL availability vector. The token verification
    has to live on the worker (the only service with the HMAC key), so
    the deny operation moves there too. Mirrors :func:`call_execute` in
    shape — both decision paths now go through audience-bound ID-token
    auth to the same worker.

    The LLM never calls this directly; the operator's Reject click in
    the coordinator's approval POST handler is what triggers it.
    """
    return call(
        "rollback",
        {"approval_id": approval_id, "approval_token": approval_token},
        endpoint="/deny",
    )
