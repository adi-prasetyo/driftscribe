# Phase 3 — Checkout build-out: execution plan (the live dogfood loop)

**Date:** 2026-06-04
**Status:** PLAN (pre-Codex-review, pre-operator-go-ahead). Implements §5 Phase 3 of
`docs/plans/2026-06-03-driftscribe-checkout-demo-and-infra-graph-design.md`.
Phase 1 (graph) + Phase 2 (resolver) are SHIPPED + LIVE. This phase **mutates prod
infra** through DriftScribe's own author → C2 → approve → C6-rebake → apply → reader-rebake loop.
**Author:** Claude (Opus 4.8). Mechanics verified against current code 2026-06-04 (4-reader workflow).

---

## 0. Verified live state (2026-06-04, read-only)

| Thing | Value |
|---|---|
| gcloud / project / ADC | `theghostsquad00@gmail.com` · `driftscribe-hack-2026` · ADC present |
| git HEAD | `8d05d95` (Phase 2 merged), tree clean |
| Coordinator | `driftscribe-agent-00037-lcl` @ 100%; `USE_ADK=true`; `COORDINATOR_ORIGIN=https://driftscribe.adp-app.com`; CF-Access wired |
| Apply loop env | `IAC_REQUIRED_CHECKS=static-gate,tofu,lint-test`; `IAC_MERGE_METHOD=squash`; `GITHUB_REPO=adi-prasetyo/driftscribe`; TOFU_APPLY_URL / TOFU_EDITOR_URL / INFRA_READER_URL all set |
| tofu-apply worker | `00013-6gs`, `--ingress=internal`, **`IAC_OPERATOR_AUTH_MODE=enforce`**, ALLOWED_CALLERS=`driftscribe-agent@` |
| tofu-editor worker | `00002-dfb` (authoring worker, single-repo write PAT) |
| infra-reader | `00006-x9q` @ `:8d05d95` (Phase 2 resolver baked in) |
| payment-demo URL | `https://payment-demo-u272wv52kq-an.a.run.app` |
| Clean slate | no `…-assets` bucket, no `order-events` topic, no `storefront`/`orders-worker` services, no `storefront-sa`/`orders-worker-sa`, no `payment-api-key` secret |
| GH plan-builder secrets | `GCP_WIF_PROVIDER`, `GCP_TOFU_PLAN_BUILDER_SA`, `GCP_TOFU_STATE_KMS_KEY` set (C2 proven live) |
| **Pipeline-SA IAM (gap!)** | `tofu-apply-sa` = only `run.developer` + `datastore.user` → **cannot create buckets/Pub-Sub**; `tofu-plan-builder` = only `run.viewer` → **cannot refresh new types**. Fixed in Track B-0. |
| APIs | `pubsub.googleapis.com`, `storage*.googleapis.com`, `run`, `secretmanager`, `cloudasset` all **enabled** (no API-enable step needed) |

**Decisive consequence of `enforce` mode:** the apply path runs *only* through the
browser approval at `https://driftscribe.adp-app.com/iac-approvals/<N>` (Cloudflare
Access SSO → the human's email becomes the signed approver, re-verified by the worker).
The agent cannot mint that JWT. **Each apply = one operator browser action (or two for
create-class: Approve, then Apply after the rebake).** No worker auth-mode flip — that
would be a security regression.

---

## 1. The per-PR loop (CREATE-class, which all Track-A resources are)

Verified end-to-end against current code. Roles: **[A]** = agent (me, via Bash/gcloud/curl);
**[U]** = user (browser, Cloudflare Access).

1. **[A] Author** — `POST /chat {"prompt": "<spec>", "workload": "provision"}` with
   `X-DriftScribe-Token` to the coordinator's raw Cloud Run URL (bypasses CF Access for
   the API). Fan-out decomposes → N slice-authors → ONE PR on `infra/<slug>-<ts>-<hex>`,
   base `main`, label `driftscribe-infra`, `iac/`-only. → **verify the PR # on GitHub
   (never trust an agent-reported #/SHA — fabrication seen).**
2. **[A] C2 plan-builder** — `gh workflow run iac.yml -f pr_number=<N>`. WIF-auth job runs
   `tofu plan`, the C1 denylist, uploads the `c2.v1` triplet + `c6.v1` iac-tree sidecar to
   `gs://driftscribe-hack-2026-tofu-artifacts/pr-<N>/…`, posts a `tofu show` PR comment.
   → **wait for the run green + the comment to appear.** Re-dispatch = same command.
3. **[U] Approve** — open `https://driftscribe.adp-app.com/iac-approvals/<N>`, reload so it
   binds the newest C2 comment, click **Approve**. Because the plan **creates** a resource,
   the coordinator takes the **C6 create-class path**: it **squash-merges the PR to main
   first**, records `waiting_for_rebake`, and the page tells you to rebake then come back.
4. **[A] C6 re-bake the tofu-apply worker** — `git fetch origin main && git checkout main &&
   git pull`, then build from the **exact squash-merge commit the approval page shows**
   (don't let an unrelated merge land in the window — I control the sequence and freeze other
   `iac/` merges): `gcloud builds submit --config=infra/cloudbuild.tofu-apply.yaml
   --substitutions=_TAG=$(git rev-parse --short HEAD) --project=driftscribe-hack-2026`.
   The worker bakes `iac/` into its image; `_verify_iac_tree_or_raise` admits the create only
   when its baked iac-tree hash == the approved `iac_tree_hash` (else HTTP 409
   `tree_mismatch_refused`; if main advanced, the resume surfaces a re-plan instruction —
   `agent/main.py:3192`). → **verify** `GET /baked-iac-hash` matches the approved hash.
5. **[U] Apply** — reload `/iac-approvals/<N>`, click **Apply**. The resume drives
   propose → apply on the worker (sole mutator): re-verify artifact + denylist + fidelity +
   freshness → `tofu apply plan.tfplan` → `{status: applied}`, audit row written.
6. **[A] Re-bake the infra-reader** — `gcloud builds submit
   --config=infra/cloudbuild.infra-reader.yaml
   --substitutions=_TAG=$(git rev-parse --short HEAD),_IAC_SNAPSHOT_SHA=$(git rev-parse HEAD)
   --project=driftscribe-hack-2026`. (Plain worker deploy; also re-wires INFRA_READER_URL.
   **No coordinator redeploy in Phase 3** — the coordinator doesn't bake `iac/`.)
7. **[A/U] Verify green** — `/describe` shows the new resource `iac=True`; the operator-UI
   Infrastructure panel colors the node managed. (CAI is eventually consistent — the
   delayed-refetch logic rides out lag.)

**Two distinct re-bakes, never conflated:** (4) tofu-apply rebake = *mandatory before a
create-class apply* (the tree-hash gate). (6) infra-reader rebake = *graph coloring only*.
**Never** use the full `infra/cloudbuild.yaml` (it rebuilds payment-demo at a fresh tag and
breaks the Phase-A zero-diff).

**Fail-closed facts to respect:** `/apply` burns the approval *before* the heavy checks —
any post-claim failure (lock 423, drift 409, tofu 502) leaves it `used`; re-click Approve to
re-propose. A create-class `/apply` 502 is **terminal `failed_state_suspect`** (a half-done
create can leave a live resource absent from state) — freeze + reconcile, never blind-retry.
An orphaned GCS state lock → operator `tofu force-unlock` (never auto).

---

## 2. Resource plan & batching

**Recommended batching: 2 apply-loops (not 4), interleaved with Track B.** Rationale: each
loop costs 1 user-Approve + 1 user-Apply + 2 cloud builds; batching halves the human-loop
while still proving fan-out (PR1 is multi-slice), create-class C6 apply, and managed-vs-drift
coloring. Storefront/worker depend on Track-B (SA + secret) existing first, so they form a
natural second batch.

### Order of operations

- **Track B-0 (pipeline-enabling grants, operator gcloud, PRE-PR1) — REQUIRED, else apply
  fails:** grant `tofu-apply-sa` the rights to *create* the new resource types, and
  `tofu-plan-builder` the rights to *refresh* them in `tofu plan`. (Found via Codex review;
  the apply SA was hardened to Cloud-Run-only.)
- **PR1 (Track A — infra primitives, no demo-SA dep):** `assets` bucket + `order-events`
  topic + `orders-sub` subscription. → loop §1. After: 3 new **green** nodes.
- **Reconcile checkpoint (post-PR1):** confirm a clean zero-diff `tofu plan` (GCS/Pub-Sub
  return server-populated defaults; the freshness/benign-drift classifier is Cloud-Run-only,
  so any genuine refresh drift on PR1 resources would 409 PR2's apply). Reconcile the HCL
  (add fields or `lifecycle.ignore_changes`) via a tiny follow-up PR through the same loop if
  the plan isn't empty. (Codex flagged; same pattern as the payment-demo adoption reconcile.)
- **Track B-1 (demo bootstrap, operator gcloud, PRE-PR2):** `payment-api-key` secret +
  version; `storefront-sa` + `orders-worker-sa`; `storefront-sa` secretAccessor on the
  secret; **`tofu-apply-sa` `serviceAccountUser` (actAs) on BOTH new runtime SAs** (Cloud
  Run create fails without actAs — Codex caught this; mirrors payment-demo-runtime).
- **PR2 (Track A — services, depend on Track B-1):** `storefront` (refs payment-demo URL +
  the secret via `value_source`, runs as `storefront-sa`) + `orders-worker` (runs as
  `orders-worker-sa`). → loop §1. After: 2 new **green** nodes.
- **Track B-2 (demo IAM wiring, operator gcloud, POST-PR2):** run.invoker storefront-sa→payment-demo;
  pubsub.publisher storefront-sa→order-events; pubsub.subscriber orders-worker-sa→orders-sub;
  storage.objectViewer storefront-sa→assets bucket. → SAs render **amber** (not-in-iac).
- **(Optional finale) Drift demo** — deferred decision (see §6).

### Track A HCL (faithful to `iac/c6e_probe.tf` + `iac/cloudrun.tf`; my reference — the agent authors its own, I diff against this)

**PR1 — `iac/checkout_assets.tf`:**
```hcl
resource "google_storage_bucket" "checkout_assets" {
  name     = "driftscribe-hack-2026-assets"   # BARE literal (resolver identity); not a -tofu-* protected suffix
  project  = var.project_id
  location = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels = { purpose = "checkout-demo-assets", managed-by = "driftscribe-iac" }
}
```

**PR1 — `iac/checkout_events.tf` (topic + sub in ONE file/slice so the ref resolves cleanly):**
```hcl
resource "google_pubsub_topic" "order_events" {
  project = var.project_id          # REQUIRED — resolver ignores provider config
  name    = "order-events"          # → projects/driftscribe-hack-2026/topics/order-events
  labels  = { managed-by = "driftscribe-iac" }
}
resource "google_pubsub_subscription" "orders_sub" {
  project              = var.project_id
  name                 = "orders-sub"               # → projects/…/subscriptions/orders-sub
  topic                = google_pubsub_topic.order_events.id  # ref OK (not an identity field)
  ack_deadline_seconds = 20
  labels               = { managed-by = "driftscribe-iac" }
}
```

**PR2 — `iac/checkout_storefront.tf`:**
```hcl
resource "google_cloud_run_v2_service" "storefront" {
  name     = "storefront"      # not a control-plane name
  location = var.region        # REQUIRED for identity
  project  = var.project_id    # REQUIRED for identity
  ingress  = "INGRESS_TRAFFIC_ALL"
  template {
    service_account = "storefront-sa@${var.project_id}.iam.gserviceaccount.com"  # Track-B SA (must pre-exist)
    scaling { min_instance_count = 0  max_instance_count = 1 }
    containers {
      image = "gcr.io/cloudrun/hello"
      env { name = "PAYMENT_DEMO_URL"  value = "https://payment-demo-u272wv52kq-an.a.run.app" }
      env {
        name = "PAYMENT_API_KEY"
        value_source { secret_key_ref { secret = "payment-api-key"  version = "latest" } }  # ref existing secret (allowed)
      }
    }
  }
  lifecycle { ignore_changes = [client, client_version, scaling] }
}
```

**PR2 — `iac/checkout_orders_worker.tf`:**
```hcl
resource "google_cloud_run_v2_service" "orders_worker" {
  name     = "orders-worker"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"
  template {
    service_account = "orders-worker-sa@${var.project_id}.iam.gserviceaccount.com"  # Track-B SA (must pre-exist)
    scaling { min_instance_count = 0  max_instance_count = 1 }
    containers {
      image = "gcr.io/cloudrun/hello"
      env { name = "ORDERS_SUBSCRIPTION"  value = "orders-sub" }
    }
  }
  lifecycle { ignore_changes = [client, client_version, scaling] }
}
```

**Authoring prompts must be prescriptive** (names, image, env, secret id, SA email, and
"set explicit `project` and `location` in every resource body to match repo conventions") —
the resolver's correctness traps (bare bucket literal, no embedded `${…}` interpolation in
the bucket name, explicit project/location) are subtle; the C2 plan + gate catch mistakes,
and I re-prompt if the authored HCL would false-drift.

### Track B gcloud (operator-only; agent FORBIDDEN to author these — denylist + static gate)

```bash
PROJECT=driftscribe-hack-2026 ; REGION=asia-northeast1
APPLY_SA=tofu-apply-sa@${PROJECT}.iam.gserviceaccount.com
PLAN_SA=tofu-plan-builder@${PROJECT}.iam.gserviceaccount.com

# ===== Track B-0 (PRE-PR1) — pipeline-enabling grants (REQUIRED; apply/plan fail without them) =====
# tofu-apply-sa (sole mutator) must CREATE + manage + read the new types — but WITHOUT regressing the
# careful "no setIamPolicy" posture that run.developer was chosen for (Codex). So: a CUSTOM storage
# bucket role (create/get/list/update, NO setIamPolicy) instead of roles/storage.admin; pubsub.editor is
# OK (verified: it has topics/subscriptions.create but NO *.setIamPolicy). Bucket/topic create needs
# project-level perms (can't resource-scope a not-yet-existing resource); the plan denylist still guards
# control-plane buckets/services at the PLAN layer.
gcloud iam roles create driftscribeTofuApplyStorage --project="$PROJECT" \
  --title="DriftScribe tofu-apply storage (bucket create/manage, no IAM)" --stage=GA \
  --permissions="storage.buckets.create,storage.buckets.get,storage.buckets.list,storage.buckets.update"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${APPLY_SA}" \
  --role="projects/${PROJECT}/roles/driftscribeTofuApplyStorage" --condition=None
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${APPLY_SA}" \
  --role="roles/pubsub.editor" --condition=None   # temporary for Phase 3; custom create/update role is the tighter future option
# tofu-plan-builder (CI, READ-ONLY refresh) — `tofu plan` refreshes all state, so it needs read on the new types.
# storage.bucketViewer (verified: buckets.get+list, no IAM, project-grantable) — NOT legacyBucketReader (bucket-scoped only).
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${PLAN_SA}" --role="roles/pubsub.viewer"        --condition=None
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${PLAN_SA}" --role="roles/storage.bucketViewer" --condition=None

# ===== Track B-1 (PRE-PR2) — demo bootstrap =====
# secret + version (value never touches the agent)
gcloud secrets create payment-api-key --project="$PROJECT" --replication-policy=automatic
printf '%s' "$PAYMENT_API_KEY" | gcloud secrets versions add payment-api-key --project="$PROJECT" --data-file=-

# service accounts (describe-then-create, idempotent)
gcloud iam service-accounts create storefront-sa    --project="$PROJECT" --display-name="DriftScribe storefront-sa"
gcloud iam service-accounts create orders-worker-sa --project="$PROJECT" --display-name="DriftScribe orders-worker-sa"

# per-secret accessor ONLY (NO project-wide secretAccessor — hard repo invariant)
gcloud secrets add-iam-policy-binding payment-api-key --project="$PROJECT" \
  --member="serviceAccount:storefront-sa@${PROJECT}.iam.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"

# tofu-apply-sa must be able to actAs the new runtime SAs (Cloud Run create REQUIRES this; mirrors setup_secrets.sh:613)
gcloud iam service-accounts add-iam-policy-binding "storefront-sa@${PROJECT}.iam.gserviceaccount.com" \
  --project="$PROJECT" --member="serviceAccount:${APPLY_SA}" --role="roles/iam.serviceAccountUser"
gcloud iam service-accounts add-iam-policy-binding "orders-worker-sa@${PROJECT}.iam.gserviceaccount.com" \
  --project="$PROJECT" --member="serviceAccount:${APPLY_SA}" --role="roles/iam.serviceAccountUser"

# ===== Track B-2 (POST-PR2) — demo IAM wiring (resource-scoped, never project-wide) =====
gcloud run services add-iam-policy-binding payment-demo --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:storefront-sa@${PROJECT}.iam.gserviceaccount.com" --role="roles/run.invoker"
gcloud pubsub topics add-iam-policy-binding order-events --project="$PROJECT" \
  --member="serviceAccount:storefront-sa@${PROJECT}.iam.gserviceaccount.com" --role="roles/pubsub.publisher"
gcloud pubsub subscriptions add-iam-policy-binding orders-sub --project="$PROJECT" \
  --member="serviceAccount:orders-worker-sa@${PROJECT}.iam.gserviceaccount.com" --role="roles/pubsub.subscriber"
gcloud storage buckets add-iam-policy-binding gs://driftscribe-hack-2026-assets \
  --member="serviceAccount:storefront-sa@${PROJECT}.iam.gserviceaccount.com" --role="roles/storage.objectViewer"
```

**Why storefront runs as `storefront-sa` (not default compute SA):** least-privilege +
it's the managed-vs-drift teaching point (the SA is amber/operator-bootstrap; the service is
green/agent-authored). Cloud Run validates at create time that (a) the runtime SA exists and
(b) it can access the mounted secret — so the SA + secret + accessor MUST precede the PR2 apply.

---

## 3. Per-step verification

- **Author:** `gh pr view <N> --json number,headRefName,labels,files` — confirm `infra/…`
  branch, `driftscribe-infra` label, only the expected `iac/*.tf` files.
- **C2:** `gh run list --workflow=iac.yml` green; PR comment carries the `c2.v1` artifact URIs.
- **Apply:** the `/iac-approvals/<N>` page shows `applied`; optionally confirm the live
  resource exists (`gcloud storage buckets describe`, `gcloud pubsub topics describe`,
  `gcloud run services describe`).
- **Green coloring:** call `/describe` as the coordinator SA (temp `tokenCreator` self-grant
  on `driftscribe-agent@` → mint ID token **with `--include-email`** → revoke) and confirm
  `declared_in_iac` incremented and the new resource shows `iac=True`.

---

## 4. Cost & cleanup

All scale-to-zero / free-tier: 2 idle Cloud Run services ≈ $0, Pub/Sub free tier, GCS pennies,
1 secret ~$0.06/mo. Well under $1/mo idle — trivial vs the ~$300 credit (expires 2026-07-20).
Cleanup (if ever) is out-of-band by design (the pipeline forbids delete): `gcloud … delete`
+ `tofu state rm` + revert the `iac/` file + re-bake clean (the c6e_probe pattern).

---

## 5. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **`tofu-apply-sa`/`plan-builder` lack rights for new types → apply/plan fails** | **Track B-0 grants BEFORE PR1**: apply-sa = custom `driftscribeTofuApplyStorage` (no setIamPolicy) + pubsub.editor; plan-builder = storage.bucketViewer + pubsub.viewer (read-only). Avoids the storage.admin hardening regression (Codex). |
| **Cloud Run create fails: deployer can't actAs the new runtime SAs** | **Track B-1 binds `tofu-apply-sa` serviceAccountUser on storefront-sa + orders-worker-sa BEFORE PR2** |
| PR1 resources show refresh drift (server defaults) → 409 on PR2 apply | Reconcile checkpoint after PR1 (zero-diff plan; add fields/`ignore_changes`); benign-drift classifier is Cloud-Run-only |
| Agent authors resolver-false-drift HCL (interpolated bucket name, missing project/location) | Prescriptive prompt; C2 plan + gate catch; re-prompt; worst case applies-but-amber, fixable |
| create-class `/apply` 502 (terminal failed_state_suspect) | Don't blind-retry; inspect state vs live; reconcile per `iac-apply-failure-recovery.md` §7 |
| Orphaned GCS state lock (OOM) | Worker pinned 2Gi/2cpu; if 423, `tofu force-unlock` by hand after confirming no apply in flight |
| Track-B secret/SA missing before PR2 apply | Strict ordering: Track B-1 secret+SA+accessor+actAs BEFORE PR2 author/apply |
| main advances between merge and rebake → tree-hash mismatch | Pin `_TAG` to the approval-page merge commit; freeze other `iac/` merges in the window |
| Wrong rebake (full cloudbuild.yaml) moves payment-demo tag | Only the targeted `cloudbuild.tofu-apply.yaml` / `cloudbuild.infra-reader.yaml` |
| Bucket name globally taken | `driftscribe-hack-2026-assets` is project-prefixed; verify free before authoring |
| Coordinator's github-pat over-scoped (pre-existing debt) | Out of Phase-3 scope; merge still works; note for later rotation |

**Rollback per resource:** out-of-band delete + `tofu state rm` + revert the iac file + clean
re-bake (both workers). No automated rollback (pipeline forbids destroy by design).

---

## 6. Open decisions for the operator

1. **Batching:** recommended **2 PRs** (PR1 = bucket+topic+sub; PR2 = storefront+orders-worker)
   vs the design-doc-literal **4 PRs** (one per resource group, more granular e2e proofs, more
   human-loop) vs **1 PR** (needs all Track-B first; riskiest).
2. **`payment-api-key` secret value:** a mock string is fine (payment-demo is `PAYMENT_MODE=mock`).
   Operator supplies it, or I use a generated dummy (confirm before `gcloud secrets … add`).
3. **Drift-demo finale (§5 of design):** the infra-graph (CAI, name/type/location read-mask)
   won't *visually* show an env/scaling divergence; a true drift demo needs `storefront`
   registered as a drift-workload target (extra wiring). **Recommend deferring** the drift demo
   to a follow-up; core Phase 3 = the build-out + green coloring.
4. **Go-ahead** to begin **mutating prod** starting with PR1. (Confirmation required per the
   global "confirm before operations that modify infra directly" rule.)
