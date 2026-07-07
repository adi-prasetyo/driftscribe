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
- Cloud Build (canonical targeted deploy): `infra/cloudbuild.infra-reader.yaml` — use this. The full `infra/cloudbuild.yaml` also has `driftscribe-infra-reader` blocks but is a full-stack redeploy that touches payment-demo (see §3 warning).
- IaC layer (the declared-set source, baked into the image): `iac/`, `iac/imports.tf`
- Phase A backend bootstrap (separate, not required for the reader): `docs/runbooks/iac-bootstrap.md`

---

## Trust-boundary invariant (read this first)

The whole point of Phase B is that this worker reads the whole project while
holding **only read-only metadata permissions**. Its grant — the two-permission
custom role `driftscribeInfraReader` (`cloudasset.assets.searchAllResources` +
`serviceusage.services.use`), or the broader predefined pair
`roles/cloudasset.viewer` + `roles/serviceusage.serviceUsageConsumer` — is a
**documented, scoped exception** to the "workers hold the narrowest possible
credential" rule. It grants read access to resource *metadata* (names, types,
locations) and the ability to call the CAI API; it grants **no** write access,
**no** resource-content access beyond what CAI exposes, and **no** ability to
decrypt state. Scoped exception: for two adoptable types the worker issues one
extra scoped `versioned_resources` search each and retains **only one field** —
a Pub/Sub subscription's `resource.topic` and a Cloud Run service's template
container image — so those resources can be adopted without stalling to ask;
every other field the versioned resource carries (env vars, SA/operator emails,
push endpoints) is read but never stored, logged, or returned, and the run image
is suppressed at emission for DriftScribe's own control-plane services. Do not add any other role. The **custom role (step 2a) is the
recommended default** — it is exactly the two permissions the worker calls,
nothing more; the predefined pair (step 2b) is the simpler-but-broader
alternative.

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

First create the SA, then grant it the read-only role. **Use the custom role
(2a) — it is the recommended default.** It is exactly the two permissions the
worker calls; the predefined pair (2b) works but grants strictly more.

```bash
PROJECT=driftscribe-hack-2026
SA="infra-reader-sa@${PROJECT}.iam.gserviceaccount.com"

gcloud iam service-accounts create infra-reader-sa \
  --project="$PROJECT" \
  --display-name="DriftScribe infra-reader (read-only CAI inventory)"
```

### 2a. Custom role (recommended — least privilege)

The worker calls exactly two permissions; the custom role grants exactly those:

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

### 2b. Predefined roles (simpler, broader)

If you'd rather not manage a custom role, the predefined pair works — at the
cost of granting more metadata-read surface than the worker uses:

```bash
PROJECT=driftscribe-hack-2026
SA="infra-reader-sa@${PROJECT}.iam.gserviceaccount.com"

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

> **Live-state reconciliation note.** The first Phase B deploy granted the SA
> the **predefined pair** (2b). To migrate the live `infra-reader-sa` to the
> least-privilege custom role, do it **bind-first, then remove** so there is no
> permission gap if the custom role has a typo or hasn't propagated:
>
> ```bash
> PROJECT=driftscribe-hack-2026
> SA="infra-reader-sa@${PROJECT}.iam.gserviceaccount.com"
> # 1. Create + bind the custom role (step 2a above), then verify the reader
> #    still answers (runbook §5) BEFORE removing anything.
> # 2. Only then remove the now-redundant predefined grants:
> gcloud projects remove-iam-policy-binding "$PROJECT" \
>   --member="serviceAccount:${SA}" --role="roles/cloudasset.viewer"
> gcloud projects remove-iam-policy-binding "$PROJECT" \
>   --member="serviceAccount:${SA}" --role="roles/serviceusage.serviceUsageConsumer"
> ```

## 3. Deploy the worker via Cloud Build

> **⚠️ Do NOT use the full `infra/cloudbuild.yaml` to deploy the infra-reader on
> this project.** That config is a **full-stack** build: it rebuilds and
> redeploys **all ~9 images, including `payment-demo`** (the `${_TARGET_SERVICE}`
> step), at a fresh image tag. Phase A adopted `payment-demo` into OpenTofu state
> pinned to a specific serving image tag (`iac/cloudrun.tf`). Running the full
> build moves `payment-demo` off that tag and **breaks the Phase A zero-diff** —
> a later `tofu apply` could then revert prod. Use the **targeted** config below
> instead. (The full config remains the right tool only when you *intend* a
> coordinated full-stack redeploy and will re-pin `iac/cloudrun.tf` afterward.)

Use the targeted config `infra/cloudbuild.infra-reader.yaml`. It builds + deploys
**only** `driftscribe-infra-reader`, runs the OWN_URL writeback, and wires
`INFRA_READER_URL` onto the already-running coordinator via an incremental
`--update-env-vars` (preserving all other coordinator env/secrets/SA). It does
**not** touch `payment-demo` or any other worker. Like every config here it has
**no deploy-on-push trigger** — operator-run only. Do not submit until
`infra-reader-sa` exists (step 2) and the CAI API is enabled (step 1).

> **Coordinator-first on initial rollout.** The targeted config ships the
> *worker*, not the coordinator image. The coordinator needs its own image to
> contain the `read_project_inventory` tool. If you are rolling out Phase B for
> the first time (or the running coordinator predates the tool), deploy the
> coordinator **first** via `infra/cloudbuild.coordinator-update.yaml`, then run
> the targeted build below.

```bash
PROJECT=driftscribe-hack-2026

# (Initial rollout only) ship the coordinator image carrying read_project_inventory:
gcloud builds submit --config infra/cloudbuild.coordinator-update.yaml \
  --project="$PROJECT" --substitutions=_TAG="$(git rev-parse --short HEAD)"

# Deploy the infra-reader worker + wire INFRA_READER_URL onto the coordinator:
gcloud builds submit --config infra/cloudbuild.infra-reader.yaml \
  --project="$PROJECT" \
  --substitutions=_TAG="$(git rev-parse --short HEAD)",_IAC_SNAPSHOT_SHA="$(git rev-parse HEAD)"
```

The worker deploy sets, on `driftscribe-infra-reader`:

- `--service-account=infra-reader-sa@$PROJECT_ID.iam.gserviceaccount.com`
- `--no-allow-unauthenticated` (ID-token auth required)
- `--set-env-vars=GCP_PROJECT=…,OWN_URL=…,ALLOWED_CALLERS=driftscribe-agent@…,IAC_SNAPSHOT_SHA=${_IAC_SNAPSHOT_SHA}`

> **`IAC_SNAPSHOT_SHA` caveat.** The targeted config takes it from the explicit
> `_IAC_SNAPSHOT_SHA` substitution (Cloud Build does **not** recursively expand
> user substitutions, and a manual `gcloud builds submit` has no built-in
> `$COMMIT_SHA`). Pass `_IAC_SNAPSHOT_SHA=$(git rev-parse HEAD)` as shown; if you
> omit it, it defaults to the literal `manual`. This only affects the provenance
> string in the response, never the inventory itself. To fix it after the fact:
> `gcloud run services update driftscribe-infra-reader --region=asia-northeast1 --update-env-vars=IAC_SNAPSHOT_SHA=$(git rev-parse HEAD)`.

## 4. Auth wiring — who allowlists whom + the platform invoker grant

There are **two** independent gates on a coordinator→worker call, and you need
both:

1. **Cloud Run platform IAM (`roles/run.invoker`).** The worker is deployed
   `--no-allow-unauthenticated`, so Cloud Run itself rejects the request at the
   admission layer unless the **coordinator's SA holds `roles/run.invoker` on
   the `driftscribe-infra-reader` service**. This is a per-service binding,
   separate from anything in the app. **This grant is required** — without it,
   every inventory call 403s before the worker code ever runs. (It is the grant
   that had to be applied by hand during the first Phase B deploy.) The
   idempotent way to apply it is to **(re-)run `infra/scripts/setup_secrets.sh`**
   — its per-service invoker loop now includes `driftscribe-infra-reader` and is
   gated on the service existing, so run it *after* the worker is deployed. Or
   apply it directly:

   ```bash
   PROJECT=driftscribe-hack-2026
   gcloud run services add-iam-policy-binding driftscribe-infra-reader \
     --project="$PROJECT" --region=asia-northeast1 \
     --member="serviceAccount:driftscribe-agent@${PROJECT}.iam.gserviceaccount.com" \
     --role="roles/run.invoker"
   ```

2. **App-level caller allowlist (`ALLOWED_CALLERS`).** The **coordinator calls
   the worker**, so the **worker** allowlists the **coordinator's** SA — not the
   other way around. This is already encoded in the deploy step: the worker's
   `ALLOWED_CALLERS` = `driftscribe-agent@$PROJECT_ID.iam.gserviceaccount.com`,
   and `verify_caller` rejects any other caller, fail-closed. The coordinator
   does **not** need to allowlist `infra-reader-sa`; it only needs the worker's
   URL (below).

`INFRA_READER_URL` is set onto the coordinator automatically — by the targeted
`infra/cloudbuild.infra-reader.yaml` (its final step), or by the full
`infra/cloudbuild.yaml`'s post-deploy writeback loop. If you ever set it by hand:

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
