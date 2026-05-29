# Phase C4 — the `tofu-apply` worker (`workers/tofu_apply/`)

**Status:** PLAN (Codex-reviewed, thread `019e733b` — blockers folded; draft for user sign-off → implementation → live deploy + smoke)
**Date:** 2026-05-29
**Depends on:** C1 (denylist, `ed26d7a`), C2 (plan-builder + `c2.v1` metadata, proven live `26620367059`),
C3 (plan-bound approval schema, `180281c` / PR #16 — `PlanApproval`/`PlanApprovalStore` + the §3.6 consumer contract)
**Feeds:** C5 (coordinator + approval page + trusted operator-auth + the **apply-then-merge ordering** + **head-config delivery**)

**User decisions (AskUserQuestion, 2026-05-29):**

- **Scope = go all the way live.** Build + merge all of C4a/C4b/C4c as offline-tested code, then drive
  a **live no-op apply smoke** this session (operator runs the gcloud/Cloud-Build auth steps).
- **IAM = pre-grant broad apply IAM.** Default = a **hardened-broad custom role** (broad resource
  CRUD/read, but **no `*.setIamPolicy` / no IAM-policy-writer perms / no project-wide actAs / no
  SA-key or HMAC-key creation**) + an explicit KMS crypto binding + a `plan-hmac-key` accessor +
  `serviceAccountUser` scoped to payment-demo's runtime SA. This is "broad apply" without any
  IAM-escalation or cross-SA-impersonation vector. **Raw `roles/editor` + the org policy
  `constraints/iam.disableServiceAccountKeyCreation`** is the operator's documented fast path, with a
  larger (accurately stated) blast radius — editor's project-wide `iam.serviceAccounts.actAs` lets the
  worker deploy a workload as another SA and inherit its access. See §4.3 + §6.
- **Merge ownership = defer to C5.** The C4 worker holds **no GitHub token**. C5 owns the GitHub
  head/required-check verification *before* calling `/propose`, the apply-then-merge ordering (design
  Decision #6), **and the delivery of the approved head's `iac/` config to the worker** (see §3.0).

---

## §0. What C4 is — and what it deliberately is NOT

C4 is the **sole mutator** of DriftScribe-managed infra (design §9). It is a new private Cloud Run
worker (`workers/tofu_apply/`, service `driftscribe-tofu-apply`, SA `tofu-apply-sa`) that **owns the
plan-bound HMAC key** and runs `/propose`, `/apply`, `/deny` — the exact trust split as
`workers/rollback` (the worker holds the key; the coordinator can request a proposal + render the
page but **cannot mint a valid approval**, design §6.4/§7). It consumes the C3 schema
(`driftscribe_lib/approvals.py` `PlanApproval`/`PlanApprovalStore`) and the C2 artifacts; C3 §3.6 is
its ordering spec.

**Three firsts** vs every existing worker: (1) bakes a **pinned `tofu` 1.12.0 binary**; (2) bakes
`iac/` in (like `infra_reader`); (3) deploys with private ingress (design §2/§9) — see §5.6 for the
live-smoke caveat.

**In scope (C4):**

1. **C4a — promote `iac_plan_denylist` into the lib** (verbatim move + thin re-export shim, the C3
   metadata pattern) so the worker re-runs it at runtime, **and fix the denylist to protect the real
   service name `driftscribe-tofu-apply`** (Codex blocker — see §2).
2. **C4b — the worker security core:** `main.py` (three endpoints, §3.6 ordering), `gcs_fetch.py`
   (fetch-by-pinned-generation), the `tofu` subprocess orchestration (init → refresh-only freshness
   gate → saved-plan apply in a per-request temp workdir), and a small additive
   `PlanApprovalStore.claim_pending(extra_fields=...)` for crash-safe audit. Fully offline-testable.
3. **C4c — deploy + IAM + runbook:** `Dockerfile` (pinned+verified tofu), `cloudbuild.tofu-apply.yaml`,
   the `tofu-apply-sa` IAM bootstrap (broad apply + KMS + artifact-read + Firestore + plan-hmac-key +
   the org-policy preflight), the `plan-hmac-key` secret, and `docs/runbooks/tofu-apply.md`.
4. **Live (this session):** provision secret + IAM, submit the Cloud Build, drive the **no-op apply
   smoke** + negatives, then redeploy `--ingress=internal`.

**Explicitly deferred (NOT C4 — assigned to C5):**

- The `/propose` orchestration, the approval page, **trusted operator-auth** (gives D2 `approver`
  genuine non-repudiation — until then tamper-evident audit, C3 §4), the **GitHub head/required-check
  verification before `/propose`**, the **apply-then-merge ordering** (Decision #6), and the
  **delivery of the approved head's `iac/` config to the worker** for resource-set-changing plans
  (§3.0).
- Async apply / status-poll (C4 applies synchronously; `main.py` keeps apply separable for C5, §5.2).

---

## §0.1 (Codex blocker #1) Config fidelity: what the baked `iac/` can and cannot apply

The worker bakes `iac/` from `main` at build time, but C2 produces plans from the **PR head SHA**
(`.github/workflows/iac.yml:210-219`) and uploads only `plan.tfplan` + `plan.json` + `metadata.json`
(no config tree). The locked design §6.7 wants the refresh-only check "from the approved head." This
creates a fidelity boundary that C4 handles **fail-closed**:

- **`tofu apply plan.tfplan`** uses the **config embedded in the saved plan** (which the worker
  verifies by sha256 + HMAC), so apply is faithful to the approved head regardless of the baked files.
- **`tofu init` + `tofu plan -refresh-only`** read the **baked `iac/` files**. The fidelity gate
  (below) refuses any plan that creates a resource, touches an address the baked config doesn't
  declare, or changes providers — so the accepted set is **updates/no-ops of baked-declared resources**
  (deletes are independently hard-denied by the denylist), for which init/refresh against the baked
  snapshot are faithful (refresh is live-vs-state). Plans needing the head config (adds, provider changes, data-source/lifecycle reshapes
  that alter refresh) are **refused fail-closed**, not silently mis-applied.
- **C4 fail-closed fidelity gate (before init/refresh/apply):**
  (i) assert `signed metadata.opentofu_version == baked tofu version` AND
  `signed metadata.provider_lockfile_sha256 == sha256(baked iac/.terraform.lock.hcl)` → mismatch
  (e.g. the PR changed providers) → **refuse**;
  (ii) **resource-set guard (Codex round-2/3):** parse the fetched `plan.json` and **refuse** if any
  managed `resource_changes` action contains `"create"`, OR if any mutating `resource_changes[].address`
  (after **normalizing `count`/`for_each` instance suffixes** — strip a trailing `[...]` — so
  `google_x.y[0]`/`google_x.y["k"]` map to `google_x.y`) is **not present in the baked `iac/` declared
  address set** (extracted from the baked `*.tf` — `resource "<type>" "<name>"` → `<type>.<name>`,
  optionally via `driftscribe_lib.iac_hcl`). **Module-nested addresses** (`module.*`) are refused
  outright unless module-aware extraction is added (fail-closed; none today). This makes baked-config
  apply genuinely fail-closed: the worker faithfully applies updates/no-ops of baked-declared resources
  and **refuses adds / unknown / module addresses** (which need the head config) — init/refresh/apply
  against the baked snapshot are never misled, not merely "scoped by convention."
- **C4-live scope:** the smoke applies a **no-op (or existing-attribute-change) plan against the
  current `main` `iac/`** — passes the fidelity gate (no creates, payment-demo address declared). This
  proves the full gate machinery live.
- **General resource-set-changing applies are a C5 capability:** C5 delivers the approved head's
  `iac/` to the worker — either by **merge-then-apply-from-main** (it already owns the merge, Decision
  #6) or a **signed config bundle** added to C2. C4 documents this contract and does not claim to
  apply arbitrary config-changing PRs.

---

## §1. Reconciling the locked design (§6) + C3 §3.6 + the user decisions

The locked design §6.4/§6.5/§6.7/§7 fixes ownership (apply worker), the consumer order
(claim → re-fetch → re-verify → denylist → freshness → saved-plan apply, no re-plan), and the
freshness requirement. C3 §3.6 refines to **claim-first** and mandates **every apply-time decision
reads from `signed_payload(stored)`**. The user decisions resolve the forks (live no-op scope, broad
IAM, merge+head-config→C5).

**C3 library C4 consumes (do not redesign):** `verify_plan_approval`, `signed_payload`,
`plan_approval_is_expired`, `verify_artifact_integrity`, `build_plan_approval_payload`,
`new_approval_window`, `PlanApproval`, `PlanApprovalStore.{create,get,claim_pending,claim_denied}`.
C4's only lib changes: the denylist promotion (§2) + an additive optional `apply_audit` field on
`PlanApproval`, an `apply_audit=` param on `claim_pending`, and a `set_apply_audit` method — for
crash-safe, schema-safe audit (§3.3).

---

## §2. C4a — promote `iac_plan_denylist` into `driftscribe_lib` (mirrors C3 §2) + fix the service name

**Why:** `/propose` and `/apply` re-run the C1 denylist on the **fetched** `plan.json` (contract #4).
The logic is in `tools/iac_plan_denylist.py`, but no worker container ships `tools/` and `tools/` is
not an installed package — same forcing function as the C3 metadata promotion. No import-time state
(all control-plane constants are hardcoded literals; the only `open()` is in the CLI `_main`).

**Change:**

- **New `driftscribe_lib/iac_plan_denylist.py`** — move **verbatim**: docstring (with a one-line
  tweak noting the CLI now lives in the shim), `from __future__ import annotations`, `import json`
  (drop CLI-only `argparse`/`sys`), `Violation` + `DenylistInput`, **all** action-tuple + control-plane
  constants, `load_plan_json`, every `_check_*`/helper, `evaluate`. Add
  `__all__ = ["Violation","DenylistInput","load_plan_json","evaluate"]`.
- **(Codex blocker #3) Fix `CONTROL_PLANE_SERVICE_NAMES`:** the actual deployed name follows the
  `driftscribe-` convention → **add `driftscribe-tofu-apply`** (and `driftscribe-tofu-editor` for
  symmetry). Keep the placeholder `tofu-apply`/`tofu-editor` as harmless aliases. Add a denylist
  fixture asserting a mutating change to `driftscribe-tofu-apply` emits `control-plane-service`.
  (`tofu-apply-sa` is already in `CONTROL_PLANE_SA_ACCOUNT_IDS`; `plan-hmac-key` already in
  `CONTROL_PLANE_SECRET_IDS` — verified.)
- **Shrink `tools/iac_plan_denylist.py`** to: docstring + a re-export of the 4 API symbols **AND all
  15 uppercase module constants** (Codex important #7 — `tests/unit/test_iac_plan_denylist.py`
  imports 10, but the old module exposed all 15 incl. the action-tuple ones; re-export every uppercase
  name to keep all callers green) + `import argparse, json, sys` + the **byte-identical** CLI tail
  (`prog="python -m tools.iac_plan_denylist"` + `__main__`). Preserves `iac.yml:364`.

**Tests/ownership:** existing denylist tests stay green untouched; add `test_iac_plan_denylist_lib.py`
(lib import + behavior + shim-identity `shim.evaluate is lib.evaluate` + `shim.IAM_EXTRA_TYPES is
lib.IAM_EXTRA_TYPES`) + the `driftscribe-tofu-apply` fixture. CODEOWNERS line for the new lib file.
Flip `iac/README.md` promotion wording to past tense. No `pyproject` change.

**In-process API C4 calls:** `parsed, v = load_plan_json(text)` → if `v`, deny; else
`evaluate(DenylistInput(plan=parsed))` → non-empty == deny. Wrap both in a broad `try/except` →
**deny** (lib is fail-closed on policy/structure only).

---

## §3. C4b — the worker security core

### 3.1 `workers/tofu_apply/gcs_fetch.py` — fetch-by-pinned-generation (worker-local I/O, NOT lib)

GCS I/O is fenced out of the lib (C3 Decision D); the lib's pure `verify_artifact_integrity` consumes
the bytes this returns. Mirror `tools/iac_plan_artifact_upload.py` (frozen-dataclass inputs, deferred
`from google.cloud import storage`, `Any`-typed bucket for `MagicMock` injection).

```python
def parse_gs_uri(uri: str) -> tuple[str, str]:
    # "gs://<bucket>/<object>" → (bucket, object). Fail-closed: reject non-gs://,
    # empty bucket, empty object.

def fetch_object_pinned(bucket: Any, object_name: str, generation: int) -> bytes:
    blob = bucket.blob(object_name, generation=generation)
    return blob.download_as_bytes(raw_download=True, if_generation_match=generation)
```

- **`generation=`** pins the read to that exact archived-but-resolvable revision (artifact bucket has
  Object Versioning ON); **`raw_download=True`** returns un-decoded bytes so `sha256` matches the
  producer's `sha256sum` (`iac.yml:369-370`); **`if_generation_match`** → `PreconditionFailed` if the
  server-resolved generation differs.
- **(Codex important #6) Validate all three artifacts from the signed payload/metadata:** use **both**
  the signed `artifact_uri_*` (for bucket+object) **and** the signed `generation_*`. Assert the parsed
  **bucket == `driftscribe-hack-2026-tofu-artifacts`**, the **object prefix** matches
  `pr-<N>/<head_sha>/run-<id>-<attempt>/`, and the **basename** is exactly `metadata.json` /
  `plan.tfplan` / `plan.json` respectively. Reject otherwise (defense-in-depth even though the URIs
  are HMAC-signed).
- **Errors → fail closed:** lazy-import `NotFound, PreconditionFailed`; both → HTTPException refusal.
  `storage.Client(project="driftscribe-hack-2026")` passed explicitly. sha256 mismatch surfaces as the
  lib's `ArtifactIntegrityError`, not here. `int()` the numeric-string generations at the boundary.

### 3.2 The `tofu` subprocess orchestration (the §6.7 freshness algorithm)

Run in a **per-request temp workdir** (Codex blocker #2): copy the baked `iac/` into
`<tmp>/iac` (or symlink the read-only parts) and **write the fetched, integrity-verified
`plan.tfplan` bytes to `<tmp>/iac/plan.tfplan`**; run every `tofu -chdir=<tmp>/iac …` against that
exact path. Never apply a baked/ambient plan file. `TF_VAR_tofu_state_kms_key` is set in the
subprocess env on **every** call (mandatory: `iac/versions.tf` `encryption{state/plan enforced=true}`;
even `tofu show` must decrypt and takes no `-var`). Source it from the Cloud Run env var (a key
resource **path**, not key material — the decrypt authority is the SA's KMS IAM binding).

**Fidelity asserts (Codex blocker #1 + round-2 guard, before init/refresh/apply):**
(i) `signed metadata.opentofu_version == tofu version -json` AND
`signed metadata.provider_lockfile_sha256 == sha256(baked iac/.terraform.lock.hcl)`;
(ii) **resource-set guard** — parse fetched `plan.json`: refuse if any managed `resource_changes`
action contains `"create"` or any mutating address ∉ the baked declared address set (§0.1). Else
refuse. (`tofu version -json` is itself a subprocess — these run before init/refresh/apply.)

1. **`tofu -chdir=<tmp>/iac init -input=false -no-color -lockfile=readonly`** — REQUIRED (the saved
   plan embeds neither backend config nor providers); `-input=false` so a prompt never blocks
   (Codex important #2); `-lockfile=readonly` forbids silent provider upgrade. Providers are baked
   (§4.1) so init does no registry fetch.
2. **Drift gate (non-mutating):**
   `tofu -chdir=<tmp>/iac plan -refresh-only -detailed-exitcode -input=false -no-color -lock=true -lock-timeout=120s`.
   The bare `plan` form **never writes state** (unlike `refresh`/`apply -refresh-only`).
   `-detailed-exitcode`: **0 = fresh → proceed; 2 = drift → REFUSE; 1 = error → REFUSE.** Key on the
   exit code; capture stdout/stderr for audit. **Load-bearing:** tofu's built-in saved-plan staleness
   check only fires on a state **serial/lineage** change; pure out-of-band edits (e.g. operator
   `gcloud run deploy`) don't bump the serial, so only this gate catches them.
3. **Apply (only if step 2 == 0):**
   `tofu -chdir=<tmp>/iac apply -input=false -no-color -lock=true -lock-timeout=120s -auto-approve plan.tfplan`.
   tofu re-validates serial+lineage at apply (defense-in-depth: "Saved plan is stale" if the serial
   moved since step 2). Saved-plan apply rejects new planning options (anti-TOCTOU §6.6). **Never
   re-plan.** `-auto-approve` is correct in a worker — the human gate is the C3 HMAC approval.

**(Codex important #1) TOCTOU residual — stated honestly:** the refresh-only gate + apply's own
serial recheck close drift that **bumps the state serial**. Drift landing in the gap between the gate
and apply that does **not** bump the serial (e.g. an out-of-band `gcloud` edit to payment-demo)
remains a residual; the gate narrows but does not fully eliminate out-of-band drift. Documented, not
overstated.

**Subprocess seam for tests:** one `_run_tofu(args, *, env) -> (rc, out, err)` indirection; tests
stub `rc` (0/1/2, apply ok/fail, lock-timeout), assert `TF_VAR_tofu_state_kms_key` is on every call,
apply never runs when the gate ≠ 0, the fidelity asserts gate before any tofu run, and no re-plan.

### 3.3 `workers/tofu_apply/main.py` — the three endpoints

Boot-time `os.environ[...]` (hard-required): `GCP_PROJECT`, `OWN_URL`, `COORDINATOR_URL`,
`ALLOWED_CALLERS`, **`PLAN_APPROVAL_HMAC_KEY`** (renamed from rollback's `APPROVAL_HMAC_KEY` to avoid
confusion — Codex nit), `TF_VAR_tofu_state_kms_key`, `ARTIFACT_BUCKET`. Mirror rollback:
`_verify_caller_dep`, `_get_plan_approval_store()` + `_get_artifact_bucket()` indirections,
`install_trace_middleware`, unauthenticated `/healthz`. Closed schemas (`extra="forbid"`).

**`/propose`** (worker independently verifies BEFORE minting — design §6.4). **Note (Codex blocker
#5):** C4 has no GitHub token, so `/propose` does NOT verify the PR head / required checks — that is
a **C5 coordinator responsibility before it calls `/propose`**. C4's `/propose` does:
```
caller = verify_caller                                  # coordinator SA; operator subject in req body (until C5)
fetch metadata.json @ (signed uri, generation_metadata) → validate c2.v1
fetch plan.tfplan @ (uri, generation_plan) + plan.json @ (uri, generation_json)
verify_artifact_integrity(...)                          # contract #2
denylist on fetched plan.json: parse-violation or non-empty == refuse   # contract #4
fidelity asserts (version+lockfile sha; resource-set guard)             # §3.2 — also here so /propose never mints a dead approval (Codex round-3)
(issued_at, expires_at) = new_approval_window(now=utcnow())             # single clock site
payload = build_plan_approval_payload(metadata, locators, approver=operator_subject, window)
record, raw_token = PlanApprovalStore.create(payload, hmac_key, created_by=caller)
return {approval_id, approval_token: raw_token, expires_at}             # token returned ONCE
```

**`/apply`** (the §3.6 claim-first order — every read from `sp = signed_payload(stored)`):
```
actor = verify_caller
stored = store.get(approval_id)                         # 404 if missing
stored.status == "pending"                              # 403 otherwise
verify_plan_approval(token, stored, hmac_key)           # 403 — signed bytes now trusted
sp = signed_payload(stored)                             # ALL reads below from here
not plan_approval_is_expired(stored, now=utcnow())      # SIGNED window (not stored.expires_at) — 403
actor == sp["approver"]                                 # 403 — D2 enforcement (teeth with C5)
claimed = store.claim_pending(approval_id, used_by=actor, used_at=now,
            apply_audit={"phase": "claimed", "claimed_at": now_iso, "apply_attempt_id": attempt_id})  # BURN + crash-safe audit; None→403
# ---- burned; every failure below is fail-closed (re-propose required) ----
re-fetch metadata @ (sp["artifact_uri_metadata"], sp["generation_metadata"]); rebuild+compare_digest vs payload_canonical  # #1
re-fetch plan.tfplan/plan.json @ (sp["metadata"]["artifact_uri_*"], generation_*); verify_artifact_integrity              # #2
denylist on fetched plan.json: non-empty == abort       # #4
fidelity asserts (version+lockfile sha; resource-set guard)   # §3.2
materialize plan.tfplan → temp workdir; tofu init → refresh-only gate (0) → apply plan.tfplan        # §3.2
store.set_apply_audit(approval_id, {phase, freshness_exit, apply_exit, apply_status, applied_at, state_serial, state_lineage, apply_attempt_id})  # §5.3
return {approval_id, status: "applied", apply_attempt_id}
```

**(Codex important #3 + round-2 blocker #2) Crash-safe audit, schema-safe:** `PlanApproval.get()`
builds `PlanApproval(approval_id=…, **data)`, so a NEW top-level Firestore key would raise `TypeError`
on every read of a used doc. Therefore: add **one** additive optional field `apply_audit: dict | None =
None` to `PlanApproval` (single nested map, future-proof — no field churn as audit grows), and add an
optional `apply_audit: dict | None = None` param to `PlanApprovalStore.claim_pending` (merged into the
transactional update alongside `used_by`/`used_at`) so `{"phase":"claimed",…}` lands **atomically with
the status flip**. The param is validated to be a plain dict and the merge writes only the
`apply_audit` key (it cannot collide with control fields `status`/`used_by`/`used_at`/`token_hmac`/
`payload_canonical`/`payload_sha256`). A new `set_apply_audit(approval_id, dict)` does the post-apply
non-transactional update with the terminal record (`phase`=`applied`/`failed`/`drift_refused`/
`integrity_refused`/…). If the worker dies post-claim, the doc reads `used` + `apply_audit.phase=
"claimed"` (outcome-unknown signal), never a silent `used`.

**`/deny`** mirrors rollback's hardened path: `get` → pending → `verify_plan_approval` →
`claim_denied(denied_by=actor, denied_at=now)`. HMAC verified **before** the flip; coordinator never
flips Firestore directly.

---

## §4. C4c — container, deploy, IAM, secret, runbook

### 4.1 `workers/tofu_apply/Dockerfile`

`FROM python:3.12-slim`; **multi-stage**: builder installs `ca-certificates curl unzip`, downloads
the **pinned** `tofu_1.12.0_linux_amd64.zip`, and verifies it against a **repo-controlled
`ARG TOFU_SHA256`** literal via `sha256sum -c` (fail build on mismatch), unzips one
`/usr/local/bin/tofu`; runtime `COPY --from=builder` only the binary. **Checksum trust (Codex
review):** the expected SHA-256 is pinned in-repo + code-reviewed, NOT trusted from a fetched
`SHA256SUMS` (which only proves same-origin consistency) — a swapped/MITM'd release artifact set fails
the build; a `gpg --verify` of the release SUMS (pinned OpenTofu release-key fingerprint) is an
optional higher-assurance add. `ARG TOFU_VERSION=1.12.0` + `ARG TOFU_SHA256=…` (both updated in
lockstep; `TOFU_VERSION` must equal `iac/.terraform.lock.hcl` / `metadata.opentofu_version`). `COPY driftscribe_lib/`, the worker
sources, **`COPY iac/ ./iac/`**, and **bake providers** (builder `tofu -chdir=iac init -backend=false`
→ copy `.terraform/`) for a hermetic no-registry apply. `tofu version` build-time smoke. Runtime deps
mirror rollback + **`google-cloud-storage`** (NEW). Parallel `pyproject.toml` (canonical dep doc).
`ENV PYTHONPATH=/app PORT=8080 IAC_SNAPSHOT_SHA=<build sha>`; shell-form uvicorn CMD.

### 4.2 `infra/cloudbuild.tofu-apply.yaml`

Model on `infra/cloudbuild.infra-reader.yaml` (build ONE image, deploy ONE service) — NOT the full
`cloudbuild.yaml` (it redeploys payment-demo and breaks the Phase-A zero-diff pin). Deploy:
`gcloud run deploy driftscribe-tofu-apply --region=asia-northeast1 --no-allow-unauthenticated
--service-account=tofu-apply-sa@$PROJECT_ID.iam.gserviceaccount.com --min-instances=0
--max-instances=1 --concurrency=1 --timeout=900
--set-env-vars=GCP_PROJECT=$PROJECT_ID,TARGET_REGION=asia-northeast1,OWN_URL=https://placeholder.invalid,
COORDINATOR_URL=https://placeholder.invalid,ALLOWED_CALLERS=driftscribe-agent@$PROJECT_ID.iam.gserviceaccount.com,
TF_VAR_tofu_state_kms_key=<full key path>,ARTIFACT_BUCKET=driftscribe-hack-2026-tofu-artifacts
--set-secrets=PLAN_APPROVAL_HMAC_KEY=plan-hmac-key:latest`, then the post-deploy `OWN_URL` writeback.
**Ingress (Codex important #5):** the C4-live deploy uses `--no-allow-unauthenticated` (ID-token-gated,
internet-reachable for the operator-driven smoke); the runbook **immediately redeploys
`--ingress=internal` after the smoke** (a one-flag redeploy) and removes any temporary invoker grant —
NOT an open-ended fast-follow. The worker is never publicly unauthenticated.

### 4.3 `tofu-apply-sa` IAM (broad — user decision; corrected per Codex blocker #4)

A **dedicated** SA (NOT the federated CI plan-builder SA). A `setup_iac_backend.sh` §5e/§8 block,
idempotent + gated on resource existence:

1. **The broad apply grant — corrected per Codex round-2.** `roles/editor` is broader than "apply
   resources": it **includes** `iam.serviceAccounts.actAs` **project-wide** + Cloud Run service/job
   `create/update/run`, so a compromised worker can **deploy a workload AS any project SA and inherit
   that SA's access** (e.g. deploy a job as a SA with `secretAccessor` on the rollback key → read it).
   It also includes `iam.serviceAccountKeys.create` + Storage-HMAC-key creation (direct credential
   minting). Editor **excludes** `cloudkms.*` crypto, `secretmanager.versions.access` (direct),
   IAM-policy admin, owner, project deletion. **So raw editor does NOT give "cannot read other
   secrets / cannot escalate" — the actAs path defeats that, and the key-creation org policy does not
   close it.**
   **Recommended implementation of the user's "broad apply" choice (hardened-broad):** a **custom
   role** carrying broad resource CRUD/read but **NO IAM-policy-writer permissions at all** —
   explicitly **no `*.setIamPolicy`** (incl. `run.services.setIamPolicy`/`run.jobs.setIamPolicy`,
   Codex round-3: `roles/run.admin` grants these, so do **not** use run.admin — use `run.developer`-
   level perms for the Cloud Run resources iac/ manages, add per resource-type as iac/ grows),
   **no `resourcemanager.*Iam*`**, **no `iam.serviceAccounts.setIamPolicy`**, **no project-wide
   `iam.serviceAccounts.actAs`**, **no `iam.serviceAccountKeys.create`**, **no `storage.hmacKeys.create`**;
   **plus** explicit `roles/iam.serviceAccountUser` scoped to **payment-demo's runtime SA only** (the
   one actAs the apply genuinely needs). This is "broad apply across the managed resource types"
   without any IAM-escalation or cross-SA-impersonation vector. **Fast path (operator's call) — raw
   `roles/editor`** + the org policy
   `constraints/iam.disableServiceAccountKeyCreation` (preflight §5.7): simpler and matches "project-
   wide broad," but accept the documented actAs blast radius (worker can impersonate other project SAs
   via workload deploy). The plan defaults to **hardened-broad**; surfaced to the user at presentation.
2. **`roles/cloudkms.cryptoKeyEncrypterDecrypter` on the single `tofu-state` key** — REQUIRED
   (editor grants no KMS crypto).
3. **`roles/storage.objectViewer` on `gs://…-tofu-artifacts`** — read artifacts by pinned generation.
4. **`roles/datastore.user`** — `plan_approvals` (Firestore has no collection-scope IAM — shared
   blast radius with rollback, acknowledged).
5. **`roles/secretmanager.secretAccessor` on `plan-hmac-key`** — REQUIRED (editor grants no secret
   payload access; the worker reads ONLY this secret, not the rollback key). Plus `actAs` on
   payment-demo's runtime SA to apply the service (covered by editor; kept explicit if using a custom
   role).

Add `tofu-apply-sa` to the Cloud Build `actAs` loop (`setup_prod_project.sh`) and the coordinator
`run.invoker` loop (`setup_secrets.sh`).

### 4.4 `plan-hmac-key` secret

New Secret Manager secret (already a C1 denylist entry), **separate** from rollback's
`approval-hmac-key` (the C3 HMAC is domain-separated). Auto-generated first-run-only
(`token_urlsafe(32)`), mirroring `approval-hmac-key`. Operator step (§7 non-goal of the code PRs).

### 4.5 `docs/runbooks/tofu-apply.md`

The §5.7 ordered pre-flight (incl. the org-policy check), the no-op smoke (§8) + the post-smoke
`--ingress=internal` redeploy, the claim-burns-on-failure → re-propose footgun, lock-timeout +
**never auto-force-unlock**, and the partial-apply reconciliation contract (§5.1).

---

## §5. Resolved completeness gaps (reasoned defaults)

**5.1 Partial/failed apply.** A mid-apply failure leaves tofu **partial state**; the approval is
already burned. The worker returns a `502`-class error + the apply exit code + captured stderr tail,
writes `apply_status="failed"`, and the operator **reconciles manually + re-proposes** (a fresh plan
against the partial state). C4 does **not** auto-rollback (sole-mutator stays minimal). Runbook §4.5.

**5.2 Sync vs async.** Synchronous (`--timeout=900`; one resource = fast). `main.py` keeps apply in a
separable function for C5 async.

**5.3 Audit / non-repudiation (Codex important #4 + round-2 blocker #2).** OpenTofu CLI does not
reliably expose a Cloud Run LRO name, so drop the rollback-style `operation_name` for apply. Define an
internal `apply_attempt_id` (uuid) and record everything in the **single `apply_audit` map** on the
`plan_approvals` doc (NOT new top-level fields — see §3.3 for the `get()` `TypeError` reason):
`apply_attempt_id`, `phase`, `freshness_exit_code`, `apply_exit_code`, `apply_status`, `applied_at`,
and the **state serial/lineage** parsed from `tofu state pull` (serial + lineage **only** — never log
full state). `apply_audit.phase="claimed"` is written atomically with the claim (§3.3); the terminal
record via `set_apply_audit`.

**5.4 Idempotency / retry.** Claim-first **is** the idempotency contract: retry after a successful
claim → `status=="used"` → 403 (no double-apply). Post-claim transient failure burns the approval →
re-propose. Documented, accepted.

**5.5 Lock contention.** Rely on the GCS atomic `.tflock`; finite `-lock-timeout=120s`, **fail closed**
on lock-acquire failure; **never auto-force-unlock**. `max-instances=1`/`concurrency=1` avoids
self-contention. Contention with a concurrent C2 plan-builder is expected (§3.2) → fail-fast.

**5.6 Private ingress.** Design §2/§9 mandate `--ingress=internal`, but the coordinator→internal-ingress
egress path (VPC connector / `internal-and-cloud-load-balancing`) is unproven and C5 doesn't exist.
**C4-live (Codex important #5):** deploy `--no-allow-unauthenticated` (ID-token-gated) for the
operator-driven smoke, then **immediately redeploy `--ingress=internal` and remove any temporary
invoker grant** in the same runbook session. Not open-ended.

**5.7 Pre-flight (runbook).** (1) create `tofu-apply-sa` + grant the **hardened-broad custom role**
(broad resource CRUD minus key-creation minus project-wide actAs) — OR, if the operator picks the
raw-`roles/editor` fast path, first verify the org policy
`constraints/iam.disableServiceAccountKeyCreation` is enforced; (2) grant KMS crypto + artifact-read +
datastore + `plan-hmac-key` accessor + `serviceAccountUser` scoped to payment-demo's runtime SA;
(3) create+populate `plan-hmac-key`; (4) verify the `cryptoKeyEncrypterDecrypter` binding exists (else
`tofu init` fails to decrypt); (5) submit `cloudbuild.tofu-apply.yaml`; (6) coordinator `run.invoker`.
Missing any → first call 500s.

**5.8 Freshness scope.** Whole-config `refresh-only` (one resource today); revisit plan-targeted
refresh when `iac/` grows.

**5.9 `checksum='auto'` × `raw_download`.** Keep both; verify against the real SDK during impl.

**5.10 Health/cold-start.** `/healthz` unauthenticated; `min-instances=0` OK (tofu+providers baked →
cold start = boot + `tofu init`, within `--timeout=900`).

---

## §6. Threat model (extends C3 §4; corrected per Codex blocker #4 + importants #1/#8)

| Attack | Defense |
|---|---|
| Stale/older saved plan replayed | C3 binds `generation_*`+`head_sha`; C4 fetches those generations + refresh-only gate + tofu's serial/lineage recheck. |
| Out-of-band live drift not bumping the state serial | The refresh-only gate (exit 2) catches it. **Residual:** drift landing between the gate and apply that doesn't bump the serial (§3.2) — narrowed, not eliminated. |
| Tampered artifact bytes | `verify_artifact_integrity` recompute vs signed digests, on fetched bytes. |
| Fetch a different/"latest" generation | `generation=`+`if_generation_match` pin; `NotFound`/`PreconditionFailed`→refuse; bucket+prefix+basename allowlist. |
| Forbidden change slips through C2 | Denylist **re-run** on fetched `plan.json` at `/propose` AND `/apply` — independent of CI. Now also protects `driftscribe-tofu-apply` (§2). |
| Re-plan-at-apply action TOCTOU | Saved-plan apply only; never re-plan. |
| Double-apply / replay of a used approval | Transactional claim-first single-use flip; retry → 403. |
| Compromised coordinator mints/forges | Only the worker holds the HMAC key; `/deny` HMAC-verified before flip. |
| Post-mint Firestore edit of signed fields | Recomputed digest ≠ `token_hmac` → `verify_plan_approval` fails. |
| Post-mint edit of a denormalized field | Never trusted: expiry via signed window; all reads via `signed_payload`. |
| Concurrent applies / lock races | GCS atomic `.tflock`; finite timeout, fail closed; never auto-unlock. |
| Wrong/un-fidelity config applied | Fidelity asserts (opentofu_version + lockfile sha); C4-live scoped to baked-resource-set plans; resource-set-changing applies → C5 (§0.1). |
| **In-worker compromise (broad IAM) — escalation** | **Un-gatable by the approval flow (design §9).** Raw editor has THREE credential vectors: `iam.serviceAccountKeys.create`, Storage-HMAC-key creation, **and `iam.serviceAccounts.actAs` project-wide** (deploy a Cloud Run workload as another SA → inherit its access, incl. other secrets). The key-creation org policy closes only the first two; the **actAs path requires the hardened-broad custom role** (drops project-wide actAs **and all `*.setIamPolicy`**; scopes `serviceAccountUser` to payment-demo). Raw editor/run.admin also expose `*.setIamPolicy` (grant-self escalation) — another reason hardened-broad is the default. Even raw editor cannot decrypt other KMS keys *directly*, become owner, or delete the project. Residual containment = minimization (private-ingress post-smoke, tiny code surface, no shell/provisioner). |
| Worker deletes artifact generations (availability, Codex important #8) | Versioning ≠ immutability; a broad principal can delete generations. Documented availability risk; recommend bucket **retention/soft-delete** as a hardening follow-up. |

**Broad-IAM blast-radius (user-accepted, corrected per Codex round-2/3):** the plan's **hardened-broad**
default (custom role: broad resource CRUD/read minus **all `*.setIamPolicy`/IAM-policy-writer perms**
minus key-creation minus project-wide actAs; `serviceAccountUser` scoped to payment-demo) lets a
compromised worker create/update the managed resource types but **cannot** set IAM policy on any
resource, impersonate other SAs, mint SA/HMAC keys, decrypt other KMS keys, read other secrets, escalate
IAM, become owner, or delete the project. The **raw-editor fast path** (operator's call) is broader: even
with the key-creation org policy, editor's project-wide `iam.serviceAccounts.actAs` lets the worker
deploy a workload as another SA and inherit its access (incl. other secrets) — an accepted, documented
blast radius if chosen. Either way the gate constrains what applies **through the approval flow**; raw
SA power is the design's accepted sole-mutator posture, contained by minimization. Least-privilege
(payment-demo only) was offered and declined in favor of broad.

**Residual gap (C3 §4):** D2 `approver` is genuine non-repudiation only once `/apply` receives a
**trusted** operator identity (C5). At C4-live the smoke drives curl as the coordinator SA, so D2
degrades to tamper-evident audit. C4 ships the binding + enforcement code.

---

## §7. Test matrix (offline — design §10 "no live apply in automated tests")

- **C4a:** `test_iac_plan_denylist_lib.py` (lib import + behavior + shim-identity); the
  `driftscribe-tofu-apply` control-plane fixture; existing denylist tests green untouched.
- **`gcs_fetch`:** `parse_gs_uri` happy/malformed/empty; bucket+prefix+basename validation; pinned
  `blob(name, generation=N)` + `download_as_bytes(raw_download=True, if_generation_match=N)`;
  `NotFound`/`PreconditionFailed` → raise; MagicMock harness (reuse the upload test fakes).
- **tofu runner:** stubbed `_run_tofu` — refresh-only 0 → apply runs; 2 → refuse (apply never called);
  1 → refuse; apply failure → `apply_audit.phase="failed"`; lock-timeout → fail closed;
  `TF_VAR_tofu_state_kms_key` on every call; fidelity-assert mismatch (opentofu_version / lockfile
  sha) → refuse **before init/refresh/apply**; **resource-set guard** — a plan.json with a `["create"]`
  action or an address not in the baked declared set → refuse; never re-plans; plan.tfplan materialized
  to the temp path.
- **`/propose`:** mints once (token returned once, HMAC stored); integrity mismatch → refuse;
  denylist violation → refuse; bad metadata → refuse.
- **`/apply` (§3.6 matrix, fake Firestore + literal bytes + stubbed tofu):** HMAC-mismatch → 403;
  wrong-approver → 403; expired-via-**signed**-window (push denormalized `expires_at` to the future,
  prove the signed window still rejects — the C3 TTL-bypass) → 403; replayed/used (claim None) → 403;
  **claim writes `apply_audit.phase="claimed"` atomically + `get()` of a used doc with `apply_audit`
  does not raise**; **claim-burns-before-recheck** (post-claim failure leaves `status="used"` +
  `apply_audit.phase` reflecting the failure stage); digest-mismatch →
  abort; denylist on re-fetch → abort; freshness drift (exit 2) → abort; happy → applied + audit
  fields. **Expiry boundary (Codex nit):** pin that `plan_approval_is_expired` uses `<` (valid at the
  exact expiry second).
- **`/deny`:** token+HMAC verified before `claim_denied`; wrong token → 403, no flip.
- **Dockerfile structure test;** full suite (1339 + new) green; `ruff` clean.

---

## §8. Live deploy + no-op smoke (this session — design §8 Phase-C exit proof)

Driven with the operator. Pre-flight §5.7, then:

1. **Provision:** verify the org-policy key lockdown; create+populate `plan-hmac-key`; create
   `tofu-apply-sa` + grants; verify the KMS binding.
2. **Deploy:** `gcloud builds submit --config infra/cloudbuild.tofu-apply.yaml`.
3. **Produce a no-op artifact:** with `iac/` at the current `main` (payment-demo zero-change import,
   resource set == baked), hand-open a trivial no-op `iac/` PR and run the C2 plan-builder
   (`workflow_dispatch`) → uploads a no-op plan + posts artifact URIs + generations in the PR comment.
4. **Drive (authenticated curl as the operator/coordinator identity with `run.invoker`):** `/propose`
   (locator from the PR comment) → `approval_id` + token → `/apply` (id + token). `tofu apply
   plan.tfplan` executes **zero actions** — proves deploy + IAM + HMAC + fetch + integrity + denylist
   + fidelity + freshness + apply end-to-end with **no real infra change**.
5. **Negatives:** a control-plane/IAM/delete PR → worker denylist **rejects**; a **tampered-payload**
   approval (edited `payload_canonical`/`token_hmac`) → HMAC mismatch **rejected**; an
   expired/replayed/wrong-approver approval → **rejected**. (Note: "wrong PR head" is a C5/CI check,
   not a C4 capability — C4 only proves the *tamper* case via HMAC; Codex blocker #5.)
6. **Harden:** redeploy `--ingress=internal`, remove any temporary invoker grant. **Record** the run
   (like C2's `26620367059`).

---

## §9. Blast radius, invariants, non-goals

**Touched:** new `driftscribe_lib/iac_plan_denylist.py` + thin `tools/` shim (+ `driftscribe-tofu-apply`
denylist entry); additive `PlanApproval.apply_audit` field + `claim_pending(apply_audit=…)` +
`set_apply_audit`; new `workers/tofu_apply/`
(`main.py`, `gcs_fetch.py`, `Dockerfile`, `pyproject.toml`, `__init__.py`, `tests/`); new
`infra/cloudbuild.tofu-apply.yaml`; `setup_iac_backend.sh`/`setup_secrets.sh`/`setup_prod_project.sh`
IAM+secret blocks; new `tests/unit/test_iac_plan_denylist_lib.py`; CODEOWNERS; `iac/README.md`;
`docs/runbooks/tofu-apply.md`. **Untouched:** rollback `Approval`/`ApprovalStore`; the C3 `PlanApproval`
schema (consumed; only an additive store param); `iac/` HCL (no self-management); the C2 workflow; all
deployed services (the targeted Cloud Build does not touch payment-demo).

**Invariants:** (1) fail-closed at every gate; (2) claim-before-apply ordering (§3.6); (3) every
apply-time read from `signed_payload`; (4) no re-plan; (5) denylist re-run at apply independent of CI;
(6) fetch by pinned generation only + un-decoded bytes + bucket/prefix/basename allowlist; (7)
self-protection intact (worker never in `iac/`; denylist protects `driftscribe-tofu-apply`);
(8) `driftscribe_lib/` imports nothing from `tools/`; (9) rollback path byte-identical; (10) the worker
holds no GitHub token (merge + head-config delivery → C5); (11) fidelity asserts gate before any tofu
run.

**Non-goals:** C5 coordinator/approval-page/operator-auth + apply-then-merge + head-config delivery;
async apply; resource-set-changing applies (need C5 head-config); HMAC-key rotation; payment-demo
redeploy; least-privilege IAM (broad chosen).

---

## §10. Implementation order

1. **C4a** — denylist→lib promotion + shim (re-export all 15 constants) + `driftscribe-tofu-apply`
   entry + `test_iac_plan_denylist_lib.py` + fixture; denylist suite + `ruff` green.
2. **C4b** — additive `apply_audit` field + `claim_pending(apply_audit=…)` + `set_apply_audit` (TDD) → `gcs_fetch.py` (TDD) → tofu runner +
   fidelity asserts + temp-workdir materialization (TDD) → `main.py` endpoints (TDD). Full §7 matrix.
3. **C4c** — `Dockerfile` + structure test → `cloudbuild.tofu-apply.yaml` → IAM/secret bootstrap (+
   org-policy preflight) → `docs/runbooks/tofu-apply.md` → CODEOWNERS + `iac/README.md`.
4. Two-stage subagent review (spec then quality) + adversarial crypto/safety lens; `ruff`; **Codex
   completed-work review** on thread `019e733b`; admin-merge PR(s).
5. **Live** — §8 deploy + no-op smoke + negatives → redeploy internal ingress; record.

---

## §11. Codex review resolutions (thread `019e733b`)

**Blockers (all folded):** #1 config fidelity → §0.1 + §3.2 fidelity asserts + C4-live scoped to
baked-resource-set/no-op + general config delivery → C5; #2 plan materialization → §3.2 per-request
temp workdir + explicit path; #3 denylist service name → §2 adds `driftscribe-tofu-apply`; #4 IAM
escalation → §4.3/§6 corrected (editor includes SA-key/HMAC-key creation; excludes secret/KMS/IAM-
admin) + org-policy preflight; #5 wrong-head not a C4 capability → §3.3/§8 reframed (GitHub checks →
C5; C4 proves only the tamper case).

**Importants (folded):** #1 TOCTOU residual stated honestly (§3.2/§6); #2 `init -input=false -no-color`;
#3 crash-safe audit via atomic `apply_status="claimed"` (§3.3); #4 `apply_attempt_id` + `state pull`
serial/lineage instead of CLI `operation_name` (§5.3); #5 ingress is smoke-only → redeploy internal
(§4.2/§8); #6 GCS fetch validates uri+gen+prefix+basename (§3.1); #7 shim re-exports all 15 constants
(§2); #8 artifact deletion availability risk documented + retention recommended (§6).

**Nits (folded):** "before init/refresh/apply" wording (§3.2); env rename `PLAN_APPROVAL_HMAC_KEY`
(§3.3/§4.2); expiry-boundary `<` test pin (§7).

**Round 2 (thread `019e733b`) — folded:** (Blocker) raw editor's project-wide `iam.serviceAccounts.actAs`
+ Cloud Run job/service create = cross-SA workload impersonation → §4.3 defaults to a **hardened-broad
custom role** (drops actAs/key-creation; scoped `serviceAccountUser` on payment-demo), raw-editor+org-policy
documented as the operator's fast path with its accurate blast radius; the false "cannot read other
secrets/escalate" claims removed (§6). (Blocker) audit fields would break `PlanApproval.get()`'s
`PlanApproval(**data)` → use ONE additive `apply_audit: dict` field + `claim_pending(apply_audit=…)` +
`set_apply_audit`, collision-safe (§3.3/§5.3). (Important) cheap fail-closed **resource-set fidelity
guard** (refuse creates / addresses not in the baked declared set) added so baked-config apply is
genuinely correct, not scoped-by-convention (§0.1/§3.2). (Nit) §7 "before init/refresh/apply".
Materialization, denylist-name, and wrong-head resolutions confirmed sound by Codex round 2.

**Round 3 (thread `019e733b`) — folded:** (Blocker) `roles/run.admin` includes
`run.services.setIamPolicy` → hardened-broad redefined as a custom role with **no `*.setIamPolicy`/
IAM-policy-writer perms at all** (use `run.developer`-level, not run.admin), no project-wide actAs, no
key/HMAC creation, scoped `serviceAccountUser` on payment-demo (§4.3/§6 + the header summary updated).
(Importants) resource-set guard normalizes `count`/`for_each` instance suffixes + refuses `module.*`
fail-closed (§0.1/§3.2); the guard runs in `/propose` too so it never mints a dead approval (§3.3);
audit fix confirmed sound. (Nits) §0.1 "updates/no-ops" (deletes are denylist-denied); §10
`claim_pending(apply_audit=…)`. Codex confirmed the audit + materialization + guard direction sound;
only these IAM/wording items remained.
