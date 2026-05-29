"""tofu-apply Agent — the SOLE MUTATOR of DriftScribe-managed infra (Phase C4).

The only worker that runs ``tofu apply``. It owns the plan-bound HMAC key and
runs ``/propose`` (verify-artifact-then-create) + ``/apply`` (claim-then-apply
the saved binary plan) + ``/deny`` — the exact trust split as ``workers/rollback``
(the worker holds the key; the coordinator can request a proposal + render the
page but CANNOT mint a valid approval). It consumes the C3 schema
(``driftscribe_lib.approvals``) and the C2 artifacts.

Safety layers:

- **Layer 1 (IAM):** ``tofu-apply-sa`` holds the broad apply role but — per the
  hardened-broad default (no ``*.setIamPolicy``, no project-wide ``actAs``, no
  SA/HMAC-key creation) — cannot escalate IAM or impersonate other SAs. The gate
  below constrains what applies THROUGH the approval flow; in-worker compromise
  is un-gatable by design (§9) and contained by minimization (this tiny surface,
  private ingress, no shell/provisioner in the image).
- **Layer 2 (payload policy):** closed request schemas (``extra="forbid"``);
  the apply request carries only ``approval_id`` + token (no artifact fields —
  the worker resolves everything from the signed record).
- **Layer 3 (inter-service auth):** :func:`driftscribe_lib.auth.verify_caller`.
- **Layer 4 (the C3 HMAC gate, §3.6 claim-first):** verify → signed_payload →
  expiry (signed window) → approver → single-use claim (burn) → re-fetch +
  re-verify integrity → denylist re-run → fidelity gate → freshness gate →
  saved-plan apply. Every apply-time decision reads from ``signed_payload`` (the
  HMAC-bound dict), NEVER the denormalized record fields.

Design: docs/plans/2026-05-29-infra-iac-phase-c4-tofu-apply.md
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from driftscribe_lib.approvals import (
    ArtifactIntegrityError,
    PlanApprovalStore,
    build_plan_approval_payload,
    canonicalize_payload,
    plan_approval_is_expired,
    signed_payload,
    verify_plan_approval,
)
from driftscribe_lib.auth import verify_caller
from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate, load_plan_json
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging
from workers.tofu_apply import gcs_fetch, tofu_runner

log = setup_logging("tofu-apply-agent")

# Boot-time env. Hard-required values KeyError at import so a misconfigured Cloud
# Run revision fails to start (clear "Revision is not ready" over a runtime 500).
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
COORDINATOR_URL = os.environ["COORDINATOR_URL"].rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
# Distinct from rollback's APPROVAL_HMAC_KEY — the C3 plan-approval HMAC is
# domain-separated and lives in its own secret (plan-hmac-key).
PLAN_APPROVAL_HMAC_KEY = os.environ["PLAN_APPROVAL_HMAC_KEY"]
# A key resource PATH, not key material — the decrypt authority is the SA's KMS
# IAM binding. Injected as TF_VAR_tofu_state_kms_key on every tofu subprocess.
TOFU_STATE_KMS_KEY = os.environ["TF_VAR_tofu_state_kms_key"]
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", gcs_fetch.ARTIFACT_BUCKET)
IAC_DIR = Path(os.environ.get("IAC_DIR", "/app/iac"))

# The tofu subprocess seam — tests monkeypatch this module attribute so the full
# decision matrix runs with no live tofu (design §10).
_RUN_TOFU: tofu_runner.RunTofu = tofu_runner._default_run_tofu


# --------------------------------------------------------------------------- #
# Indirections (testability — mirror rollback / infra_reader)
# --------------------------------------------------------------------------- #


def _verify_caller_dep(request: Request) -> str:
    return verify_caller(request, own_url=OWN_URL, allowed_callers=ALLOWED_CALLERS)


def _get_plan_approval_store() -> PlanApprovalStore:
    return PlanApprovalStore(project=GCP_PROJECT)


def _get_artifact_bucket() -> Any:
    """The google-cloud-storage Bucket for the artifact bucket. Deferred import so
    unit tests (which monkeypatch this) need no google-cloud-storage install."""
    from google.cloud import storage  # type: ignore

    client = storage.Client(project=GCP_PROJECT)
    return client.bucket(ARTIFACT_BUCKET)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _baked_lockfile_sha256() -> str:
    return hashlib.sha256((IAC_DIR / ".terraform.lock.hcl").read_bytes()).hexdigest()


def _baked_tofu_version() -> str:
    """The baked binary's version via ``tofu version -json`` (.terraform_version)."""
    rc, out, err = _RUN_TOFU(["version", "-json"], str(IAC_DIR), dict(os.environ))
    if rc != 0:
        raise tofu_runner.TofuStepError("version", rc, err)
    return str(json.loads(out).get("terraform_version", ""))


# --------------------------------------------------------------------------- #
# Request schemas (closed)
# --------------------------------------------------------------------------- #

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class ProposeRequest(BaseModel):
    """Coordinator → /propose. ``approver`` is the authenticated operator subject
    (asserted by the coordinator until C5 forwards a trusted operator identity —
    see the plan §6 residual gap)."""

    artifact_uri_metadata: str = Field(min_length=1, max_length=512)
    generation_metadata: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    approver: str = Field(min_length=1, max_length=320)
    model_config = ConfigDict(extra="forbid")


class TokenRequest(BaseModel):
    """Closed schema for /apply + /deny — id + raw token only, no artifact fields."""

    approval_id: str = Field(min_length=36, max_length=36, pattern=_UUID)
    approval_token: str = Field(min_length=43, max_length=64)
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Shared verification helpers
# --------------------------------------------------------------------------- #


def _fetch_and_verify_artifacts(bucket: Any, signed_md: dict, *, signed_meta_uri: str,
                                signed_meta_gen: str) -> tuple[dict, bytes]:
    """Fetch metadata@gen + plan.tfplan@gen + plan.json@gen (all pinned), run the
    lib integrity recompute, and return ``(parsed_plan_json, plan_tfplan_bytes)``.

    Raises ``GcsFetchError`` / ``ArtifactIntegrityError`` / ``ValueError`` on any
    failure — the caller fails closed. The metadata is re-fetched here ONLY in
    /propose; /apply passes the already-trusted signed metadata + re-fetches for
    the digest comparison separately (contract #1)."""
    md = signed_md
    plan_bytes = gcs_fetch.fetch_artifact(
        bucket, signed_uri=md["artifact_uri_plan"],
        generation=md["generation_plan"], expected_basename="plan.tfplan",
    )
    json_bytes = gcs_fetch.fetch_artifact(
        bucket, signed_uri=md["artifact_uri_json"],
        generation=md["generation_json"], expected_basename="plan.json",
    )
    from driftscribe_lib.approvals import verify_artifact_integrity

    verify_artifact_integrity(
        plan_tfplan_bytes=plan_bytes,
        plan_json_bytes=json_bytes,
        expected_plan_sha256=md["plan_sha256"],
        expected_plan_json_sha256=md["plan_json_sha256"],
    )
    parsed, parse_v = load_plan_json(json_bytes.decode("utf-8"))
    if parse_v is not None:
        raise ValueError(f"plan.json unparseable: {parse_v.detail}")
    return parsed, plan_bytes


def _denylist_or_raise(parsed_plan_json: dict) -> None:
    try:
        violations = evaluate(DenylistInput(plan=parsed_plan_json))
    except Exception as e:  # noqa: BLE001 — lib is fail-closed on policy only; any bug → deny
        raise ValueError(f"denylist evaluation error (deny): {e}")
    if violations:
        raise ValueError(
            "denylist violations: " + "; ".join(f"[{v.rule}] {v.detail}" for v in violations)
        )


def _fidelity_or_raise(signed_md: dict, parsed_plan_json: dict) -> None:
    tofu_runner.assert_fidelity(
        signed_metadata=signed_md,
        baked_tofu_version=_baked_tofu_version(),
        baked_lockfile_sha256=_baked_lockfile_sha256(),
        plan_json=parsed_plan_json,
        declared_addresses=tofu_runner.extract_declared_addresses(IAC_DIR),
    )


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="DriftScribe tofu-apply Agent")
install_trace_middleware(app)


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/propose")
def propose(req: ProposeRequest, caller: str = Depends(_verify_caller_dep)) -> dict:
    """Independently verify the artifact, then mint a pending plan approval.

    The worker fetches the metadata at the operator-supplied locator, re-fetches
    the plan/json at the metadata's generations, recomputes integrity, re-runs
    the denylist, and runs the fidelity + resource-set guard (so it never mints a
    dead approval), THEN signs the c3.v1 payload and stores it. Returns the raw
    token ONCE."""
    bucket = _get_artifact_bucket()
    try:
        meta_bytes = gcs_fetch.fetch_artifact(
            bucket, signed_uri=req.artifact_uri_metadata,
            generation=req.generation_metadata, expected_basename="metadata.json",
        )
        fetched_md = json.loads(meta_bytes)
        if not isinstance(fetched_md, dict):
            raise ValueError(f"metadata.json root is {type(fetched_md).__name__}, expected object")
        parsed_plan_json, _plan_bytes = _fetch_and_verify_artifacts(
            bucket, fetched_md, signed_meta_uri=req.artifact_uri_metadata,
            signed_meta_gen=req.generation_metadata,
        )
        _denylist_or_raise(parsed_plan_json)
        _fidelity_or_raise(fetched_md, parsed_plan_json)
    except gcs_fetch.GcsFetchError as e:
        raise HTTPException(status_code=422, detail=f"artifact fetch failed: {e}")
    except ArtifactIntegrityError as e:
        raise HTTPException(status_code=422, detail=f"artifact integrity: {e}")
    except tofu_runner.FidelityError as e:
        raise HTTPException(status_code=422, detail=f"fidelity: {e}")
    except tofu_runner.TofuStepError as e:
        # The fidelity check probes `tofu version`; a probe failure → fail closed.
        raise HTTPException(status_code=502, detail=f"tofu probe failed: {e}")
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as e:
        # TypeError covers indexing a non-dict fetched artifact — clean 422 refusal,
        # never a 500 (untrusted artifacts must fail closed cleanly).
        raise HTTPException(status_code=422, detail=f"propose rejected: {e}")

    issued_at, expires_at = _approval_window()
    try:
        payload = build_plan_approval_payload(
            metadata=fetched_md,
            artifact_uri_metadata=req.artifact_uri_metadata,
            generation_metadata=req.generation_metadata,
            approver=req.approver,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"payload build rejected: {e}")

    store = _get_plan_approval_store()
    record, raw_token = store.create(
        payload=payload, hmac_key=PLAN_APPROVAL_HMAC_KEY, created_by=caller
    )
    log.info("propose: id=%s approver=%s caller=%s", record.approval_id, req.approver, caller)
    return {
        "approval_id": record.approval_id,
        "approval_token": raw_token,
        "expires_at": expires_at,
    }


def _approval_window() -> tuple[str, str]:
    from driftscribe_lib.approvals import new_approval_window

    return new_approval_window(now=_now())


@app.post("/apply")
def apply(req: TokenRequest, caller: str = Depends(_verify_caller_dep)) -> dict:
    """Verify → claim (single-use burn) → re-verify → denylist → fidelity →
    freshness → saved-plan apply. The §3.6 claim-first order: every decision
    reads from ``signed_payload`` once the HMAC has verified the bytes."""
    store = _get_plan_approval_store()
    stored = store.get(req.approval_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if stored.status != "pending":
        raise HTTPException(status_code=403, detail=f"approval status is {stored.status!r}, not 'pending'")
    if not verify_plan_approval(req.approval_token, stored, PLAN_APPROVAL_HMAC_KEY):
        log.warning("apply: HMAC mismatch id=%s caller=%s", req.approval_id, caller)
        raise HTTPException(status_code=403, detail="invalid approval token")

    sp = signed_payload(stored)  # trusted dict — every read below uses THIS
    if plan_approval_is_expired(stored, now=_now()):
        raise HTTPException(status_code=403, detail="approval expired")
    if caller != sp["approver"]:
        log.warning("apply: actor %s != signed approver %s id=%s", caller, sp["approver"], req.approval_id)
        raise HTTPException(status_code=403, detail="actor is not the signed approver")

    attempt_id = str(uuid.uuid4())
    now = _now()
    claimed = store.claim_pending(
        req.approval_id, used_by=caller, used_at=now,
        apply_audit={"phase": "claimed", "claimed_at": now.isoformat(), "apply_attempt_id": attempt_id},
    )
    if claimed is None:
        raise HTTPException(status_code=403, detail="approval already used or revoked")

    # ---- approval BURNED; every failure below is fail-closed (re-propose) ----
    md = sp["metadata"]
    try:
        bucket = _get_artifact_bucket()
        # Contract #1: re-fetch metadata @ signed locator+gen, rebuild + compare to the signed bytes.
        meta_bytes = gcs_fetch.fetch_artifact(
            bucket, signed_uri=sp["artifact_uri_metadata"],
            generation=sp["generation_metadata"], expected_basename="metadata.json",
        )
        rebuilt = build_plan_approval_payload(
            metadata=json.loads(meta_bytes),
            artifact_uri_metadata=sp["artifact_uri_metadata"],
            generation_metadata=sp["generation_metadata"],
            approver=sp["approver"],
            issued_at=sp["issued_at"],
            expires_at=sp["expires_at"],
        )
        if canonicalize_payload(rebuilt) != stored.payload_canonical:
            raise ValueError("re-fetched metadata does not reproduce the signed payload")
        # Contract #2: re-fetch plan/json @ signed generations + integrity recompute.
        parsed_plan_json, plan_bytes = _fetch_and_verify_artifacts(
            bucket, md, signed_meta_uri=sp["artifact_uri_metadata"],
            signed_meta_gen=sp["generation_metadata"],
        )
        _denylist_or_raise(parsed_plan_json)          # contract #4
        _fidelity_or_raise(md, parsed_plan_json)       # §3.2 fidelity + resource-set guard
    except gcs_fetch.GcsFetchError as e:
        _fail(store, req.approval_id, attempt_id, "integrity_refused", 422, f"artifact fetch failed: {e}")
    except ArtifactIntegrityError as e:
        _fail(store, req.approval_id, attempt_id, "integrity_refused", 422, f"artifact integrity: {e}")
    except tofu_runner.FidelityError as e:
        _fail(store, req.approval_id, attempt_id, "fidelity_refused", 422, f"fidelity: {e}")
    except tofu_runner.TofuStepError as e:
        # The fidelity check probes `tofu version`; a probe failure pre-apply →
        # fail closed with a terminal audit (not an unhandled 500 that would
        # leave the burned approval at the outcome-unknown phase="claimed").
        _fail(store, req.approval_id, attempt_id, "failed", 502, f"tofu probe failed: {e}",
              extra={"step": e.step, "apply_exit_code": e.exit_code})
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as e:
        _fail(store, req.approval_id, attempt_id, "verify_refused", 422, f"apply rejected: {e}")

    # ---- gates passed: materialize a per-request workdir + run tofu ----
    try:
        with tempfile.TemporaryDirectory(prefix="tofu-apply-") as tmp:
            workdir = Path(tmp) / "iac"
            shutil.copytree(IAC_DIR, workdir)
            (workdir / "plan.tfplan").write_bytes(plan_bytes)
            outcome = tofu_runner.run_apply_sequence(
                workdir=str(workdir), kms_key=TOFU_STATE_KMS_KEY,
                base_env=dict(os.environ), run_tofu=_RUN_TOFU,
            )
    except tofu_runner.FreshnessDrift as e:
        _fail(store, req.approval_id, attempt_id, "drift_refused", 409,
              "refusing apply: refresh-only detected out-of-band drift", extra={"stderr_tail": e.stderr[-500:]})
    except tofu_runner.TofuStepError as e:
        _fail(store, req.approval_id, attempt_id, "failed", 502,
              f"tofu {e.step} failed (exit {e.exit_code})",
              extra={"step": e.step, "apply_exit_code": e.exit_code, "stderr_tail": e.stderr[-500:]})

    store.set_apply_audit(req.approval_id, {
        "phase": "applied", "apply_attempt_id": attempt_id,
        "freshness_exit_code": outcome.freshness_exit, "apply_exit_code": outcome.apply_exit,
        "applied_at": _now().isoformat(),
        "state_serial": outcome.state_serial, "state_lineage": outcome.state_lineage,
    })
    log.info("apply: id=%s attempt=%s APPLIED serial=%s", req.approval_id, attempt_id, outcome.state_serial)
    return {"approval_id": req.approval_id, "status": "applied", "apply_attempt_id": attempt_id}


def _fail(store: PlanApprovalStore, approval_id: str, attempt_id: str, phase: str,
          http_status: int, detail: str, *, extra: dict | None = None) -> NoReturn:
    """Write a terminal apply_audit (the approval is already burned) + raise.
    ``NoReturn`` documents the always-raises contract so a type checker narrows
    ``plan_bytes``/``outcome`` as bound after the gate try-blocks (and flags any
    future non-raising path)."""
    audit = {"phase": phase, "apply_attempt_id": attempt_id, "failed_at": _now().isoformat(), "detail": detail}
    if extra:
        audit.update(extra)
    try:
        store.set_apply_audit(approval_id, audit)
    except Exception as e:  # noqa: BLE001 — audit best-effort; never mask the refusal
        log.warning("apply: set_apply_audit failed id=%s: %s", approval_id, e)
    log.warning("apply: id=%s phase=%s detail=%s", approval_id, phase, detail)
    raise HTTPException(status_code=http_status, detail=detail)


@app.post("/deny")
def deny(req: TokenRequest, caller: str = Depends(_verify_caller_dep)) -> dict:
    """Verify the token + HMAC, then transactionally flip pending → denied.

    Mirrors rollback's hardened deny path: the token is verified BEFORE the flip
    so the coordinator can never deny an approval it doesn't hold the token for
    (the pre-11.9 availability bug stays fixed)."""
    store = _get_plan_approval_store()
    stored = store.get(req.approval_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if stored.status != "pending":
        raise HTTPException(status_code=403, detail=f"approval status is {stored.status!r}, not 'pending'")
    if not verify_plan_approval(req.approval_token, stored, PLAN_APPROVAL_HMAC_KEY):
        log.warning("deny: HMAC mismatch id=%s caller=%s", req.approval_id, caller)
        raise HTTPException(status_code=403, detail="invalid approval token")
    claimed = store.claim_denied(req.approval_id, denied_by=caller, denied_at=_now())
    if claimed is None:
        raise HTTPException(status_code=403, detail="approval already used or revoked")
    log.info("deny: id=%s caller=%s", req.approval_id, caller)
    return {"approval_id": req.approval_id, "status": "denied"}
