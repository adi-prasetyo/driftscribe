"""Rollback Agent — Worker #3 of 4 (Phase 11.5).

The only worker that mutates live Cloud Run state. Even with the strict
IAM scoping (`roles/run.developer` granted *only* on the ``payment-demo``
service, not project-wide) the blast radius from a compromised
coordinator is "shift traffic on payment-demo to a different existing
revision" — no new deploys, no image substitution, no env changes, no
ability to touch any other service.

The two endpoints are intentionally split so the operator-facing
approval UI (which lives on the Coordinator, Phase 11.7 — *not* here,
because this worker is private and cannot host an unauthenticated
approval page) can mediate between intent (``/propose``) and execution
(``/execute``):

- ``POST /propose`` (coordinator → here): validate the target revision
  is sane, write a pending approval doc to Firestore, return a single-use
  HMAC-bound token. The coordinator stashes the approval_id, drops the
  raw token into the operator's approval URL on its own UI, and waits.
- ``POST /execute`` (coordinator → here, after operator hits "approve"):
  verify the HMAC, transactionally claim the pending approval, then call
  the Cloud Run admin API to update traffic.

Safety layers in play here:

- **Layer 1 (IAM scoping):** ``rollback-agent-sa`` has resource-scoped
  ``roles/run.developer`` on ``payment-demo`` ONLY. Project-level
  ``roles/datastore.user`` is an acknowledged constraint (Firestore
  doesn't expose collection-scope IAM), bounded by the application
  only ever touching the ``approvals/`` collection.
- **Layer 2 (payload-intent policy):**
    - Target service / region / project all sourced from env at boot.
      The request schema (``extra="forbid"``) refuses any caller-supplied
      override.
    - Target revision must exist in the service's revision list (Cloud
      Run admin read) — refuses fabricated revision names.
    - Target revision must NOT be the currently-serving revision —
      refuses no-op "rollbacks" that are actually just resource churn.
- **Layer 3 (inter-service auth):**
  :func:`driftscribe_lib.auth.verify_caller` validates the inbound
  Google ID token's audience claim against ``OWN_URL`` and the caller's
  email against ``ALLOWED_CALLERS``.
- **Layer 4 (HITL approval):** single-use HMAC-bound token, 15-min TTL,
  transactional ``pending → used`` flip in Firestore. The HMAC input
  binds the target revision so a stolen token for revision A cannot be
  used to roll back to revision B. See :mod:`driftscribe_lib.approvals`
  for the cryptographic details.

Layer 0 (tool registry) lives on the coordinator.
"""
from __future__ import annotations

import datetime as dt
import hmac as hmac_mod
import os
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from google.cloud import run_v2
from google.protobuf.field_mask_pb2 import FieldMask
from pydantic import BaseModel, ConfigDict, Field

from driftscribe_lib.approvals import (
    ApprovalStore,
    compute_token_hmac,
)
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import setup as setup_logging

log = setup_logging("rollback-agent")

# Boot-time env resolution. ``TARGET_SERVICE`` / ``TARGET_REGION`` have sane
# defaults for the hackathon demo; the rest MUST be set explicitly so a
# misconfigured Cloud Run revision fails to start (a KeyError here yields a
# clear "Revision is not ready" error instead of a runtime 500).
TARGET_SERVICE = os.environ.get("TARGET_SERVICE", "payment-demo")
TARGET_REGION = os.environ.get("TARGET_REGION", "asia-northeast1")
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
COORDINATOR_URL = os.environ["COORDINATOR_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
APPROVAL_HMAC_KEY = os.environ["APPROVAL_HMAC_KEY"]


# --------------------------------------------------------------------------- #
# Auth + indirection helpers (mirror reader / docs pattern)
# --------------------------------------------------------------------------- #


def _verify_caller_dep(request: Request) -> str:
    """Thin wrapper around :func:`verify_caller` so tests can swap auth via
    ``app.dependency_overrides`` without monkey-patching the shared lib."""
    return verify_caller(
        request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS
    )


def _get_approval_store() -> ApprovalStore:
    """Indirection for testability — tests monkeypatch this to inject an
    in-memory fake. Wrapped (rather than imported as a module-level
    singleton) so test patches don't leak across the test session."""
    return ApprovalStore(project=GCP_PROJECT)


def _get_services_client() -> run_v2.ServicesClient:
    """Indirection for testability — patched in production-shape integration
    tests; the worker-level tests patch ``_list_revisions`` / ``_apply_traffic``
    directly so this never gets called there."""
    return run_v2.ServicesClient()


def _get_revisions_client() -> run_v2.RevisionsClient:
    return run_v2.RevisionsClient()


def _service_name() -> str:
    """Fully-qualified Cloud Run service path for the configured target.

    Centralized so both ``_list_revisions`` and ``_apply_traffic`` share
    the same string and a future region/project change is a single edit.
    """
    return f"projects/{GCP_PROJECT}/locations/{TARGET_REGION}/services/{TARGET_SERVICE}"


def _list_revisions() -> tuple[list[str], str]:
    """Return ``(all_revision_short_names, active_revision_short_name)``.

    ``active`` is the revision currently receiving 100% of traffic. The
    Phase 11 demo never splits traffic (the coordinator only ever rolls
    back to a single revision at 100%), so a single "active" name is a
    clean abstraction. If a split-traffic mode is added later, this
    function will need to return the set of revisions with ``percent > 0``
    and the caller will need to reason about "active" differently.

    Short names like ``payment-demo-00003-ccc`` are the form the coordinator
    sees and the form the operator approves — fully-qualified resource
    paths (``projects/.../revisions/<short>``) are not exposed.
    """
    sclient = _get_services_client()
    svc = sclient.get_service(name=_service_name())
    active = ""
    for ts in svc.traffic_statuses:
        if ts.percent == 100 and ts.revision:
            active = ts.revision
            break

    revisions: list[str] = []
    rclient = _get_revisions_client()
    for rev in rclient.list_revisions(parent=_service_name()):
        # rev.name is the full path; the operator-facing name is the basename.
        revisions.append(rev.name.rsplit("/", 1)[-1])

    return revisions, active


def _assert_no_tagged_targets() -> None:
    """Raise :class:`HTTPException` (409) if any existing traffic target on
    the service has a tag set.

    Called twice on the happy path:

    1. As a **preflight** from ``/execute``, BEFORE the approval is
       transactionally claimed. Without this, a service that's grown a
       tagged direct-URL target since the approval was issued would have
       its rollback fail at ``_apply_traffic`` time — but only after the
       approval token has been burned by ``claim_pending``. The operator
       would then have to re-propose, eating an extra round-trip and a
       fresh approval. Per Codex review of Phase 11.5 (operational
       finding #2): we'd rather refuse early and leave the approval
       intact so the operator can clear the tag and retry the same
       approval.

    2. As a **defense-in-depth** check inside :func:`_apply_traffic` — a
       belt-and-suspenders re-check in case the preflight is bypassed by
       a future caller or the service's traffic block races between the
       preflight read and the apply.
    """
    sclient = _get_services_client()
    svc = sclient.get_service(name=_service_name())
    for existing in svc.traffic:
        if getattr(existing, "tag", ""):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"refusing rollback: service has a tagged traffic target "
                    f"(tag={existing.tag!r}). Tagged direct-URL targets are "
                    f"out of scope for the rollback agent — clear the tag "
                    f"manually first."
                ),
            )


def _apply_traffic(target_revision: str) -> str:
    """Update ``TARGET_SERVICE``'s traffic to send 100% to ``target_revision``.

    Returns the long-running operation name. We deliberately don't block
    on ``.result()`` — Cloud Run's traffic-shift LROs take 10–30s and the
    coordinator already polls. The operation name lets the coordinator
    correlate this call with the resulting Cloud Run audit log entry.

    Two safety properties (added per Codex review of Phase 11.5):

    1. **Tag-target refusal.** Cloud Run allows traffic targets that
       reference a revision by tag (`--tag=canary` style direct URLs).
       The demo deployment never uses tags, but if a future operator
       added one we would silently destroy it by replacing the traffic
       block. Refuse the rollback up-front rather than nuke tagged
       targets — the operator can manually re-tag if they really want
       this behavior. ``/execute`` ALSO runs :func:`_assert_no_tagged_targets`
       as a preflight before claiming the approval; this is a
       defense-in-depth re-check that catches a race between preflight
       read and apply.

    2. **Explicit FieldMask=traffic.** Without an ``update_mask``,
       :meth:`ServicesClient.update_service` treats the populated fields
       of the Service proto as the update set (AIP-134). Our local
       ``svc`` was just fetched from the server, so re-uploading it
       could clobber any field that another principal changed between
       our ``get_service`` and ``update_service`` calls (env vars,
       scaling settings, etc.). Restricting the mask to ``traffic``
       narrows the patch to the one field we intend to mutate.
    """
    sclient = _get_services_client()
    svc = sclient.get_service(name=_service_name())

    # Defense-in-depth re-check of the tag invariant. ``/execute`` runs
    # the preflight earlier so the approval doesn't get burned on the
    # 409 path; this block catches the race where a tag was added
    # between preflight and here.
    for existing in svc.traffic:
        if getattr(existing, "tag", ""):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"refusing rollback: service has a tagged traffic target "
                    f"(tag={existing.tag!r}). Tagged direct-URL targets are "
                    f"out of scope for the rollback agent — clear the tag "
                    f"manually first."
                ),
            )

    # Replace the entire traffic block with a single 100% target. Using
    # ``del svc.traffic[:]`` instead of ``svc.traffic.clear()`` because
    # proto-plus repeated fields don't all support ``clear()`` uniformly
    # across versions; slice-delete is the canonical idiom.
    del svc.traffic[:]
    svc.traffic.append(
        run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=target_revision,
            percent=100,
        )
    )
    op = sclient.update_service(
        service=svc,
        update_mask=FieldMask(paths=["traffic"]),
    )
    # ``op`` is a google.api_core.operation.Operation — its ``.operation.name``
    # is the LRO name. Defensive fallback to "" if the SDK shape ever changes.
    try:
        return op.operation.name
    except AttributeError:
        return ""


# --------------------------------------------------------------------------- #
# Request schemas
# --------------------------------------------------------------------------- #


# Cloud Run revision names follow ``<service>-NNNNN-<3-letter-suffix>``
# (e.g., ``payment-demo-00007-abc``). The service prefix and suffix
# character set are well-defined; cap the total length at 64 (Cloud Run's
# actual limit is 63 + null) to make oversized inputs fail at the schema
# layer rather than after an admin-API round-trip.
_REVISION_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")

# Canonical UUID4 shape: 8-4-4-4-12 lowercase hex with the version nibble
# anywhere in the third group (we don't enforce that here — Firestore
# doc IDs don't care, and over-tightening would block legitimate UUID
# variants if the underlying library ever changes). The regex makes
# ``approval_id`` strictly path-safe (no slashes, dots, percent-encoding,
# etc.) so a malformed value cannot construct an unexpected Firestore path.
_UUID_SHAPE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ProposeRequest(BaseModel):
    """Closed schema — see module docstring, Layer 2.

    No ``target_service`` / ``region`` / ``project`` fields: those are
    hardcoded at boot via env vars. ``extra="forbid"`` makes pydantic
    raise on any unexpected field, which FastAPI converts to HTTP 422.

    Field-level constraints (added per Codex review of Phase 11.5):

    - ``target_revision`` matches the Cloud Run revision-name regex
      (lowercase letters/digits/hyphens, starts with a letter, max 64
      chars). Catches gross malformations before the Cloud Run admin
      lookup runs and refuses path-traversal-style inputs at the
      schema layer.
    - ``reason`` is capped at 500 chars. The approval page renders this
      verbatim to the operator; bounding it keeps the Firestore doc
      cheap and the UI legible.
    """

    target_revision: str = Field(min_length=1, max_length=64, pattern=_REVISION_NAME.pattern)
    reason: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")


class ExecuteRequest(BaseModel):
    """Closed schema for ``/execute``.

    ``approval_id`` must be a UUID4 string (the form
    :class:`ApprovalStore.create` emits). ``approval_token`` is bounded
    at the 43-char length of :func:`secrets.token_urlsafe(32)` plus a
    little slack — anything longer is definitely malformed and should
    fail before the HMAC computation runs.
    """

    approval_id: str = Field(min_length=36, max_length=36, pattern=_UUID_SHAPE.pattern)
    approval_token: str = Field(min_length=43, max_length=64)

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# FastAPI app
# --------------------------------------------------------------------------- #


app = FastAPI(title="DriftScribe Rollback Agent")


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    """Liveness probe — intentionally unauthenticated so Cloud Run health
    checks (and operator curl from outside the VPC) work without minting
    an ID token."""
    return {"ok": True}


@app.post("/propose")
def propose(
    req: ProposeRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Create a pending approval and return the single-use token.

    The raw approval token is returned **only here**, **only once**.
    Subsequent reads of the Firestore doc see only the HMAC. The caller
    (coordinator) is responsible for delivering the raw token to the
    operator (typically embedded in the approval-page URL).

    Status codes:

    - **200**: approval created. Body: ``{approval_id, approval_token,
      approval_url, expires_at}``.
    - **400**: target_revision is the currently-active revision (Layer 2).
    - **404**: target_revision is not present in the service's revision
      list (Layer 2).
    - **401/403**: auth failure (delegated to ``verify_caller``).
    - **422**: schema violation (extra/missing field).
    """
    revisions, active = _list_revisions()
    if req.target_revision == active:
        # No-op rollback. Refuse before touching Firestore so the approvals
        # collection doesn't accumulate dead pending docs from misclicks.
        raise HTTPException(
            status_code=400,
            detail=(
                f"target_revision {req.target_revision!r} is currently active — "
                "nothing to roll back to"
            ),
        )
    if req.target_revision not in revisions:
        raise HTTPException(
            status_code=404,
            detail=(
                f"target_revision {req.target_revision!r} not found in service "
                f"{TARGET_SERVICE!r}"
            ),
        )

    store = _get_approval_store()
    approval, raw_token = store.create(
        target_revision=req.target_revision,
        reason=req.reason,
        hmac_key=APPROVAL_HMAC_KEY,
        created_by=caller,
    )
    log.info(
        "propose: id=%s rev=%s active=%s caller=%s",
        approval.approval_id, req.target_revision, active, caller,
    )
    return {
        "approval_id": approval.approval_id,
        "approval_token": raw_token,
        "approval_url": f"{COORDINATOR_URL}/approvals/{approval.approval_id}",
        "expires_at": approval.expires_at.isoformat(),
    }


@app.post("/execute")
def execute(
    req: ExecuteRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Verify the approval token and execute the rollback.

    Verification order matters:

    1. Look up the doc (404 if missing).
    2. Status pre-check (403 if not pending) — short-circuits before we
       waste a HMAC compare on a doc we know we'll reject.
    3. Expiry check (403 if past TTL).
    4. Constant-time HMAC compare against the stored ``token_hmac``.
       This is the only place we trust the caller-supplied token; using
       :func:`hmac.compare_digest` avoids the timing-side-channel where
       an attacker could probe byte-by-byte.
    5. Transactional ``pending → used`` flip via the store. This is the
       authoritative anti-replay step — even if a race got past the
       earlier status check (two ``/execute`` calls arriving concurrently),
       at most one transaction wins.
    6. Cloud Run traffic update.

    Status codes:

    - **200**: rollback initiated. Body: ``{approval_id, target_revision,
      status, operation_name}``.
    - **404**: approval doc not found.
    - **403**: status not pending / expired / wrong token / lost race.
    - **401/403**: auth failure (delegated to ``verify_caller``).
    """
    store = _get_approval_store()

    approval = store.get(req.approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if approval.status != "pending":
        raise HTTPException(
            status_code=403,
            detail=f"approval status is {approval.status!r}, not 'pending'",
        )
    if approval.expires_at < dt.datetime.now(dt.timezone.utc):
        raise HTTPException(status_code=403, detail="approval expired")

    expected_hmac = compute_token_hmac(
        req.approval_token, approval.target_revision, APPROVAL_HMAC_KEY
    )
    if not hmac_mod.compare_digest(expected_hmac, approval.token_hmac):
        log.warning(
            "execute: HMAC mismatch id=%s caller=%s",
            req.approval_id, caller,
        )
        raise HTTPException(status_code=403, detail="invalid approval token")

    # Preflight tag check: if the service has a tagged traffic target,
    # _apply_traffic will refuse with a 409. We re-check here BEFORE
    # claim_pending so the operator can clear the tag and retry the same
    # approval instead of having the token burned by an unrecoverable
    # 409. The defensive copy inside _apply_traffic remains for the
    # race between this preflight and the apply.
    _assert_no_tagged_targets()

    claimed = store.claim_pending(req.approval_id)
    if claimed is None:
        # The pre-check above said pending, but the transactional claim
        # lost — concurrent /execute, or a coordinator-side state change
        # between our reads. Either way, refuse to roll back.
        raise HTTPException(
            status_code=403,
            detail="approval already used or revoked",
        )

    log.info(
        "execute: id=%s rev=%s caller=%s",
        req.approval_id, approval.target_revision, caller,
    )
    operation_name = _apply_traffic(approval.target_revision)
    return {
        "approval_id": req.approval_id,
        "target_revision": approval.target_revision,
        "status": "executed",
        "operation_name": operation_name,
    }
