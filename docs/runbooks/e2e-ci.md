# E2E CI: Workload Identity Federation + GitHub Environment

Phase 20 wires GitHub Actions → GCP authentication for the manual-dispatch
E2E workflow (`.github/workflows/e2e.yml`, added in Task 20.7b) via
Workload Identity Federation — no long-lived service-account keys ever
leave the GCP project boundary.

The security model layers two independent gates. Both must hold for a
workflow run to successfully mint a GCP access token:

1. **WIF attribute-condition** on the provider pins the trust to the
   exact `(repository, environment)` pair
   `('adi-prasetyo/driftscribe', 'e2e')`. A workflow run with any other
   claim cannot complete the OIDC exchange. Combined with
   `workflow_dispatch`-only trigger on the workflow, this means only an
   actor with `workflow` scope on the repo can produce a token-eligible
   run.
2. **Per-resource IAM** on `e2e-runner-sa` is scoped narrowly:
   `roles/run.viewer` project-wide, per-secret `secretAccessor`, and
   `roles/run.developer` ONLY on `payment-demo-e2e` (resource-scoped).
   The two project-wide grants (`run.viewer`, `artifactregistry.reader`)
   are read-only on this isolated E2E project — even with a token, the
   SA cannot mutate the coordinator or workers.

The `e2e` GitHub Environment still exists and still holds the two
secrets (`GCP_WIF_PROVIDER`, `GCP_E2E_RUNNER_SA`) — the environment
binding is what produces the `environment: e2e` OIDC claim the
attribute-condition checks against. **No protection rules are
configured** (cleared 2026-05-24 — the per-job approval prompt added
2× friction per dispatch on a solo-maintainer workflow). Add a
"Required reviewers" rule via repo Settings → Environments if a future
maintainer wants the human gate back; the rest of this runbook works
either way.

Cross-references:

- E2E project provisioning: `docs/runbooks/e2e-environment.md`
- Provisioning script: `infra/scripts/setup_e2e_project.sh`
- E2E workflow (Task 20.7b): `.github/workflows/e2e.yml`

---

## Pre-flight

- `driftscribe-e2e` GCP project exists and `setup_e2e_project.sh` has
  been run successfully (see
  [`e2e-environment.md`](e2e-environment.md)).
- You hold `roles/owner` on `driftscribe-e2e` and have **repo admin**
  on `adi-prasetyo/driftscribe`.
- The first `gcloud builds submit` against `driftscribe-e2e` has
  succeeded — the post-deploy bindings in §3 reference
  `payment-demo-e2e`, which doesn't exist until the build creates it.

---

## 1. Create the WIF pool and provider

The provider is pinned with an `attribute-condition` so the only OIDC
claims it will trust originate from this repo running in the `e2e`
environment.

```bash
# 1. WIF pool + provider, pinned to repo + environment.
gcloud iam workload-identity-pools create gha-pool \
  --project=driftscribe-e2e --location=global

gcloud iam workload-identity-pools providers create-oidc gha-provider \
  --project=driftscribe-e2e --location=global \
  --workload-identity-pool=gha-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.environment=assertion.environment" \
  --attribute-condition="assertion.repository == 'adi-prasetyo/driftscribe' && assertion.environment == 'e2e'"
```

The attribute-condition is case-sensitive. `assertion.environment` must
equal `'e2e'` exactly — a workflow that runs without `environment: e2e`
in its job spec (or runs against a different environment name) will
fail the condition and the OIDC exchange will return
`unauthorized_client`.

---

## 2. Bind `e2e-runner-sa` via the environment principalSet

The binding uses an `attribute.environment/e2e` principalSet, not a
`subject`. This means any workflow run that satisfies the
attribute-condition AND runs in the `e2e` environment can impersonate
`e2e-runner-sa` — but ONLY those runs.

```bash
# 2. Bind e2e-runner-sa via the environment principal set.
gcloud iam service-accounts add-iam-policy-binding \
  e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com \
  --project=driftscribe-e2e \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/gha-pool/attribute.environment/e2e"
```

Replace `PROJECT_NUMBER` with the numeric project ID of
`driftscribe-e2e`:

```bash
gcloud projects describe driftscribe-e2e --format='value(projectNumber)'
```

The numeric project number — not the string project ID
`driftscribe-e2e` — is what the IAM API expects in the principalSet
URI.

---

## 3. Verify `e2e-runner-sa` IAM grants

These bindings are applied by `setup_e2e_project.sh` (with the
resource-scoped ones in `e2e-environment.md` §5). They are listed here
so an operator debugging a permission failure can verify the full
binding set from this single file.

- `roles/run.viewer` (project-wide) — reads the coordinator Cloud Run
  service URL.
- `roles/secretmanager.secretAccessor` **bound per-secret** (no
  project-wide accessor anywhere) on:
  - `coordinator-shared-token`
  - `upgrade-docs-github-pat`
- `roles/run.developer` on the `payment-demo-e2e` service
  (resource-scoped) — required so the E2E fixture can mutate
  `PAYMENT_MODE` / `FEATURE_NEW_CHECKOUT` env vars during baseline
  reset.
- `roles/iam.serviceAccountUser` on `payment-demo-e2e`'s Cloud Run
  runtime SA (the default compute SA
  `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com` — this is
  **e2e's own** compute SA, distinct from hack-2026's, which was retired
  2026-05-31 — unless the deploy step pinned `--service-account=...`).
  Required so the runner can `act-as` the service identity during
  `update_service` calls.
- `roles/artifactregistry.reader` (project-wide) — Cloud Run's admin
  API validates the *caller* can pull the image referenced by a
  service when `update_service` is called (security: prevents image-
  existence leaks via deploy probing). Without this the per-test env
  mutator teardown 403s on `artifactregistry.repositories.downloadArtifacts`
  even though the runtime SA can pull fine. Project-wide is acceptable
  for this isolated E2E project (single AR repo `driftscribe`); a
  prod equivalent should bind on the specific repo instead.
- `roles/datastore.user` — Firestore writes for the cleanup tracker
  fixture.

Two of these — `roles/run.developer` on `payment-demo-e2e` and
`roles/iam.serviceAccountUser` on the runtime SA — are **post-deploy**
bindings. `setup_e2e_project.sh` prints the commands rather than
executing them, because `payment-demo-e2e` does not exist until the
first build completes. Apply them manually from
`e2e-environment.md` §5 after the first `gcloud builds submit`
succeeds.

---

## 4. Configure the GitHub Environment

In the repo settings → **Environments** → "New environment" →
name `e2e`. Set the two environment secrets the workflow's
`google-github-actions/auth` step needs:

- `GCP_WIF_PROVIDER` =
  `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/gha-pool/providers/gha-provider`
- `GCP_E2E_RUNNER_SA` =
  `e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com`

**No protection rules are required.** The environment exists so the
OIDC token carries the `environment: e2e` claim the WIF
attribute-condition checks against — that, combined with
`workflow_dispatch`-only triggering, is the trust boundary. Adding a
"Required reviewers" rule is optional; if you do, every job that
declares `environment: e2e` produces a separate approval prompt, so
multi-job workflows multiply the friction.

---

## 5. Verify the wiring

After Task 20.7b's workflow file lands, trigger a run via the GitHub
UI or:

```bash
gh workflow run e2e.yml --ref main
```

The run starts immediately (no protection rules — see §4). The
`google-github-actions/auth` step should successfully exchange the
OIDC token for a GCP access token and the suite should execute. If
you re-added the "Required reviewers" rule from §4, the run pauses
at the "waiting for review" gate first; approve to proceed.

If the auth step fails, the common causes are:

- `PROJECT_NUMBER` mismatch in the `GCP_WIF_PROVIDER` secret — verify
  with `gcloud projects describe driftscribe-e2e --format='value(projectNumber)'`.
- Attribute-condition typo. The string is case-sensitive: the literal
  values `'adi-prasetyo/driftscribe'` and `'e2e'` must match exactly.
- `e2e-runner-sa` missing the `roles/iam.workloadIdentityUser`
  binding for the `attribute.environment/e2e` principalSet — re-run §2.
- Workflow job missing `environment: e2e` in its job spec — without
  it, the OIDC token has no `environment` claim and the
  attribute-condition rejects the exchange.

---

## 6. Teardown

The WIF pool + provider are owned by the `driftscribe-e2e` project and
are deleted alongside it:

```bash
gcloud projects delete driftscribe-e2e
```

The GitHub Environment `e2e` can be deleted from the repo settings
when the workflow is retired — no orphan GCP resources remain.
