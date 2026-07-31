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
from concurrent import futures
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from google.cloud import run_v2
from google.protobuf.field_mask_pb2 import FieldMask
from pydantic import BaseModel, ConfigDict, Field

from driftscribe_lib.approvals import (
    PHASE_APPLIED,
    PHASE_APPLYING,
    PHASE_FAILED,
    PHASE_OUTCOME_UNKNOWN,
    TERMINAL_ROLLBACK_PHASES,
    ApprovalStore,
    compute_token_hmac,
)
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging

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

# How long /execute blocks on the traffic-shift LRO before recording
# outcome_unknown and handing back a 504.
#
# 60s, NOT the Cloud Run request ceiling. Observed traffic-shift LROs take
# 10-30s, so this covers the overwhelming majority while leaving room for the
# constraint that actually binds: the operator reaches the approval POST through
# Cloudflare, whose proxied-response budget is ~100s (agent/worker_client.py
# sizes _DESCRIBE_HTTPX_TIMEOUT against the same ceiling and states outright
# that "90s read + overhead fits, 120 would not"). This service's own Cloud Run
# deadline is the 300s default and is not the limiting factor.
#
# COST, stated rather than discovered: this worker runs --concurrency=1
# --max-instances=1, so a blocking /execute serializes it. /propose and an
# unrelated /deny queue behind it for up to this long. Denying the SAME approval
# is already moot once its credential is burned. Acceptable for an
# operator-driven action at demo scale.
_LRO_TIMEOUT_S = 60.0

# Ceiling on a /reconcile poll. Short on purpose: reconcile is a best-effort
# catch-up on an already-answered request, not the operator's live path, and it
# is called from the coordinator's /decisions fan-out where latency is visible.
_RECONCILE_TIMEOUT_S = 10.0


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


def _get_operations_client():  # noqa: ANN201 — google.api_core operations client
    """Operations client for reading a previously-started traffic-shift LRO by
    name (``/reconcile`` only).

    Reached through the ServicesClient's transport rather than constructed
    standalone: the LRO belongs to the Cloud Run service endpoint, so it must be
    read through the same transport/credentials that started it. Same
    indirection-for-testability rationale as the two clients above.

    ⚠ IAM: this needs ``run.operations.get`` bound at the PROJECT level. The
    worker SA's ``roles/run.developer`` is bound to the ``payment-demo``
    *service*, and that binding cannot reach an operation — operations live at
    ``projects/{p}/locations/{l}/operations/{id}``, which is not a child of the
    service, so the role's own ``run.operations.get`` applies to nothing there.
    Verified 2026-07-28 with Cloud Asset Policy Analyzer: before the grant, the
    permission resolved to NOT GRANTED anywhere in the project while
    ``run.services.update`` resolved to the payment-demo binding.

    Created out of band as project custom role
    ``driftscribeRunOperationsReader`` — one permission, nothing else — and now
    declared in ``iac-operator/iam_rollback_operations.tf`` (ds-10m). That is a
    SEPARATE OpenTofu root from ``iac/`` on purpose: ``iac/`` is the agent's
    authoring surface, and its plan/apply identities hold no IAM permissions at
    all, so declaring this there would break every plan on refresh and would
    require granting the SA that applies agent-authored plans the ability to
    rewrite project IAM. See that directory's README. Consequence worth knowing:
    nothing plans this root automatically, so drift here surfaces when an
    operator runs ``tofu plan``, not on a schedule.

    If that binding is ever dropped, or the SA is recreated, polling degrades to
    PermissionDenied: ``/execute`` reports every rollback as ``outcome_unknown``
    (the traffic shift still lands) and ``/reconcile`` can never settle it. That
    failure is silent from the operator's seat, so grep the worker logs for
    ``PermissionDenied`` in the ``execute: poll raised`` / ``reconcile: could
    not read operation`` lines before suspecting anything else."""
    return _get_services_client().transport.operations_client


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
        if ts.percent != 100:
            continue
        # A LATEST traffic target reports an EMPTY ``revision``: the resolved
        # name lives only in ``latest_ready_revision``. Requiring ts.revision to
        # be truthy therefore left ``active`` as "" for ANY service serving
        # LATEST — which is payment-demo's normal state — and silently disabled
        # two things:
        #
        #   1. The Layer-2 guard in /propose ("target_revision is the currently
        #      active revision"). ``req.target_revision == ""`` is never true, so
        #      the check could not fire and the worker would mint an approval to
        #      roll back onto the revision already serving — the no-op
        #      masquerading as work that the guard exists to refuse.
        #   2. ds-uwc's change snapshot, whose source is this value: with no
        #      source revision it returns None and the approval page renders
        #      "could not be recorded" on every proposal.
        #
        # Found live 2026-07-29: a real /propose logged ``active=`` empty
        # against payment-demo serving {latestRevision: true}.
        active = ts.revision or svc.latest_ready_revision.rsplit("/", 1)[-1]
        break

    revisions: list[str] = []
    rclient = _get_revisions_client()
    for rev in rclient.list_revisions(parent=_service_name()):
        # rev.name is the full path; the operator-facing name is the basename.
        revisions.append(rev.name.rsplit("/", 1)[-1])

    return revisions, active


# --------------------------------------------------------------------------- #
# ds-uwc — what will this rollback actually change?
#
# A rollback reverts the target revision's ENTIRE env, and until now nothing
# anywhere read that revision's config. The worker validated only that the
# target exists and is not active; the coordinator's validator reasons about the
# CURRENT env. So the operator approved a revision NAME and nothing else, while
# the change could revert a var they had deliberately set, or move the service
# onto a revision that violates the contract in its own way — drift removed,
# drift introduced, and DriftScribe would then detect its own rollback.
#
# The snapshot below is computed here, at propose time, rather than on the
# approval-page GET. Propose time binds the comparison to the state the proposal
# was actually made against, works for an already-resolved approval (a GET-time
# recompute would silently rewrite history against today's revision), adds no
# per-view amplification to an anonymously reachable page, and needs no extra
# API call: ``list_revisions`` already returns full Revision protos.
#
# NO OBSERVED ENV VALUE IS STORED. Not a redaction pass — a design constraint.
# The approval page is reachable by anyone holding the link, and a name-based
# heuristic (`is_secret_name`) cannot see a credential sitting under an
# innocuous name. "This var is declared in the contract" also does NOT make its
# CURRENT value public: the contract publishing ``PAYMENT_MODE=mock`` says
# nothing about a bad deploy having set ``PAYMENT_MODE=sk_live_...``. So the
# snapshot carries names, booleans and tags only, and the page renders contract
# values (which really are public — they are in the repo) as the reference.
# --------------------------------------------------------------------------- #

#: Per-var state on one revision. ``("plain", value)`` for a literal env value,
#: ``("secret", "<secret>/<version>")`` for a Secret-Manager-backed one, and
#: absent from the mapping when the var is not set at all.
#:
#: The distinction is load-bearing. ``driftscribe_lib.cloud_run``'s extractor
#: SKIPS ``value_source`` entries, which is right for "what is the live value"
#: but wrong here: two revisions pointing the same var at DIFFERENT secrets
#: would both read as absent and compare equal, reporting "no change" for a
#: change. Comparing the reference (never displaying it) keeps that honest.
def _revision_env_states(containers) -> dict[str, tuple[str, str]]:
    """Tagged env state for one revision's containers.

    Last-one-wins across containers, mirroring
    ``_extract_env_from_containers``. The target service is single-container;
    a multi-container service would need a per-container key here, and the
    flattening would otherwise let one container's plain value mask another's
    secret reference.
    """
    states: dict[str, tuple[str, str]] = {}
    for container in containers:
        for ev in container.env:
            source = getattr(ev, "value_source", None)
            if source:
                ref = getattr(source, "secret_key_ref", None)
                secret = getattr(ref, "secret", "") if ref is not None else ""
                version = getattr(ref, "version", "") if ref is not None else ""
                states[ev.name] = ("secret", f"{secret}/{version}")
            else:
                states[ev.name] = ("plain", ev.value or "")
    return states


def _env_change_snapshot(
    *,
    source_revision: str,
    target_revision: str,
    contract_env: dict[str, str] | None,
    contract_hash: str | None,
) -> dict[str, Any] | None:
    """Compare the ACTIVE revision's env to the target's. Value-free.

    Returns ``None`` — never a partial or empty dict — when either revision
    cannot be read. The caller stores ``None`` and the approval page renders
    "we could not read this", because an empty change set is indistinguishable
    from "nothing will change" and that is the one wrong answer this feature
    must never give.

    ``contract_env`` maps a declared var name to its expected value. It arrives
    from the coordinator, which owns the contract; the worker has none. It is
    used ONLY to compute booleans — the worker never echoes a value back.
    """
    if not source_revision or not target_revision:
        return None
    try:
        # Client construction is INSIDE the guard on purpose: building the
        # transport can fail on its own (credentials, quota, a cold metadata
        # server), and a preview that takes down the proposal it describes is
        # worse than no preview at all.
        rclient = _get_revisions_client()
        base = f"{_service_name()}/revisions"
        source = _revision_env_states(
            rclient.get_revision(name=f"{base}/{source_revision}").containers
        )
        target = _revision_env_states(
            rclient.get_revision(name=f"{base}/{target_revision}").containers
        )
    except Exception as e:  # noqa: BLE001 — never break a propose over a preview
        log.warning("env snapshot unavailable: %s", type(e).__name__)
        return None

    changed = sorted(
        name
        for name in set(source) | set(target)
        if source.get(name) != target.get(name)
    )

    contract_vars: dict[str, dict[str, bool]] = {}
    for name, expected in (contract_env or {}).items():
        contract_vars[name] = {
            "changed": source.get(name) != target.get(name),
            # A secret-backed or absent target can never equal the contract's
            # literal, so this is False for both — correctly: neither is
            # observably the declared value.
            "target_matches_contract": target.get(name) == ("plain", expected),
        }

    return {
        "source_revision": source_revision,
        "target_revision": target_revision,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "contract_hash": contract_hash or "",
        # Every var whose state differs between the two revisions, by NAME.
        "changed_names": changed,
        "contract_vars": contract_vars,
    }


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


def _start_traffic_update(target_revision: str):  # noqa: ANN201 — google.api_core Operation
    """Start a traffic update sending 100% to ``target_revision``.

    Returns the long-running operation WITHOUT waiting on it. ``/execute``
    persists the operation name before blocking on the result, so that a
    container death mid-wait still leaves a durable handle to reconcile
    against instead of an approval stuck "in flight" forever.

    (This function used to return only the operation name and never block at
    all, with a comment claiming "the coordinator already polls". Nothing ever
    polled — ``operation_name`` had no consumer anywhere in the repo — so a
    rollback that failed after the approval was claimed left a ``used`` doc
    indistinguishable from a successful one. See ds-2mc.)

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
    # Returns the LRO WITHOUT waiting. The wait is the caller's job precisely so
    # the operation handle can be persisted first — see /execute.
    return sclient.update_service(
        service=svc,
        update_mask=FieldMask(paths=["traffic"]),
    )


def _is_established_failure(op: Any) -> bool:
    """True only when the operation itself is demonstrably ``done`` with a
    nonzero error code.

    ``Operation.result()`` raising is NOT proof the operation failed. The SDK
    fails the whole future when a POLLING RPC errors and its retries are
    exhausted ("If a polling RPC throws an error and retrying it fails, the
    whole future fails with the corresponding exception" —
    google/api_core/future/polling.py), which can happen while the operation is
    still running and goes on to succeed. Failure is established only the way
    google.api_core.operation itself establishes it: ``done`` plus an ``error``
    field.

    Concretely: ``operations.get`` starts returning PermissionDenied after an
    IAM change, ``result()`` raises, and the traffic shift applies anyway. A
    worker that called that "failed" would tell the operator their rollback did
    not happen when it did — the same over-claim as the original seal bug, just
    pointing the other way.
    """
    raw = getattr(op, "operation", None)
    if raw is None or not getattr(raw, "done", False):
        return False
    err = getattr(raw, "error", None)
    return bool(err is not None and getattr(err, "code", 0))


def _operation_name(op: Any) -> str:
    """LRO name off a google.api_core Operation. Defensive fallback to ``""``
    if the SDK shape ever changes — a missing name costs us reconcilability,
    so it must not also cost us the apply."""
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
    - ``reason`` is capped at 2000 chars. The approval page renders this
      verbatim to the operator; bounding it keeps the Firestore doc
      cheap and the UI legible.

      Raised from 500 (ds-j0i, 2026-07-31). 500 was too tight for real model
      output and took autonomous self-heal down on prod: a 581-char rationale
      hit ``string_too_long``, this endpoint returned 422, and no approval was
      ever minted. The coordinator now clamps to the SAME number
      (``agent.renderer.ROLLBACK_REASON_MAX_CHARS``) before sending, so this
      bound is the trust boundary rather than the thing ordinary traffic
      collides with. 2000 is still trivial for Firestore and still reviewable
      on the approval page.

      ⚠️ DEPLOY ORDER: this worker must ship BEFORE a coordinator that sends
      more than 500 chars — same ``extra="forbid"`` asymmetry the ds-uwc
      fields document below.
    """

    target_revision: str = Field(min_length=1, max_length=64, pattern=_REVISION_NAME.pattern)
    reason: str = Field(min_length=1, max_length=2000)
    # ds-uwc. The coordinator owns the ops contract; this worker has none. These
    # let it compute the "does the TARGET revision satisfy the contract" booleans
    # the approval page needs, without ever being handed — or returning — an
    # observed env value. The values here are the contract's own literals, which
    # are public (they live in demo/ops-contract.yaml in a public repo).
    #
    # BOTH ARE OPTIONAL, and that is a deploy-order requirement rather than
    # politeness: ``extra="forbid"`` means a NEW coordinator sending these to an
    # OLD worker gets a 422 and every rollback proposal fails. So the worker
    # MUST deploy first, and an old coordinator that sends neither must keep
    # working — it does, and the approval page renders the unknown-state note.
    contract_env: dict[str, str] | None = Field(default=None, max_length=64)
    contract_hash: str | None = Field(default=None, max_length=64)

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

# Phase 15.2: per-request trace id propagation (see driftscribe_lib.logging).
install_trace_middleware(app)


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

    # ds-uwc: what this rollback would change, observed now, against the
    # revision that is serving now. Never raises — a preview must not be able to
    # break the proposal it describes — and returns None rather than a partial
    # answer, which the page renders as "could not read" instead of "no changes".
    env_snapshot = _env_change_snapshot(
        source_revision=active,
        target_revision=req.target_revision,
        contract_env=req.contract_env,
        contract_hash=req.contract_hash,
    )

    store = _get_approval_store()
    approval, raw_token = store.create(
        target_revision=req.target_revision,
        reason=req.reason,
        hmac_key=APPROVAL_HMAC_KEY,
        created_by=caller,
        env_snapshot=env_snapshot,
    )
    log.info(
        "propose: id=%s rev=%s active=%s caller=%s",
        approval.approval_id, req.target_revision, active, caller,
    )
    return {
        "approval_id": approval.approval_id,
        "approval_token": raw_token,
        # Phase 11.7: the raw token rides as a query param so the
        # coordinator's approval page can pre-populate the hidden form
        # field without making the operator paste anything. Defense in
        # depth that justifies a credential-in-URL:
        #   - 15-min TTL on the approval doc
        #   - single-use HMAC binding (revision-bound, see
        #     compute_token_hmac docstring)
        #   - the approval page sends Referrer-Policy: no-referrer +
        #     Cache-Control: no-store + X-Frame-Options: DENY (Phase
        #     11.7), so a same-tab navigation cannot exfiltrate the
        #     token via Referer and the page is not cached by any
        #     proxy that respects the directive
        # Caveat: query strings often appear in Cloud Run / LB request
        # logs. The 15-min TTL plus single-use HMAC means a log-leak
        # within the window still requires the attacker to act before
        # the legitimate operator does (or after, but on an already-used
        # approval, /execute returns 403).
        "approval_url": (
            f"{COORDINATOR_URL}/approvals/{approval.approval_id}?t={raw_token}"
        ),
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
        req.approval_token,
        approval.approval_id,
        approval.target_revision,
        APPROVAL_HMAC_KEY,
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

    # ---- Outcome recording (ds-2mc) ---------------------------------------
    # The claim above burned the credential; it did NOT roll anything back.
    # Everything below establishes WHICH outcome this approval reached, because
    # `status == "used"` alone cannot distinguish success from failure, and the
    # operator desk seals on that distinction.
    #
    # The phase written on each path is chosen by what we can honestly assert:
    #   - our own pre-mutation refusal  -> failed          (nothing was sent)
    #   - anything else around the call -> outcome_unknown (may have landed)
    # A lost response on an accepted mutation is indistinguishable from a
    # rejected one from here, so the ambiguous paths must NOT claim failure.
    try:
        op = _start_traffic_update(approval.target_revision)
    except HTTPException as e:
        # The defense-in-depth tag re-check inside _start_traffic_update, which
        # runs BEFORE update_service. Nothing reached Google: definitely failed.
        #
        # Gated on 409 rather than catching every HTTPException (ds-mml). The tag
        # check is the only thing in there that raises TODAY, so the hardcoded
        # reason is accurate — but a future pre-mutation guard raising 4xx would
        # inherit a TERMINAL `failed` stamped with a fabricated cause, and
        # `failed` is one of the two phases record_phase refuses to overwrite.
        # A wrong terminal outcome is the single worst thing this endpoint can
        # write, so the branch is narrowed to the case it actually describes.
        if e.status_code == 409:
            store.record_phase(
                req.approval_id, phase=PHASE_FAILED,
                detail={"stage": "preflight", "reason": "tagged_traffic_target"},
            )
        else:
            # An unrecognized pre-mutation refusal. It is still pre-mutation, so
            # nothing landed — but we cannot name WHY without guessing, and
            # `failed` here would be a terminal claim built on a guess.
            log.exception(
                "execute: unrecognized pre-mutation refusal id=%s", req.approval_id
            )
            store.record_phase(
                req.approval_id, phase=PHASE_OUTCOME_UNKNOWN,
                detail={"stage": "preflight", "status_code": e.status_code},
            )
        raise
    except Exception as e:  # noqa: BLE001 — must not narrow; see below
        # Could be a refused request OR a lost response on a mutation the
        # server accepted. We cannot tell, so we do not guess.
        log.exception("execute: traffic update failed to start id=%s", req.approval_id)
        store.record_phase(
            req.approval_id, phase=PHASE_OUTCOME_UNKNOWN,
            detail={"stage": "start", "error": type(e).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail="traffic update could not be started; outcome unconfirmed",
        ) from e

    operation_name = _operation_name(op)
    # Durable handle BEFORE the wait — this is what makes a mid-wait crash
    # reconcilable rather than permanently ambiguous.
    store.record_phase(
        req.approval_id, phase=PHASE_APPLYING,
        detail={"operation_name": operation_name},
    )

    try:
        op.result(timeout=_LRO_TIMEOUT_S)
    except futures.TimeoutError as e:
        # Polling expired. The operation is NOT cancelled and may well succeed
        # a moment from now, so this is emphatically not a failure. It is the
        # exception type google.api_core raises for polling expiry (it converts
        # RetryError -> concurrent.futures.TimeoutError); catching some
        # invented LRO-specific type here would silently fall through to the
        # failure branch below in production while tests passed.
        log.warning(
            "execute: LRO still running after %ss id=%s op=%s",
            _LRO_TIMEOUT_S, req.approval_id, operation_name,
        )
        store.record_phase(
            req.approval_id, phase=PHASE_OUTCOME_UNKNOWN,
            detail={"stage": "poll", "reason": "timeout", "operation_name": operation_name},
        )
        raise HTTPException(
            status_code=504,
            detail="rollback still in progress; outcome unconfirmed",
        ) from e
    except Exception as e:  # noqa: BLE001
        # result() raised — but that alone does not say the ROLLBACK failed, only
        # that we stopped being able to watch it. Distinguish an operation that
        # actually reported an error from a polling RPC that broke underneath us
        # (see _is_established_failure); the latter may still be applying.
        established = _is_established_failure(op)
        phase = PHASE_FAILED if established else PHASE_OUTCOME_UNKNOWN
        log.exception(
            "execute: poll raised id=%s op=%s established_failure=%s",
            req.approval_id, operation_name, established,
        )
        store.record_phase(
            req.approval_id, phase=phase,
            detail={"stage": "poll", "error": type(e).__name__, "operation_name": operation_name},
        )
        raise HTTPException(
            status_code=502,
            detail="rollback failed" if established else "rollback outcome unconfirmed",
        ) from e

    # Confirmed. This is the only path that may stamp resolved_at, and so the
    # only one that can produce a success seal on the operator desk.
    store.record_phase(
        req.approval_id, phase=PHASE_APPLIED,
        detail={"operation_name": operation_name},
        resolved_at=dt.datetime.now(dt.timezone.utc),
    )
    return {
        "approval_id": req.approval_id,
        "target_revision": approval.target_revision,
        "status": "executed",  # now literally true — the LRO resolved above
        "operation_name": operation_name,
    }


@app.post("/deny")
def deny(
    req: ExecuteRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Verify the approval token and transactionally flip status to denied.

    Phase 11.9 fix (Codex review of 11.7, critical finding #1): the
    coordinator's reject path used to call ``ApprovalStore.claim_denied``
    directly without validating the approval token. That meant anyone
    holding only the ``approval_id`` could deny a pending rollback — a
    HITL availability bug (operator can be locked out of the rollback
    they intended to approve).

    The fix mirrors ``/execute`` exactly. The rollback worker is the only
    service holding ``APPROVAL_HMAC_KEY``, so it is the only service that
    can verify the operator's intent on the deny path too. Splitting the
    deny authority this way preserves the "compromised coordinator can
    refuse executions but cannot mint them" property AND adds "cannot
    silently deny them" — the operator's token is now required for BOTH
    decision paths.

    Verification order mirrors ``/execute``:

    1. Look up the doc (404 if missing).
    2. Status pre-check (403 if not pending) — replay defense.
    3. Expiry check (403 if past TTL).
    4. Constant-time HMAC compare against the stored ``token_hmac``.
    5. Transactional ``pending → denied`` flip. Race-safe.

    Unlike ``/execute``, the deny path does NOT touch Cloud Run admin or
    mutate traffic. The Firestore status flip is the entire side effect.

    Status codes:

    - **200**: approval transactionally moved to denied. Body:
      ``{approval_id, status: "denied"}``.
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
        req.approval_token,
        approval.approval_id,
        approval.target_revision,
        APPROVAL_HMAC_KEY,
    )
    if not hmac_mod.compare_digest(expected_hmac, approval.token_hmac):
        log.warning(
            "deny: HMAC mismatch id=%s caller=%s",
            req.approval_id, caller,
        )
        raise HTTPException(status_code=403, detail="invalid approval token")

    claimed = store.claim_denied(req.approval_id)
    if claimed is None:
        # Pre-check said pending; transactional claim lost — concurrent
        # /deny or /execute, or a coordinator-side state change. Either
        # way, refuse.
        raise HTTPException(
            status_code=403,
            detail="approval already used or revoked",
        )

    log.info(
        "deny: id=%s rev=%s caller=%s",
        req.approval_id, approval.target_revision, caller,
    )
    return {
        "approval_id": req.approval_id,
        "status": "denied",
    }


class ReconcileRequest(BaseModel):
    """Closed schema for ``/reconcile``. Only an approval_id — this endpoint
    resolves an outcome that is already recorded as unresolved; it can never
    initiate one, so it needs no approval token."""

    approval_id: str = Field(min_length=36, max_length=36, pattern=_UUID_SHAPE.pattern)

    model_config = ConfigDict(extra="forbid")


@app.post("/reconcile")
def reconcile(
    req: ReconcileRequest,
    caller: str = Depends(_verify_caller_dep),
) -> dict:
    """Resolve an approval left in ``applying`` / ``outcome_unknown``.

    This is what keeps ``outcome_unknown`` a temporary state instead of a
    permanent shrug. ``/execute`` blocks for up to ``_LRO_TIMEOUT_S`` and
    terminalizes the doc itself on every path it survives; this endpoint covers
    the residue where it could not:

    - the LRO ran past the 60s poll budget (the operation kept going),
    - the container died between persisting ``applying`` and recording a result.

    Deliberately NOT token-gated, unlike ``/execute`` and ``/deny``. It mints
    nothing, mutates no Cloud Run state, and cannot move an approval out of
    ``pending`` — it only reads a long-running operation this worker already
    started and writes down how it ended. The caller allowlist
    (``verify_caller``) remains the access control. Giving it the single-use
    token would be actively worse: the token is burned by then, so the
    coordinator could not present one anyway.

    Idempotent and safe to call on anything: a terminal doc returns its recorded
    phase untouched (``record_phase`` refuses to overwrite terminal phases, so
    even a racing ``/execute`` cannot be clobbered by a slow reconcile).
    """
    store = _get_approval_store()

    approval = store.get(req.approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")

    audit = approval.apply_audit or {}
    phase = audit.get("phase")
    if phase in TERMINAL_ROLLBACK_PHASES:
        return {"approval_id": req.approval_id, "phase": phase, "reconciled": False}

    operation_name = (audit.get("detail") or {}).get("operation_name")
    if not operation_name:
        # Nothing was ever started (still `claimed`), or the SDK gave us no
        # handle. Either way there is no operation to ask about — leave the doc
        # exactly as it is rather than inventing an outcome for it.
        return {"approval_id": req.approval_id, "phase": phase, "reconciled": False}

    try:
        op = _get_operations_client().get_operation(
            name=operation_name, timeout=_RECONCILE_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001 — reconcile is best-effort by design
        log.warning(
            "reconcile: could not read operation id=%s op=%s err=%s",
            req.approval_id, operation_name, type(e).__name__,
        )
        return {"approval_id": req.approval_id, "phase": phase, "reconciled": False}

    if not getattr(op, "done", False):
        # Still running. Honest answer is still "unknown".
        return {"approval_id": req.approval_id, "phase": phase, "reconciled": False}

    if getattr(op, "error", None) and getattr(op.error, "code", 0):
        store.record_phase(
            req.approval_id, phase=PHASE_FAILED,
            detail={"stage": "reconcile", "operation_name": operation_name,
                    "error_code": op.error.code},
        )
        log.info("reconcile: id=%s -> failed", req.approval_id)
        return {"approval_id": req.approval_id, "phase": PHASE_FAILED, "reconciled": True}

    # `done` with neither response nor error is malformed — not evidence of
    # success. Refuse to promote it rather than read "not an error" as "applied".
    if hasattr(op, "HasField") and not op.HasField("response"):
        log.warning("reconcile: done operation has no response id=%s", req.approval_id)
        return {"approval_id": req.approval_id, "phase": phase, "reconciled": False}

    # Promote the phase WITHOUT stamping resolved_at.
    #
    # The operation completed at some unknown earlier moment — possibly hours
    # ago, if this is a container-death recovery. Stamping `now` would hand the
    # desk a fresh timestamp it renders BOTH as "Applied {time}" and as the
    # 10-minute seal-freshness window, so a 4-hour-old rollback would pop a
    # brand-new 判子 reading the wrong time. The outcome would be right and the
    # story around it fabricated. The IaC lane already refuses this exact move
    # (its reconcile carries the original applied_at forward rather than
    # re-stamping); this matches it.
    #
    # Consequence, accepted deliberately: a rollback confirmed only by reconcile
    # never seals. It lands as `applied` in the ledger with no fresh receipt,
    # which is the honest rendering of "this succeeded, we found out later".
    store.record_phase(
        req.approval_id, phase=PHASE_APPLIED,
        detail={"stage": "reconcile", "operation_name": operation_name},
    )
    log.info("reconcile: id=%s -> applied (no seal; completion time unknown)", req.approval_id)
    return {"approval_id": req.approval_id, "phase": PHASE_APPLIED, "reconciled": True}
