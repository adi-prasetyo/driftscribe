# Runbook — the `tofu-apply` worker (Phase C4)

The `driftscribe-tofu-apply` Cloud Run service is the **sole mutator** of
DriftScribe-managed infra. It owns the plan-bound HMAC key and runs `tofu apply`
on a C2 plan artifact only after the full C3 approval + integrity + denylist +
fidelity + freshness gate passes. This runbook is the operator-side deploy +
the C4 live no-op smoke.

Design: `docs/plans/2026-05-29-infra-iac-phase-c4-tofu-apply.md`.

---

## 0. Model (what the worker does)

- `/propose` — the coordinator (C5) requests a proposal with the authenticated
  operator subject + the artifact locator. The worker independently fetches +
  verifies the artifact (integrity, denylist, fidelity) and mints a single-use,
  plan-bound, 15-minute HMAC approval. Returns the raw token **once**.
- `/apply` — verify token → check signed window + signed approver → **claim
  (single-use burn)** → re-fetch + re-verify the artifact → denylist re-run →
  fidelity gate → freshness gate (`tofu plan -refresh-only`, refuse on drift) →
  `tofu apply plan.tfplan` (the saved binary plan; never a re-plan).
- `/deny` — verify token → flip `pending → denied`.

**Fidelity boundary (important).** The worker bakes `iac/` from `main`. It
faithfully applies **no-ops and in-place updates of resources the baked `iac/`
declares**; it **refuses** a plan that touches an address the baked config doesn't
declare, a `module.*` address, a `replace`/destroy, or a provider/lockfile change.

**Creates (C6 — merge-then-apply-from-main).** A plan that *creates* a new top-level
resource is delivered by the **C6 two-step flow**: the coordinator merges the PR to
`main` FIRST, **the operator re-bakes this worker from the new `main`** (so the baked
`iac/` now declares the resource), then `/apply` admits the create — but ONLY after
the worker re-derives its baked `iac/`-tree hash and confirms it equals the approved
plan's `iac_tree_hash` (the C2 `iac-tree.json` sidecar, cross-checked against the
HMAC-signed metadata). A not-yet-re-baked worker refuses with `tree_mismatch_refused`
(HTTP 409). Modules + provider/lockfile changes stay refused. The re-bake is the same
`gcloud builds submit` deploy as §2; the operator drives it (the coordinator gets NO
build-trigger privilege). See `docs/runbooks/iac-apply-failure-recovery.md` §7 for the
create-class operating + recovery flow, and `GET /baked-iac-hash` to confirm the
re-bake before clicking Apply.

---

## 1. Pre-flight (in order — a missing step makes the first call 500/refuse)

1. **(editor IAM mode only)** Confirm the org policy
   `constraints/iam.disableServiceAccountKeyCreation` is **enforced** on
   `driftscribe-hack-2026`. The default **hardened** mode does not need this.
2. **IAM + SA** — run `infra/scripts/setup_iac_backend.sh` (idempotent). It
   creates `tofu-apply-sa` and grants (default = hardened-broad):
   `roles/run.developer` (project, broad Cloud Run apply — **no** `setIamPolicy`),
   `storage.objectAdmin` on the state bucket, `cryptoKeyEncrypterDecrypter` on
   the `tofu-state` key, `storage.objectViewer` on the artifact bucket,
   the custom `driftscribeTofuApplyPubsub` role (Pub/Sub topic+subscription
   create/get/list/update + `attachSubscription` — **no** `setIamPolicy`, **no**
   data-plane publish/consume, **no** delete; the codified least-privilege
   replacement for the temporary Phase-3 `roles/pubsub.editor`, whose removal is
   gated behind `SETUP_TOFU_APPLY_PUBSUB_CUSTOM=1`, bind-before-remove so a default
   re-run only ADDS the tight role),
   `datastore.user` **conditioned to the `plan-approvals` named Firestore database**
   (per-database isolation — the condition is
   `resource.name == "projects/driftscribe-hack-2026/databases/plan-approvals"`, so
   the `(default)` database is **denied** to the apply SA; the worker selects the DB
   via `PLAN_APPROVALS_DB=plan-approvals`). To use the broader `roles/editor` fast path instead:
   `TOFU_APPLY_IAM_MODE=editor infra/scripts/setup_iac_backend.sh` (accepts the
   documented actAs blast radius; requires step 1). **Caveat:** editor mode grants
   `roles/editor`, which carries UNCONDITIONED all-database Firestore access and
   therefore **FORFEITS the `plan_approvals` named-DB isolation** for the apply SA
   (the script prints a warning). Use the default hardened mode to keep the apply
   SA's Firestore access conditioned to `plan-approvals`.
3. **Secret + binds + apply grants** — run `infra/scripts/setup_secrets.sh`
   (idempotent). It creates `plan-hmac-key` (first-run-only, auto-generated),
   binds `secretmanager.secretAccessor` on it to `tofu-apply-sa`, adds the Cloud
   Build `actAs` on `tofu-apply-sa`, and — gated on payment-demo + the SA existing
   (§7b) — grants `tofu-apply-sa` resource-scoped `run.developer` on payment-demo
   **plus `iam.serviceAccountUser` (actAs) on payment-demo's dedicated runtime SA**
   (`payment-demo-runtime@driftscribe-hack-2026.iam.gserviceaccount.com` — a
   minimal SA with zero project roles; required for a non-no-op Cloud Run apply).
   **`rollback-agent-sa` ALSO gets `iam.serviceAccountUser` (actAs) on the same
   runtime SA** (§7b grants both): Cloud Run requires the caller to actAs the
   service's runtime SA for any update, so the rollback agent's `/execute`
   traffic-shift `update_service` needs it too. So run this **again after the
   deploy** (step 2) once payment-demo + the SA both exist.
4. **Verify the KMS binding exists** (else `tofu init` fails to decrypt with a
   confusing error):
   ```bash
   gcloud kms keys get-iam-policy tofu-state \
     --location=asia-northeast1 --keyring=driftscribe-tofu \
     --project=driftscribe-hack-2026 \
     --flatten=bindings --filter="bindings.members:tofu-apply-sa" \
     --format='value(bindings.role)'
   # expect: roles/cloudkms.cryptoKeyEncrypterDecrypter
   ```

## 2. Deploy (operator, by hand — no CI trigger)

```bash
gcloud builds submit \
  --config=infra/cloudbuild.tofu-apply.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) \
  --project=driftscribe-hack-2026
```

This builds the image (pinned tofu 1.12.0, checksum-verified; baked
`iac/` + providers), deploys `--no-allow-unauthenticated` (for the smoke) with
`--ingress=internal` pinned explicitly (`infra/cloudbuild.tofu-apply.yaml`), and
writes `OWN_URL` back. Then grant the caller `run.invoker` (re-run
`infra/scripts/setup_secrets.sh` — its invoker loop now includes
`driftscribe-tofu-apply`).

## 3. Live no-op smoke (the design §8 Phase-C exit proof)

With `iac/` at `main` (the payment-demo zero-change import):

1. Hand-open a trivial no-op `iac/` PR and run the C2 plan-builder
   (`workflow_dispatch` on `.github/workflows/iac.yml`, `pr_number=<N>`). It
   uploads a no-op plan + posts the artifact URIs + generations in a PR comment.
2. Drive the flow. **Auth matters** — the worker enforces TWO application-layer
   checks (`driftscribe_lib/auth.py`): the ID token's `aud` must equal the worker
   URL, and its `email` claim must be in `ALLOWED_CALLERS`, which is **only the
   coordinator SA** (`driftscribe-agent@…`). A bare `gcloud auth
   print-identity-token` (operator identity, wrong audience) 401s/403s. So mint an
   **audience-bound token AS the coordinator SA** (you need
   `roles/iam.serviceAccountTokenCreator` on `driftscribe-agent`; the worker must
   already hold `run.invoker` for you — granted by `setup_secrets.sh`):
   ```bash
   PROJECT=driftscribe-hack-2026
   URL=$(gcloud run services describe driftscribe-tofu-apply --region=asia-northeast1 --format='value(status.url)')
   COORD=driftscribe-agent@$PROJECT.iam.gserviceaccount.com
   TOK=$(gcloud auth print-identity-token \
          --impersonate-service-account="$COORD" --audiences="$URL" --include-email)
   # /propose — locator from the PR comment; approver = the coordinator subject
   curl -fsS -X POST "$URL/propose" -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d "{\"artifact_uri_metadata\":\"gs://.../metadata.json\",\"generation_metadata\":\"<gen>\",\"approver\":\"$COORD\"}"
   # → {approval_id, approval_token, expires_at}
   curl -fsS -X POST "$URL/apply" -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d '{"approval_id":"<id>","approval_token":"<token>"}'
   # → {status:"applied", apply_attempt_id}; tofu apply executes ZERO actions (no-op)
   ```
   This proves deploy + IAM + HMAC + fetch + integrity + denylist + fidelity +
   freshness + apply end-to-end with **no real infra change**.
   > The `approver` here is the coordinator SA, so at C4 the signed `approver` is
   > tamper-evident audit, NOT human non-repudiation — that arrives when C5
   > forwards a trusted operator identity that `/apply` verifies (C3 §4 residual gap).
3. **Negatives** (each should be rejected): a control-plane/IAM/delete PR (worker
   denylist re-run → 422), a tampered-payload approval (HMAC mismatch → 403), an
   expired/replayed/wrong-approver approval (403). ("Wrong PR head" is a C5/CI
   check, not a C4 capability — C4 proves only the tamper case via HMAC.)

## 4. Harden after the smoke (do NOT skip)

Redeploy with private ingress and drop any temporary public-invoker access:

```bash
gcloud run services update driftscribe-tofu-apply \
  --region=asia-northeast1 --ingress=internal --project=driftscribe-hack-2026
```

Under `--ingress=internal` the coordinator must reach the worker from inside the
VPC (serverless VPC connector / `internal-and-cloud-load-balancing`) — that
egress path is a **C5** concern; verify it before relying on coordinator-driven
applies.

---

## 5. Operating notes / footguns

- **Claim burns on any post-claim failure.** `/apply` claims (single-use) BEFORE
  the heavy re-checks, so a transient failure (GCS blip, lock timeout, tofu
  error) leaves the approval `used` with `apply_audit.phase` recording the stage.
  The operator must **re-propose** a fresh approval. This is intended fail-closed
  behavior.
- **Partial / failed apply.** If `tofu apply` fails, the worker runs a read-only,
  fail-closed post-failure diagnosis: if it can PROVE state stayed clean it
  returns 502 + `apply_audit.phase="failed"` (no reconcile needed); otherwise it
  returns 502 + `apply_audit.phase="failed_state_suspect"` with a bounded
  diagnostic (serials, refresh drift) — the failed apply may have persisted
  **partial state**, so a **state reconcile is required before any retry**. The
  worker does NOT auto-rollback/auto-reconcile — that's a human decision. Full
  procedure: **`docs/runbooks/iac-apply-failure-recovery.md`**.
- **Lock contention.** The GCS backend serializes via an atomic `.tflock`. The
  worker uses a finite `-lock-timeout=120s` then fails closed. **Never
  auto-force-unlock** — a stuck lock from a crashed apply is cleared by hand only
  after confirming no apply is in flight:
  `tofu -chdir=iac force-unlock <LOCK_ID>` (operator, deliberate). **A crash mid
  `plan -refresh-only` orphans the lock** (the lock is acquired before the apply
  proper). Observed live in the C4 smoke: an OOM-killed container (see memory note
  below) left `gs://…-tofu-state/prod/default.tflock`; the next `/apply` then
  failed the freshness gate with `tofu refresh-only failed (exit 1)` (a lock error
  surfacing as exit 1, not drift's exit 2). Recovery: confirm the lock's
  `Created`/`Who` match the dead attempt, then `force-unlock <LOCK_ID>`.
- **Memory.** The apply path spawns `tofu` + the google provider plugin, which
  peaked at **539 MiB** during the live smoke — past Cloud Run's **512 MiB
  default**, OOM-killing the container (HTTP 503) and orphaning the lock above.
  The deploy pins **`--memory=2Gi --cpu=2`** (infra/cloudbuild.tofu-apply.yaml).
  A real (non-no-op) apply uses more than a no-op; keep ≥2Gi and watch
  `Memory limit … exceeded` in the logs if iac/ grows.
- **Audit.** Every terminal `/apply` writes `apply_audit` to the `plan_approvals`
  doc: `apply_attempt_id`, `phase`, exit codes, `applied_at`, and the observed
  state `serial`/`lineage` (proves which state snapshot the apply ran against).
- **Version pinning.** The baked `tofu` version + `iac/.terraform.lock.hcl` must
  match the C2 plan-builder's pins, or the fidelity gate refuses every apply.
  Bump in lockstep with `.github/workflows/iac.yml`.
