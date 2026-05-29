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
from driftscribe_lib.cf_access import (
    CfAccessJwtError,
    canonical_operator_email,
    verify_cf_access_jwt,
)
from driftscribe_lib.iac_plan_denylist import DenylistInput, evaluate, load_plan_json
from driftscribe_lib.logging import install_trace_middleware, setup as setup_logging
from workers.tofu_apply import gcs_fetch, tofu_runner

log = setup_logging("tofu-apply-agent")

# Boot-time env. Hard-required values KeyError at import so a misconfigured Cloud
# Run revision fails to start (clear "Revision is not ready" over a runtime 500).
GCP_PROJECT = os.environ["GCP_PROJECT"]
OWN_URL = os.environ["OWN_URL"].rstrip("/")
# Optional (I8): unused under the propose-on-approve flow — the worker never
# calls back to the coordinator. Kept readable but no longer hard-required so a
# revision boots without it (previously a placeholder kept this from KeyError-ing).
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "").rstrip("/")
ALLOWED_CALLERS = frozenset(
    e.strip() for e in os.environ["ALLOWED_CALLERS"].split(",") if e.strip()
)
# Phase C5b-2 operator-JWT re-verification. The worker INDEPENDENTLY re-verifies a
# forwarded Cloudflare-Access operator JWT against CF's JWKS and binds
# verified-email == signed approver, closing the C3/C4 tautology (caller is the
# coordinator SA; approver was free text the coordinator asserted). These name the
# CF Access Application the operator signed into; empty in unit/e2e boots.
CF_ACCESS_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
CF_ACCESS_AUD_TAG = os.environ.get("CF_ACCESS_AUD_TAG", "")
# "enforce" (default, fail-closed for prod): a valid operator JWT bound to the
# signed approver is REQUIRED. "e2e": if a JWT is present it is verified+bound
# exactly as enforce; if absent, fall back to the pre-C5 caller==approver check
# (so the e2e smoke + offline unit tests run without a real CF JWT).
IAC_OPERATOR_AUTH_MODE = os.environ.get("IAC_OPERATOR_AUTH_MODE", "enforce")

_VALID_OPERATOR_AUTH_MODES = frozenset({"enforce", "e2e"})


def _validate_operator_auth_config(mode: str, team_domain: str, aud_tag: str) -> None:
    """Fail-fast at boot on operator-auth misconfig — surface a clear "Revision is
    not ready" rather than a runtime 403 on the first apply.

    1. The mode MUST be exactly ``enforce`` or ``e2e``. A typo (e.g. ``enfroce``)
       would otherwise boot, SKIP the enforce CF-env gate below, then behave like
       enforce at request time and 403 — a silent foot-gun. Reject unknown modes.
    2. In enforce mode the worker MUST have CF Access configured, else EVERY
       operator-JWT verify 403s (a deploy that forgot the CF env vars)."""
    if mode not in _VALID_OPERATOR_AUTH_MODES:
        raise RuntimeError(
            f"IAC_OPERATOR_AUTH_MODE must be one of {sorted(_VALID_OPERATOR_AUTH_MODES)}, "
            f"got {mode!r}"
        )
    if mode == "enforce" and (not team_domain or not aud_tag):
        raise RuntimeError(
            "IAC_OPERATOR_AUTH_MODE=enforce requires CF_ACCESS_TEAM_DOMAIN and "
            "CF_ACCESS_AUD_TAG to be set"
        )


_validate_operator_auth_config(IAC_OPERATOR_AUTH_MODE, CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD_TAG)
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
    """Coordinator → /propose. ``approver`` is the authenticated operator subject;
    in enforce mode the worker re-verifies the forwarded ``operator_jwt`` and
    binds its verified email == ``approver`` BEFORE minting (C5b-2), so a
    compromised coordinator can no longer assert an arbitrary approver."""

    artifact_uri_metadata: str = Field(min_length=1, max_length=512)
    generation_metadata: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    approver: str = Field(min_length=1, max_length=320)
    # Forwarded Cf-Access-Jwt-Assertion (C5b-2). Optional in the schema so the
    # e2e-legacy path (no real CF JWT) still validates; enforce mode rejects a
    # None at the handler.
    operator_jwt: str | None = None
    model_config = ConfigDict(extra="forbid")


class TokenRequest(BaseModel):
    """Closed schema for /deny — id + raw token only, no artifact fields.

    /deny stays cleanup-only with NO operator binding (per plan §5): because
    ``extra="forbid"``, passing an ``operator_jwt`` to /deny is a 422."""

    approval_id: str = Field(min_length=36, max_length=36, pattern=_UUID)
    approval_token: str = Field(min_length=43, max_length=64)
    model_config = ConfigDict(extra="forbid")


class ApplyRequest(TokenRequest):
    """Closed schema for /apply — TokenRequest plus the forwarded operator JWT
    (C5b-2). The worker re-verifies it and binds verified email == signed approver
    PRE-CLAIM, so an absent/forged/expired JWT fails 403 with nothing burned."""

    operator_jwt: str | None = None
    # Pydantic v2 does NOT inherit model_config across subclasses — restate it so
    # /apply stays a closed schema (a stray extra field is a 422, not silently
    # dropped), matching every other worker request model.
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


def _verify_operator(
    operator_jwt: str | None, expected_approver: str, *, caller: str
) -> str | None:
    """Re-verify the forwarded CF Access operator JWT and bind its verified email
    to ``expected_approver`` (the C5b-2 hardening). Returns the verified operator
    email, or ``None`` only in the e2e-legacy fallback. Raises ``HTTPException``
    (403) on ANY failure — never leaks the verifier's exception detail or token
    bytes.

    Trust note: this is a real hardening, NOT full non-repudiation — a compromised
    coordinator could still REPLAY a currently-valid operator JWT within its TTL.
    That residual is documented + out of scope (plan §6). The bind is on the
    canonical EMAIL claim (design I4) — this assumes the CF Access policy on the
    Application enforces email ownership/uniqueness (it gates which emails can get
    a token at all), which is the trust boundary the design relies on.

    - **enforce** (default, prod): a valid operator JWT whose
      ``canonical_operator_email`` equals the signed/asserted approver is
      REQUIRED. Absent/forged/wrong-aud|iss/EXPIRED/unconfigured → 403.
    - **e2e**: if a JWT is present, verify+bind EXACTLY as enforce; if absent, fall
      back to the pre-C5 ``caller == expected_approver`` check (returns ``None``,
      no verified operator email) so the smoke + offline tests run without a real
      CF JWT.
    """
    if IAC_OPERATOR_AUTH_MODE == "e2e" and operator_jwt is None:
        # Legacy fallback (preserves the C4 behavior exactly). Gate on ``is None``,
        # NOT falsiness: an empty-string operator_jwt is "present but invalid" and
        # must fail closed via the verify path below (a bad coordinator forwarding
        # an empty JWT in e2e is still a 403, never a silent legacy bypass).
        if caller != expected_approver:
            log.warning(
                "operator(e2e-legacy): actor %s != approver %s", caller, expected_approver
            )
            raise HTTPException(status_code=403, detail="actor is not the signed approver")
        return None

    # enforce mode (and e2e WITH a JWT): a real operator JWT is required + bound.
    if not operator_jwt:
        raise HTTPException(status_code=403, detail="operator JWT required")
    try:
        claims = verify_cf_access_jwt(operator_jwt, CF_ACCESS_TEAM_DOMAIN, CF_ACCESS_AUD_TAG)
        email = canonical_operator_email(claims)
    except CfAccessJwtError:
        # One INFO line, no token bytes; do NOT echo the verifier's detail.
        log.info("operator: CF Access JWT verification failed")
        raise HTTPException(status_code=403, detail="operator verification failed")
    if email != expected_approver:
        log.warning("operator: verified email != signed approver")
        raise HTTPException(status_code=403, detail="operator is not the approver")
    return email


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
    token ONCE.

    C5b-2: the worker first re-verifies the forwarded operator JWT and binds its
    verified email == ``req.approver`` (enforce mode) BEFORE any artifact work or
    mint, so a compromised coordinator cannot mint an approval for an approver it
    didn't authenticate."""
    operator_email = _verify_operator(
        req.operator_jwt, expected_approver=req.approver, caller=caller
    )
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
    log.info(
        "propose: id=%s approver=%s caller=%s operator=%s",
        record.approval_id, req.approver, caller, operator_email,
    )
    return {
        "approval_id": record.approval_id,
        "approval_token": raw_token,
        "expires_at": expires_at,
    }


def _approval_window() -> tuple[str, str]:
    from driftscribe_lib.approvals import new_approval_window

    return new_approval_window(now=_now())


@app.post("/apply")
def apply(req: ApplyRequest, caller: str = Depends(_verify_caller_dep)) -> dict:
    """Verify → operator-bind → claim (single-use burn) → re-verify → denylist →
    fidelity → freshness → saved-plan apply. The §3.6 claim-first order: every
    decision reads from ``signed_payload`` once the HMAC has verified the bytes.

    C5b-2: the operator-identity bind (``_verify_operator``) runs PRE-CLAIM,
    exactly where the old ``caller == approver`` check was — so an
    absent/forged/expired operator JWT fails 403 with NOTHING burned. The
    inter-service ``_verify_caller_dep`` SA-allowlist still runs; the CF binding is
    ADDITIONAL, not a replacement.

    State-lock contention on any tofu step (init/refresh-only/apply) surfaces as
    the DISTINCT terminal phase ``lock_refused`` (HTTP 423), not ``failed`` (502):
    a held or orphaned GCS lock that needs operator ``force-unlock`` (NEVER an
    auto-unlock) before retry. Detection is post-claim, so a transient lock burns
    the approval and the retry is a re-propose (operator re-clicks Approve) — an
    accepted low-friction trade for keeping the claim-first single-use invariant."""
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
    # PRE-CLAIM operator bind (replaces the C4 caller==approver check). Raises 403
    # BEFORE claim_pending, so an absent/forged/expired JWT burns NOTHING. Returns
    # the verified operator email (or None in the e2e-legacy path).
    operator_email = _verify_operator(req.operator_jwt, expected_approver=sp["approver"], caller=caller)

    attempt_id = str(uuid.uuid4())
    now = _now()
    claimed = store.claim_pending(
        req.approval_id, used_by=caller, used_at=now,
        apply_audit={"phase": "claimed", "claimed_at": now.isoformat(), "apply_attempt_id": attempt_id,
                     "caller_sa": caller, "operator_email": operator_email},
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
        _fail(store, req.approval_id, attempt_id, "integrity_refused", 422, f"artifact fetch failed: {e}",
              caller_sa=caller, operator_email=operator_email)
    except ArtifactIntegrityError as e:
        _fail(store, req.approval_id, attempt_id, "integrity_refused", 422, f"artifact integrity: {e}",
              caller_sa=caller, operator_email=operator_email)
    except tofu_runner.FidelityError as e:
        _fail(store, req.approval_id, attempt_id, "fidelity_refused", 422, f"fidelity: {e}",
              caller_sa=caller, operator_email=operator_email)
    except tofu_runner.LockRefused as e:
        # Defensive parity with the run-tofu block below. No subprocess in THIS
        # gate block acquires the state lock today — the only tofu call here is the
        # fidelity probe (`tofu version`, read-only, which raises TofuStepError, not
        # LockRefused). But classifying it identically means a future refactor that
        # ever routes a locking step through _raise_step_failure here can never
        # leave a burned approval stranded at phase="claimed" behind an unhandled
        # 500. Same terminal lock_refused/423 + operator-only force-unlock.
        _fail(store, req.approval_id, attempt_id, "lock_refused", 423,
              f"refusing apply: tofu {e.step} could not acquire the state lock "
              "(held or orphaned); operator force-unlock required before retry",
              caller_sa=caller, operator_email=operator_email,
              extra={"step": e.step, "apply_exit_code": e.exit_code, "stderr_tail": e.stderr[-500:]})
    except tofu_runner.TofuStepError as e:
        # The fidelity check probes `tofu version`; a probe failure pre-apply →
        # fail closed with a terminal audit (not an unhandled 500 that would
        # leave the burned approval at the outcome-unknown phase="claimed").
        _fail(store, req.approval_id, attempt_id, "failed", 502, f"tofu probe failed: {e}",
              caller_sa=caller, operator_email=operator_email,
              extra={"step": e.step, "apply_exit_code": e.exit_code})
    except (ValueError, TypeError, json.JSONDecodeError, KeyError) as e:
        _fail(store, req.approval_id, attempt_id, "verify_refused", 422, f"apply rejected: {e}",
              caller_sa=caller, operator_email=operator_email)

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
              "refusing apply: refresh-only detected out-of-band drift",
              caller_sa=caller, operator_email=operator_email,
              extra={"stderr_tail": e.stderr[-500:]})
    except tofu_runner.LockRefused as e:
        # State-lock contention (held or orphaned GCS lock) — a DISTINCT terminal
        # post-claim outcome (HTTP 423), not an apply failure (502). The operator
        # action is unambiguous: `tofu force-unlock` (operator-only — NO
        # auto-unlock) then retry. The claim already burned the approval, so the
        # retry is a re-propose (operator re-clicks Approve) — accepted low-friction
        # behavior. Ordered before TofuStepError defensively (LockRefused is not a
        # subclass, but the explicit ordering documents intent).
        _fail(store, req.approval_id, attempt_id, "lock_refused", 423,
              f"refusing apply: tofu {e.step} could not acquire the state lock "
              "(held or orphaned); operator force-unlock required before retry",
              caller_sa=caller, operator_email=operator_email,
              extra={"step": e.step, "apply_exit_code": e.exit_code, "stderr_tail": e.stderr[-500:]})
    except tofu_runner.TofuStepError as e:
        _fail(store, req.approval_id, attempt_id, "failed", 502,
              f"tofu {e.step} failed (exit {e.exit_code})",
              caller_sa=caller, operator_email=operator_email,
              extra={"step": e.step, "apply_exit_code": e.exit_code, "stderr_tail": e.stderr[-500:]})

    store.set_apply_audit(req.approval_id, {
        "phase": "applied", "apply_attempt_id": attempt_id,
        "freshness_exit_code": outcome.freshness_exit, "apply_exit_code": outcome.apply_exit,
        "applied_at": _now().isoformat(),
        "state_serial": outcome.state_serial, "state_lineage": outcome.state_lineage,
        "caller_sa": caller, "operator_email": operator_email,
    })
    log.info("apply: id=%s attempt=%s APPLIED serial=%s", req.approval_id, attempt_id, outcome.state_serial)
    return {"approval_id": req.approval_id, "status": "applied", "apply_attempt_id": attempt_id}


def _fail(store: PlanApprovalStore, approval_id: str, attempt_id: str, phase: str,
          http_status: int, detail: str, *, caller_sa: str, operator_email: str | None,
          extra: dict | None = None) -> NoReturn:
    """Write a terminal apply_audit (the approval is already burned) + raise.
    ``NoReturn`` documents the always-raises contract so a type checker narrows
    ``plan_bytes``/``outcome`` as bound after the gate try-blocks (and flags any
    future non-raising path). N2: every terminal audit carries BOTH identities —
    the SA ``caller_sa`` and the verified ``operator_email`` (None in e2e-legacy)."""
    audit = {"phase": phase, "apply_attempt_id": attempt_id, "failed_at": _now().isoformat(),
             "detail": detail, "caller_sa": caller_sa, "operator_email": operator_email}
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
