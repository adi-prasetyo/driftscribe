# DriftScribe infra-reader — operator deploy runbook

This runbook covers the **live-GCP** steps that stand up the Phase B
`infra-reader` worker against the real `driftscribe-hack-2026` project. Like the
Phase A bootstrap, every step here mutates live GCP (API enablement, a service
account, IAM grants, a Cloud Run deploy) — **none of it runs in CI**, and none
of it ran when the Phase B code merged. The agent produced all code, tests, and
the Cloud Build wiring; it deliberately stopped at the code. You (the operator)
run the steps below from a shell that holds `roles/owner` (or equivalent admin)
on `driftscribe-hack-2026`.

The `infra-reader` worker enumerates the project's Cloud Asset Inventory
(CAI)–searchable resources and labels each "declared in IaC" vs "not", by parsing
the `iac/*.tf` baked into its own image. It is **strictly read-only**: no
mutation tools, no write IAM, **no OpenTofu state, no KMS** — so unlike the Phase A
backend it does **not** depend on the state bucket / KMS key existing. You can
deploy and use the infra-reader before (or entirely without) running the Phase A
bootstrap.

> **Target project is `driftscribe-hack-2026` (prod), region
> `asia-northeast1`.** Confirm `PROJECT` and the active gcloud account before
> running anything below. None of these steps create or modify any *workload*
> resource — they only add the reader's own SA + Cloud Run service and grant it
> read-only metadata roles.

Cross-references:

- Phase B design + decisions log: `docs/plans/2026-05-27-infra-iac-phase-b-design.md`
- Phase B plan: `docs/plans/2026-05-27-infra-iac-phase-b.md`
- Worker source: `workers/infra_reader/main.py`, `workers/infra_reader/Dockerfile`
- Cloud Build steps: `infra/cloudbuild.yaml` (`driftscribe-infra-reader` blocks)
- IaC layer (the declared-set source, baked into the image): `iac/`, `iac/imports.tf`
- Phase A backend bootstrap (separate, not required for the reader): `docs/runbooks/iac-bootstrap.md`

---

## Trust-boundary invariant (read this first)

The whole point of Phase B is that this worker reads the whole project while
holding **only read-only metadata permissions**. Its two project-level grants —
`roles/cloudasset.viewer` and `roles/serviceusage.serviceUsageConsumer` — are a
**documented, scoped exception** to the "workers hold the narrowest possible
credential" rule. They grant read access to resource *metadata* (names, types,
locations) and the ability to call the CAI API; they grant **no** write access,
**no** resource-content access beyond what CAI exposes, and **no** ability to
decrypt state. Do not add any other role. If you prefer least-privilege over the
predefined roles, use the custom role in step 2b.

---

## 1. Enable the Cloud Asset API

CAI's `searchAllResources` lives behind `cloudasset.googleapis.com`. Enable it on
the target project:

```bash
PROJECT=driftscribe-hack-2026
gcloud services enable cloudasset.googleapis.com --project="$PROJECT"
```

(Allow a minute or two after enablement before the first call; freshly enabled
APIs can briefly return `PERMISSION_DENIED`/`SERVICE_DISABLED`. The worker
fail-soft-returns HTTP 200 with `{"error": "cloud_asset_unavailable", ...}` in
that window rather than 500, so chat narrates the degradation cleanly.)

## 2. Create the `infra-reader` service account + grant read-only roles

### 2a. Predefined roles (simplest)

```bash
PROJECT=driftscribe-hack-2026
SA="infra-reader-sa@${PROJECT}.iam.gserviceaccount.com"

gcloud iam service-accounts create infra-reader-sa \
  --project="$PROJECT" \
  --display-name="DriftScribe infra-reader (read-only CAI inventory)"

# Read-only resource metadata via Cloud Asset Inventory.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" \
  --role="roles/cloudasset.viewer"

# REQUIRED in addition to cloudasset.viewer: every CAI call needs
# serviceusage.services.use. viewer alone is insufficient (Phase B decision #11).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" \
  --role="roles/serviceusage.serviceUsageConsumer"
```

### 2b. Custom role (least-privilege alternative)

If you'd rather not grant the predefined roles, the worker needs exactly two
permissions:

```bash
PROJECT=driftscribe-hack-2026
gcloud iam roles create driftscribeInfraReader \
  --project="$PROJECT" \
  --title="DriftScribe infra-reader (minimal)" \
  --permissions="cloudasset.assets.searchAllResources,serviceusage.services.use"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:infra-reader-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="projects/${PROJECT}/roles/driftscribeInfraReader"
```

## 3. Deploy the worker via Cloud Build

`infra/cloudbuild.yaml` already contains the `driftscribe-infra-reader`
build/push/deploy steps and the OWN_URL writeback, mirroring the other workers.

**This file has no automatic deploy-on-push trigger** — it runs only when an
operator submits it manually. So this step is safe to have merged before you
complete steps 1–2; nothing deploys until you run the build. Conversely, do
**not** submit the build until `infra-reader-sa` exists (step 2) and the CAI API
is enabled (step 1), or the deploy fails on `actAs`/permission.

```bash
PROJECT=driftscribe-hack-2026
gcloud builds submit --config infra/cloudbuild.yaml --project="$PROJECT" \
  --substitutions=_TAG="$(git rev-parse --short HEAD)"
```

The deploy step sets, on the `driftscribe-infra-reader` service:

- `--service-account=infra-reader-sa@$PROJECT_ID.iam.gserviceaccount.com`
- `--no-allow-unauthenticated` (ID-token auth required)
- `--set-env-vars=GCP_PROJECT=…,OWN_URL=…,ALLOWED_CALLERS=driftscribe-agent@…,IAC_SNAPSHOT_SHA=$COMMIT_SHA`

> **`IAC_SNAPSHOT_SHA` caveat.** It is stamped from Cloud Build's built-in
> `$COMMIT_SHA`. On a **manual** `gcloud builds submit` from a local source
> upload (no connected Git repo), `$COMMIT_SHA` resolves to empty, so the worker
> reports an empty `iac_snapshot_sha`. This is cosmetic — it only affects the
> provenance string in the response, never the inventory itself. If you want
> accurate provenance from a manual submit, deploy from a build trigger wired to
> the repo, or update the service afterwards:
> `gcloud run services update driftscribe-infra-reader --region=asia-northeast1 --update-env-vars=IAC_SNAPSHOT_SHA=$(git rev-parse HEAD)`.

## 4. Auth wiring — who allowlists whom

The **coordinator calls the worker**, so the **worker** allowlists the
**coordinator's** service account — not the other way around. This is already
encoded in the Cloud Build deploy step:

- The worker's `ALLOWED_CALLERS` = `driftscribe-agent@$PROJECT_ID.iam.gserviceaccount.com`
  (the coordinator's SA). `verify_caller` rejects any other caller, fail-closed.
- The coordinator does **not** need to allowlist `infra-reader-sa`. It only needs
  the worker's URL.

`INFRA_READER_URL` is synced onto the coordinator automatically by the final
post-deploy step in `infra/cloudbuild.yaml` (the same writeback loop that sets
`READER_URL`, `UPGRADE_READER_URL`, etc.). If you ever set it by hand:

```bash
URL=$(gcloud run services describe driftscribe-infra-reader \
  --region=asia-northeast1 --format='value(status.url)')
gcloud run services update driftscribe-agent \
  --region=asia-northeast1 --update-env-vars=INFRA_READER_URL="$URL"
```

## 5. Verify

```bash
# Worker is up (will 401/403 without an ID token — that's correct, it means
# auth is enforced; a 200 on /healthz is the unauthenticated liveness probe).
URL=$(gcloud run services describe driftscribe-infra-reader \
  --region=asia-northeast1 --format='value(status.url)')
curl -fsS "$URL/healthz"   # -> {"ok":true}
```

Then exercise it end-to-end through the **explore** (read-only) chat workload and
ask for the project inventory. Confirm the response carries `inventory_source`,
`freshness_caveat`, `iac_snapshot_sha`, and that `payment-demo` shows up labeled
declared-in-IaC.

## 6. Keep `iac/imports.tf` until Phase C (recommended)

The worker matches live resources to IaC declarations at two confidence tiers:
**high** (from `import` block IDs in `iac/imports.tf`) and **derived** (from
`resource` block identities). Phase A noted the import blocks are removable once
adoption is complete — but removing them downgrades `payment-demo` (and any
future imported resource) from high- to derived-confidence matching, and widens
the `declared_not_found` surface. **Keep the import blocks until Phase C** adds
state-read, which will supersede HCL-derived matching with the authoritative
managed set. (Phase B decisions #12, #13.)

---

## Notes

- The agent authored everything in this runbook's scope as code/config; it did
  **not** run any `gcloud`, enable any API, create any SA, grant any IAM, or
  deploy. All of section 1–5 is operator-run.
- The worker needs **no** git access and **no** state/KMS access. It refreshes
  its declared set only on redeploy (the `iac/` tree is baked into the image),
  and reports `iac_snapshot_sha` so staleness is detectable.
