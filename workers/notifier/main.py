"""Notifier Agent — Worker #4 of 4 (Phase 11.6).

The simplest of the four workers, and the only one whose "capability" *is*
its secret: knowing the webhook URL is the entire authorization model.
``POST /notify`` takes ``{channel, severity, body}``, builds a normalized
payload, and POSTs it to a single outbound URL — the one loaded from
Secret Manager at boot. The caller cannot supply or override the URL.

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``notifier-agent-sa`` has **no project-level
  GCP role grants.** Its only privilege is ``roles/secretmanager.secretAccessor``
  on the ``driftscribe-webhook-url`` secret (per-secret binding). It cannot
  read Cloud Run, Firestore, GitHub, or any other secret. See
  ``docs/architecture/iam-matrix.md``.
- **Layer 2 (payload-intent policy):** the request schema
  (:class:`NotifyRequest` with ``extra="forbid"``) is the textbook
  confused-deputy defense. A compromised coordinator cannot smuggle in a
  ``url`` field to redirect notifications to an attacker-controlled host —
  ``url`` is not in the schema, ``extra="forbid"`` rejects it at 422
  before httpx is ever touched. ``channel`` and ``severity`` are
  ``Literal``-constrained; ``body`` is capped at 10000 chars so an attacker
  can't burn the worker's egress quota with a 1MB payload either.
- **Layer 3 (inter-service auth):** :func:`driftscribe_lib.auth.verify_caller`
  validates the inbound Google ID token's audience claim against ``OWN_URL``
  and the caller's email against ``ALLOWED_CALLERS``.

Layers 0 (tool registry) and 4 (HITL approval) live elsewhere and are
out of scope for this worker.

Failure mode for misconfiguration: ``NOTIFY_WEBHOOK_URL`` is read at
**module import**. An empty / unset value raises ``KeyError`` and the
Cloud Run revision fails to come up, which is the correct fail-closed
behavior for a service whose ONLY purpose is to call that URL — better
to surface the config error in the deploy logs than to silently return
503 on every request forever.
"""
from __future__ import annotations

import os
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

log = setup_logging("notifier-agent")

# Boot-time env resolution. All four MUST be set; KeyError → Cloud Run
# revision fails the start probe with a clear "Revision is not ready"
# error in the deploy log. The strip() on NOTIFY_WEBHOOK_URL catches the
# subtle case where the Secret Manager value has a trailing newline (the
# operator copy-pasted from a terminal). Without it, httpx would happily
# POST to "https://webhook.site/...\n" and the request would 4xx with an
# extremely confusing error.
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
NOTIFY_WEBHOOK_URL = os.environ["NOTIFY_WEBHOOK_URL"].strip()

# Empty-string secret value (e.g., operator created the secret but with
# empty data) gets caught here so the revision fails at start rather than
# silently returning 200s with an outbound POST to "" — which would either
# raise an httpx error 1000 times a day or, worse, succeed against a
# wildcard-resolving DNS.
if not NOTIFY_WEBHOOK_URL:
    raise RuntimeError(
        "NOTIFY_WEBHOOK_URL is empty — refusing to start. "
        "Check the driftscribe-webhook-url Secret Manager secret value."
    )


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper around :func:`verify_caller` so tests can swap auth via
    ``app.dependency_overrides`` without monkey-patching the shared lib."""
    return verify_caller(
        request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS
    )


class NotifyRequest(BaseModel):
    """Closed schema — see module docstring, Layer 2.

    Three constraints that together are the entire confused-deputy defense:

    - ``channel`` and ``severity`` are ``Literal``-typed enums. A caller
      that supplies ``"private-attack-channel"`` for channel fails
      validation at 422 before our handler runs.
    - ``body`` has a hard 10000-char cap. The downstream webhook
      (webhook.site for the demo, or a real Slack/Teams hook in
      production) typically rejects payloads larger than ~40KB; bounding
      to 10KB leaves headroom for the normalized envelope (channel +
      severity + service name) we wrap around the body.
    - ``extra="forbid"`` rejects ANY field not declared above. A caller
      that tries to pass ``url`` (the textbook redirect attack) or any
      other unexpected key gets 422 — no chance to ever reach httpx.
    """

    channel: Literal["info", "alert", "approval"]
    severity: Literal["low", "medium", "high", "critical"]
    body: str = Field(min_length=1, max_length=10000)

    model_config = ConfigDict(extra="forbid")


app = FastAPI(title="DriftScribe Notifier Agent")

# Phase 15.2: per-request trace id propagation (see driftscribe_lib.logging).
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — intentionally unauthenticated so Cloud Run's built-in
    health checks (and operator curl from outside the VPC) work without
    minting an ID token."""
    return {"ok": True}


@app.post("/notify")
def notify(
    req: NotifyRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Build a normalized payload and POST it to the env-configured webhook.

    Outbound HTTP behavior:

    - 10-second total timeout. Slack/Teams hooks typically respond in
      well under a second; webhook.site (the demo target) is similar.
      Setting this any longer would let a slow downstream eat into our
      Cloud Run request concurrency.
    - No retry. The coordinator is responsible for "did the notification
      land" semantics — adding a retry here would risk duplicate
      notifications to the operator on transient errors, which is more
      surprising than a single failed call that the coordinator can
      surface and the operator can re-issue.

    Status codes:

    - **200**: notification delivered (downstream returned 2xx). Body:
      ``{status, channel, severity, downstream_status}``.
    - **502**: downstream webhook returned non-2xx, or the request never
      reached the downstream (connection error, timeout, DNS failure).
      Body: ``{detail}`` with a short description that does NOT include
      the configured webhook URL — leaking the URL would defeat the
      "URL is the capability" model.
    - **422**: schema rejection (Layer 2 — bad enum, extra field,
      oversize body).
    - **401 / 403**: auth failure (delegated to ``verify_caller``).
    """
    # Build the normalized payload. The ``text`` field exists so the
    # notification is human-readable in any generic webhook viewer
    # (webhook.site shows it inline). The structured fields (service,
    # channel, severity) let a future custom receiver route or filter.
    payload = {
        "text": f"[DriftScribe/{req.channel}/{req.severity}] {req.body}",
        "service": "DriftScribe",
        "channel": req.channel,
        "severity": req.severity,
    }

    log.info(
        "notify: caller=%s channel=%s severity=%s body_len=%d",
        caller, req.channel, req.severity, len(req.body),
    )

    # ``httpx.Client`` (not ``AsyncClient``) — FastAPI's sync def handlers
    # run in a threadpool, and a sync client is easier to mock in tests.
    # Per-request client construction is fine here: this worker's QPS is
    # tiny (it's behind the coordinator, behind Eventarc) and Cloud Run
    # request concurrency is 1.
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.post(NOTIFY_WEBHOOK_URL, json=payload)
        except httpx.RequestError as e:
            # Connection error, timeout, DNS failure — anything that
            # prevented us from getting an HTTP response. The exception
            # type name is informative; we deliberately do NOT include
            # NOTIFY_WEBHOOK_URL in the surfaced detail (leaking it
            # defeats the "URL is the capability" model).
            log.warning("notify: webhook unavailable (%s)", type(e).__name__)
            raise HTTPException(
                status_code=502,
                detail=f"webhook unavailable: {type(e).__name__}",
            ) from e

    if not 200 <= resp.status_code < 300:
        # Truncate the downstream body in the surfaced detail — it
        # could be arbitrarily large or contain content we don't want
        # to echo back to the caller.
        snippet = resp.text[:200] if resp.text else ""
        log.warning(
            "notify: webhook returned %d (body snippet: %r)",
            resp.status_code, snippet,
        )
        raise HTTPException(
            status_code=502,
            detail=f"webhook returned {resp.status_code}: {snippet}",
        )

    return {
        "status": "sent",
        "channel": req.channel,
        "severity": req.severity,
        "downstream_status": resp.status_code,
    }
