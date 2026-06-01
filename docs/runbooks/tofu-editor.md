# Runbook — the `tofu-editor` worker (Phase D)

The `driftscribe-tofu-editor` Cloud Run service is the provision workload's
**HCL-authoring** worker. The coordinator invokes it (canonical endpoint
`/open-pr`) to author `infra/` (`iac/*.tf`) **text** and open a pull request on
`adi-prasetyo/driftscribe`. It is a **write surface — but never an infra
mutator**: it produces PR text only and never runs `tofu`. The actual
`tofu plan`/`tofu apply` step lives entirely in the separate `tofu-apply`
worker (the sole mutator), gated by the full C2/C3/C4 approval pipeline. This
runbook is the operator-side deploy + IAM wiring for the editor.

Design: `docs/plans/2026-06-01-infra-iac-phase-d-agent-authoring.md` (Task D3-1).

> **Target project is `driftscribe-hack-2026` (prod), region
> `asia-northeast1`.** Confirm `PROJECT` and the active gcloud account before
> running anything below. You (the operator) run these steps from a shell that
> holds `roles/owner` (or equivalent admin) on `driftscribe-hack-2026`. **None
> of this runs in CI** — like `tofu-apply` / `infra-reader`, the editor is
> never auto-deployed.

Cross-references:

- Worker source + build: `workers/tofu_editor/main.py`, `workers/tofu_editor/Dockerfile`, `infra/cloudbuild.tofu-editor.yaml`
- IAM (positive + negative space): `docs/architecture/iam-matrix.md` (`tofu-editor-sa@…` row)
- Bootstrap script (SA, secret, binds, invoker grant): `infra/scripts/setup_secrets.sh`
- The downstream apply worker + its runbook: `docs/runbooks/tofu-apply.md`
- Apply-failure / create-class recovery: `docs/runbooks/iac-apply-failure-recovery.md`

---

## 0. Model (what the worker does — and what it explicitly does NOT do)

- `/open-pr` — the coordinator (provision workload) requests that the editor
  author one or more `iac/*.tf` files and open a PR. The worker **re-validates
  every request** before writing:
  - an **`iac/`-only file gate** (it may write only `iac/` HCL files; never a
    foundation/provider/backend file, never a non-`iac/` path, never a secret
    file);
  - the shared **AGENT-mode static gate** (`tools/iac_static_gate.py`),
    including the **secret-authoring ban** (D1-6) — it refuses to author
    `google_secret_manager_secret_*` resources or inline `secret_data` /
    credential attributes, keeping secret material operator-only.
  It then opens a PR (label `driftscribe-infra`) using its write-scoped
  fine-grained PAT.
- It **never runs `tofu`**, holds **no** OpenTofu state / KMS / `plan-hmac-key`
  access, and holds **no** project-level GCP role. Its entire authority is the
  single GitHub PAT (`tofu-editor-github-pat`). A compromised editor can open a
  junk PR, but it cannot mutate infra and it cannot author a control-plane / IAM
  / secret-material change that would later apply (the static gate refuses it,
  and even if a PR slipped through, the C1 denylist + C3 approval + C4
  apply-worker re-verification would refuse the apply).
- **Parallel fan-out (Phase D5) does NOT change this worker.** When the
  coordinator decomposes a multi-independent-file request into parallel
  sub-agent slices, it still converges them into **exactly ONE** `/open-pr`
  call carrying the merged `files` list (one commit → one PR). The fan-out is
  entirely **coordinator-internal** — **no new SA, secret, IAM, or worker** —
  so the editor receives an `/open-pr` request byte-identical to the
  single-agent path, every gate above (`iac/`-only, AGENT-mode static gate,
  secret ban) and the whole downstream flow (§7) are unchanged, and the
  sub-agents themselves hold **no** PR/apply/mutation tool. Shipping D5 is just
  a coordinator image rebuild (no step in this runbook changes). See
  `docs/plans/2026-06-01-infra-iac-phase-d5-fanout.md`.

---

## 1. Mint the write-scoped fine-grained GitHub PAT (operator)

The editor's only secret material is a **fine-grained** GitHub PAT — this is the
credential it uses to open PRs.

1. https://github.com/settings/personal-access-tokens/new
2. **Repository access:** select **ONE** repo — `adi-prasetyo/driftscribe`.
3. **Permissions:**
   - `Contents`: **Read and write**
   - `Pull requests`: **Read and write**
4. **Nothing else** — **no** admin, **no** Issues, **no** Actions/workflow,
   **no** account-level scope, **no** second repo (in particular not
   `driftscribe-e2e-target`).

This PAT is **DISTINCT** from the coordinator's `github-pat`, from the drift
docs / upgrade-docs PATs, and from `tofu-apply` (which holds no GitHub
credential at all). It backs **only** the editor's `/open-pr` flow.

## 2. Create `tofu-editor-sa` + the `tofu-editor-github-pat` secret

The bootstrap script is idempotent and owns both. Run it **with the PAT as the
8th positional arg** (preserving the earlier args):

```bash
PROJECT=driftscribe-hack-2026
infra/scripts/setup_secrets.sh \
  "$PROJECT" "$GH_TOKEN" "$DOCS_PAT" "$WEBHOOK_URL" "$DEV_KEY" \
  "$UPGRADE_READER_PAT" "$UPGRADE_DOCS_PAT" "$TOFU_EDITOR_PAT"
```

This:

- creates `tofu-editor-sa` (one of the worker SAs this script owns; created in
  the §4 SA loop, idempotent);
- creates the `tofu-editor-github-pat` secret and adds your PAT as a version
  (describe-then-create — if you omit the 8th arg the script prints
  instructions and **skips** creating the secret, so re-run later with the
  value);
- binds **per-secret** `roles/secretmanager.secretAccessor` on
  `tofu-editor-github-pat` to `tofu-editor-sa` **only** (no project-scope grant,
  no second secret).

> If you prefer to do the secret by hand (equivalent to the script's block):
> ```bash
> gcloud secrets create tofu-editor-github-pat \
>   --project="$PROJECT" --replication-policy=automatic
> printf '%s' "$TOFU_EDITOR_PAT" | gcloud secrets versions add tofu-editor-github-pat \
>   --project="$PROJECT" --data-file=-
> gcloud secrets add-iam-policy-binding tofu-editor-github-pat \
>   --project="$PROJECT" \
>   --member="serviceAccount:tofu-editor-sa@${PROJECT}.iam.gserviceaccount.com" \
>   --role=roles/secretmanager.secretAccessor
> ```

> **Re-run is safe.** Every create is describe-gated; every IAM binding is
> server-side idempotent. Run the script before the first build (the invoker
> grant in §4 below is a no-op until the service exists) and again after.

## 3. Deploy the worker (operator, by hand — no CI trigger)

```bash
PROJECT=driftscribe-hack-2026
gcloud builds submit \
  --config=infra/cloudbuild.tofu-editor.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) \
  --project="$PROJECT"
```

`infra/cloudbuild.tofu-editor.yaml` builds + pushes **only** the
`driftscribe-tofu-editor` image (it does **not** rebuild payment-demo, the
coordinator, or any other worker — that would break the Phase A zero-diff), then
deploys it:

- `--no-allow-unauthenticated` (ID-token auth required);
- `--service-account=tofu-editor-sa@$PROJECT_ID.iam.gserviceaccount.com`;
- `--set-secrets=GITHUB_TOKEN=tofu-editor-github-pat:latest`;
- `--set-env-vars=IAC_EDITOR_TARGET_REPO=${_IAC_EDITOR_TARGET_REPO},OWN_URL=…,ALLOWED_CALLERS=${_ALLOWED_CALLER}@$PROJECT_ID.iam.gserviceaccount.com`
  where `_IAC_EDITOR_TARGET_REPO=adi-prasetyo/driftscribe` (the single repo the
  worker may write to, re-validated against the request body) and
  `_ALLOWED_CALLER=driftscribe-agent` (the **only** caller the in-app
  `ALLOWED_CALLERS` allowlist accepts);
- `--max-instances=1 --concurrency=1` (PR creation serialized);
- a final post-deploy step resolves the assigned URL and writes it back into
  `OWN_URL` (fail-closed if it can't resolve — `verify_caller` checks the
  audience against `OWN_URL`).

## 4. Grant the coordinator `run.invoker` on `driftscribe-tofu-editor`

There are **two** independent gates on a coordinator→worker call, and you need
both:

1. **Cloud Run platform IAM (`roles/run.invoker`).** The worker is
   `--no-allow-unauthenticated`, so Cloud Run rejects the call at the admission
   layer unless the **coordinator's SA holds `run.invoker` on
   `driftscribe-tofu-editor`**. Apply it idempotently by **re-running
   `setup_secrets.sh`** — its per-service invoker loop now includes
   `driftscribe-tofu-editor` and is gated on the service existing, so run it
   *after* the worker is deployed. Or apply it directly:
   ```bash
   PROJECT=driftscribe-hack-2026
   gcloud run services add-iam-policy-binding driftscribe-tofu-editor \
     --project="$PROJECT" --region=asia-northeast1 \
     --member="serviceAccount:driftscribe-agent@${PROJECT}.iam.gserviceaccount.com" \
     --role="roles/run.invoker"
   ```
2. **App-level caller allowlist (`ALLOWED_CALLERS`).** Already set by the deploy
   step (§3) — the worker allowlists the **coordinator's** SA and `verify_caller`
   rejects any other caller, fail-closed.

## 5. Set `TOFU_EDITOR_URL` on the coordinator (incremental redeploy)

The coordinator reaches the editor via the `TOFU_EDITOR_URL` env var. Set it via
an **incremental** `--update-env-vars` that preserves all other coordinator
env / secrets / SA (the `infra-reader` rollout is the template — see
`docs/runbooks/infra-reader.md` §4):

```bash
PROJECT=driftscribe-hack-2026
URL=$(gcloud run services describe driftscribe-tofu-editor \
  --region=asia-northeast1 --project="$PROJECT" --format='value(status.url)')
gcloud run services update driftscribe-agent \
  --region=asia-northeast1 --project="$PROJECT" \
  --update-env-vars=TOFU_EDITOR_URL="$URL"
```

> **This also clears the reachability warning.** `TOFU_EDITOR_URL` is in the
> coordinator's worker-URL fan-out (`worker_client._WORKER_URL_ENV`), so until it
> is set, the coordinator's `GET /iac-apply/reachability` diagnostic reports the
> editor **unreachable** (introduced in D2-1 — non-fatal, but noisy). Setting it
> here in the same incremental redeploy resolves that.

> **Coordinator image must already carry `open_infra_pr_tool`.** The
> coordinator needs an image that contains the provision-workload tool +
> `provision` request Literals (D2-2 / D2-4). If the running coordinator
> predates them, ship the coordinator image first (via the coordinator deploy
> path), then set `TOFU_EDITOR_URL`.

## 6. Harden the worker to `--ingress=internal` (after a first verified call)

Once a first `/open-pr` call has verified the path end-to-end, redeploy with
private ingress:

```bash
gcloud run services update driftscribe-tofu-editor \
  --region=asia-northeast1 --ingress=internal --project=driftscribe-hack-2026
```

Under `--ingress=internal` the coordinator must reach the worker from inside the
VPC (Direct VPC egress / serverless VPC connector) — the same egress path the
`tofu-apply` worker relies on (a C5 concern). Verify that path before relying on
coordinator-driven authoring.

---

## 7. The downstream flow (what an opened PR becomes)

The editor's job ends when the PR is open. From there the standard IaC pipeline
takes over — the editor adds **no new apply-time trust**:

1. **Editor opens an `infra/` PR** (label `driftscribe-infra`) on
   `adi-prasetyo/driftscribe`.
2. **CI static gate (AGENT mode)** runs on the PR — the same gate the worker
   ran in-process, re-run in CI as the authoritative check.
3. **Dispatch the C2 plan-builder** on the PR number:
   `gh workflow run iac.yml -f pr_number=<N>` (`workflow_dispatch` on
   `.github/workflows/iac.yml`). It uploads the trusted plan triplet and posts
   the artifact URIs + generations in a PR comment.
4. **Review + approve** at the coordinator's `/iac-approvals/<N>` page.
5. **C4 applies** — the `tofu-apply` worker re-fetches + re-verifies the plan
   (integrity, denylist, fidelity, freshness) and runs `tofu apply` only after
   the plan-bound HMAC approval passes. See `docs/runbooks/tofu-apply.md`.

> **Create-class PRs (a new top-level resource) additionally need a C6 operator
> re-bake before apply.** The apply worker bakes `iac/` from `main`; a plan that
> *creates* a resource the baked config doesn't yet declare is refused
> (`tree_mismatch_refused`, HTTP 409) until the operator merges the PR and
> re-bakes the apply worker from the new `main`. See
> `docs/runbooks/iac-apply-failure-recovery.md` §7 (create-class operating +
> recovery flow) and `docs/runbooks/tofu-apply.md` §0.

---

## 8. CODEOWNERS note

`infra/scripts/` is a CODEOWNERS-protected path — changes to
`infra/scripts/setup_secrets.sh` (the SA / secret / invoker wiring above)
require `@adi-prasetyo` review on the PR.
