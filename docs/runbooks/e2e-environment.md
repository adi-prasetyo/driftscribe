# DriftScribe E2E environment — operator runbook

This runbook is the canonical reference for standing up and tearing
down the `driftscribe-e2e` GCP project — the isolated, parameterized
copy of the DriftScribe coordinator + workers that hosts the assertive
end-to-end test suite from Phase 20. It is operator-facing: every
command below is meant to be copy-pasted from a shell that holds
`roles/owner` on the E2E project.

The E2E project is **never** `driftscribe-hack-2026`. Mistakes that
mutate prod (e.g. running the E2E baseline-reset against the wrong
service) are blast-radius-isolated only because the two projects are
fully separated by GCP project ID. Double-check `PROJECT_E2E` is set
to the E2E ID before running anything below.

Cross-references:

- Provisioning script: `infra/scripts/setup_e2e_project.sh`
- Shared helpers: `infra/scripts/_setup_lib.sh`
- Parameterized build manifest: `infra/cloudbuild.yaml`
- Coordinator-side registry override: `agent/workloads/registry.py:resolve_upgrade_target`
- CI workflow runbook: `docs/runbooks/e2e-ci.md` (coming in Task 20.7a)

---

## 1. One-time pre-flight (outside this script)

These are the steps **before** `setup_e2e_project.sh` can run.

1. Create the `driftscribe-e2e` GCP project under the billing account
   that holds the $300 hackathon coupon. Use the Console or:

   ```bash
   gcloud projects create driftscribe-e2e \
     --name "DriftScribe E2E" \
     --set-as-default
   gcloud billing projects link driftscribe-e2e --billing-account <BILLING_ACCOUNT_ID>
   ```

2. Create the `adi-prasetyo/driftscribe-e2e-target` GitHub repository
   (private). It will be seeded in step 5 below.

3. Generate four fine-grained PATs (GitHub →
   Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → "Generate new token"):

   | Token name                 | Repository access     | Permissions                                             |
   |----------------------------|-----------------------|---------------------------------------------------------|
   | `e2e-coordinator-pat`      | driftscribe-e2e-target | Contents: read, Pull requests: read (coordinator-side read-only PR search — mirrors prod `github-pat`) |
   | `e2e-docs-pat`             | driftscribe-e2e-target | Contents: read/write, Pull requests: read/write          |
   | `e2e-upgrade-reader-pat`   | driftscribe-e2e-target | Contents: read, Pull requests: read                      |
   | `e2e-upgrade-docs-pat`     | driftscribe-e2e-target | Contents: read/write, Pull requests: read/write          |

   Hold these tokens for step 3 of provisioning (see § "Populate the
   secrets" below). Never paste them into the runbook itself.

4. Provision a Developer Knowledge MCP API key on the
   `driftscribe-e2e` project. The Console flow is the simplest one
   that gets the API-restriction binding right:

   - https://console.cloud.google.com/apis/credentials?project=driftscribe-e2e
   - "+ Create credentials" → API key
   - Edit the key → "Restrict key" → API restrictions → "Restrict key"
     → select **only** "Developer Knowledge API".

5. Pick a webhook endpoint that returns 2xx for the notifier secret.
   `https://webhook.site/<your-uuid>` is fine for E2E — the suite makes
   no assertions on the webhook contents.

6. (Optional, recommended) Verify Vertex AI Gemini quota for
   `gemini-2.5-flash` in `asia-northeast1` on the `driftscribe-e2e`
   project before the first build:

   - https://console.cloud.google.com/iam-admin/quotas?project=driftscribe-e2e
   - filter by "Vertex AI" service, "Online prediction tokens per
     minute per model per project per region".

---

## 2. Provision the project

Run the bootstrap script. It is idempotent — re-running on a partially
provisioned project skips the already-done steps.

```bash
PROJECT_E2E=driftscribe-e2e infra/scripts/setup_e2e_project.sh
```

The script:

- enables every API the prod project enables (`run`, `firestore`,
  `secretmanager`, `logging`, `cloudbuild`, `iam`, `iamcredentials`,
  `aiplatform`, `artifactregistry`, `developerknowledge`, `eventarc`,
  `eventarcpublishing`) plus `gcloud beta services mcp enable
  developerknowledge.googleapis.com`;
- creates the Artifact Registry repo `driftscribe` in `asia-northeast1`;
- creates 8 service accounts (the 7 from prod plus `e2e-runner-sa`)
  and the corresponding `iam.serviceAccountUser` grants for Cloud Build;
- creates 8 empty Secret Manager resources;
- binds per-secret `roles/secretmanager.secretAccessor` (no project-wide
  accessor anywhere);
- grants the per-SA project-level roles (Firestore, Vertex AI,
  `logging.viewer` on the coordinator, `run.viewer` on the reader and
  e2e-runner, `datastore.user` on rollback + e2e-runner);
- initializes Firestore Native in `asia-northeast1`;
- extends the `_Default` Cloud Logging bucket retention to 365 days.

The script is safe to re-run (every helper is describe-then-act), but
re-running does NOT execute the post-deploy bindings in §5 — those are
printed only. Apply §5 manually after the first build succeeds.

---

## 3. Populate the secrets

The script creates the secret **resources** but cannot populate them.
Run the following for each:

```bash
# 1. coordinator-shared-token (auto-generate; SAVE the printed value —
#    it's the X-DriftScribe-Token your E2E tests will send).
COORDINATOR_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "coordinator-shared-token = $COORDINATOR_TOKEN"
printf '%s' "$COORDINATOR_TOKEN" \
  | gcloud secrets versions add coordinator-shared-token \
    --project driftscribe-e2e --data-file=-
# Lost the value? Retrieve it later with:
#   gcloud secrets versions access latest --secret=coordinator-shared-token --project=driftscribe-e2e
```

```bash
# 2. approval-hmac-key (auto-generate; never used by the operator directly)
printf '%s' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  | gcloud secrets versions add approval-hmac-key \
    --project driftscribe-e2e --data-file=-
```

```bash
# 3. github-pat — coordinator-side read-only PAT (e2e-coordinator-pat)
printf '%s' "<e2e-coordinator-pat>" \
  | gcloud secrets versions add github-pat \
    --project driftscribe-e2e --data-file=-
```

```bash
# 4. docs-agent-github-pat — drift PR opener (e2e-docs-pat)
printf '%s' "<e2e-docs-pat>" \
  | gcloud secrets versions add docs-agent-github-pat \
    --project driftscribe-e2e --data-file=-
```

```bash
# 5. upgrade-reader-github-pat — read-only PAT (e2e-upgrade-reader-pat)
printf '%s' "<e2e-upgrade-reader-pat>" \
  | gcloud secrets versions add upgrade-reader-github-pat \
    --project driftscribe-e2e --data-file=-
```

```bash
# 6. upgrade-docs-github-pat — upgrade PR opener (e2e-upgrade-docs-pat)
printf '%s' "<e2e-upgrade-docs-pat>" \
  | gcloud secrets versions add upgrade-docs-github-pat \
    --project driftscribe-e2e --data-file=-
```

```bash
# 7. developer-knowledge-api-key — GCP API key scoped to Developer Knowledge API
printf '%s' "<paste-from-Console-Credentials-page>" \
  | gcloud secrets versions add developer-knowledge-api-key \
    --project driftscribe-e2e --data-file=-
```

```bash
# 8. driftscribe-webhook-url — webhook.site URL (or any 2xx endpoint)
printf '%s' "https://webhook.site/<your-uuid>" \
  | gcloud secrets versions add driftscribe-webhook-url \
    --project driftscribe-e2e --data-file=-
```

---

## 4. Deploy via the parameterized cloudbuild

The same `infra/cloudbuild.yaml` that deploys prod also deploys E2E.
The four substitutions below redirect the deploy at the E2E target,
flip `USE_ADK` on, and pin the upgrade-target redirect.

```bash
gcloud builds submit --config infra/cloudbuild.yaml \
  --project=driftscribe-e2e \
  --substitutions=_TARGET_SERVICE=payment-demo-e2e,_TARGET_GITHUB_REPO=adi-prasetyo/driftscribe-e2e-target,_UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe-e2e-target,_USE_ADK=true
```

`_USE_ADK=true` is mandatory for E2E — every `/chat` test depends on
the ADK path being live. Prod deploys omit this substitution so the
default (`false`) keeps the coordinator's chat endpoint hard-503ing
until the operator explicitly opts in via
`gcloud run services update driftscribe-agent --update-env-vars=USE_ADK=true`
on prod.

---

## 5. Post-deploy IAM bindings

These bindings reference `payment-demo-e2e`, which doesn't exist until
the first build above completes.

```bash
PROJECT=driftscribe-e2e
REGION=asia-northeast1
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')

# Rollback worker — resource-scoped run.developer on payment-demo-e2e
gcloud run services add-iam-policy-binding payment-demo-e2e \
  --project=$PROJECT --region=$REGION \
  --member="serviceAccount:rollback-agent-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/run.developer"

# E2E runner — resource-scoped run.developer on payment-demo-e2e
gcloud run services add-iam-policy-binding payment-demo-e2e \
  --project=$PROJECT --region=$REGION \
  --member="serviceAccount:e2e-runner-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/run.developer"

# E2E runner — actAs the payment-demo-e2e runtime SA. If the deploy
# step pinned --service-account on payment-demo-e2e, target THAT SA.
# Default is the project's Compute Engine default SA:
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --project=$PROJECT \
  --member="serviceAccount:e2e-runner-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Coordinator <-> worker run.invoker grants (each worker, per-service)
for worker in driftscribe-reader driftscribe-docs driftscribe-rollback \
              driftscribe-notifier driftscribe-upgrade-reader \
              driftscribe-upgrade-docs; do
  gcloud run services add-iam-policy-binding "$worker" \
    --project=$PROJECT --region=$REGION \
    --member="serviceAccount:driftscribe-agent@$PROJECT.iam.gserviceaccount.com" \
    --role="roles/run.invoker"
done
```

---

## 6. Seed the upgrade target

The upgrade workload needs a vulnerable lodash to chew on. Seed the
`driftscribe-e2e-target` repo with the same demo lockfile prod uses:

```bash
git clone git@github.com:adi-prasetyo/driftscribe-e2e-target.git
cd driftscribe-e2e-target
mkdir -p demo/upgrade-target
cat > demo/upgrade-target/package.json <<'JSON'
{
  "name": "driftscribe-upgrade-demo",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "lodash": "4.17.20"
  }
}
JSON
git add demo/upgrade-target/package.json
git commit -m "Seed: lodash 4.17.20 (vulnerable to GHSA-35jh-r3h4-6jhm)"
git push origin main
```

The path `demo/upgrade-target/package.json` matches both
`UPGRADE_TARGET_REGISTRY["phase17_demo"].lockfile_path` and the
upgrade workers' `_LOCKFILE_PATH_RE` (pinned by
`tests/integration/test_upgrade_deploy_pin.py`).

---

## 7. Verify the deploy

```bash
COORD_URL=$(gcloud run services describe driftscribe-agent \
  --project=driftscribe-e2e --region=asia-northeast1 \
  --format='value(status.url)')
echo "Coordinator: $COORD_URL"

# /health (no auth): expect 200
curl -sf "$COORD_URL/health"

# /chat (auth required, ADK path): expect 200 with a reply field
curl -sf -X POST "$COORD_URL/chat" \
  -H "X-DriftScribe-Token: $(gcloud secrets versions access latest --secret=coordinator-shared-token --project=driftscribe-e2e)" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is the current drift status?", "workload": "drift"}'
```

If `/chat` returns 503 with "ADK disabled", verify the build was run
with `_USE_ADK=true`. If it returns 401, verify the token in the
`X-DriftScribe-Token` header matches the latest version of
`coordinator-shared-token`.

---

## 8. Manual baseline reset

A crashed E2E test can leave `payment-demo-e2e` in a non-baseline env
state (e.g. `PAYMENT_MODE=live`). The session-scoped fixture in
`tests/e2e/conftest.py` is supposed to restore it at teardown, but if
the test process was killed, the operator can manually force the
baseline back:

```bash
gcloud run services update payment-demo-e2e \
  --project=driftscribe-e2e --region=asia-northeast1 \
  --update-env-vars=PAYMENT_MODE=mock,FEATURE_NEW_CHECKOUT=false
```

`PAYMENT_MODE=mock` + `FEATURE_NEW_CHECKOUT=false` are the
contract-declared baseline values from `demo/ops-contract.yaml`.

---

## 9. Teardown

When the E2E project is no longer needed (post-submission):

```bash
gcloud projects delete driftscribe-e2e
```

This is a destructive, audit-logged action. The 30-day pending-delete
window in GCP lets you reverse it via the Console if needed.

---

## 10. CI integration

The Workload Identity Federation setup that lets GitHub Actions
impersonate `e2e-runner-sa@driftscribe-e2e.iam.gserviceaccount.com`
is documented in `docs/runbooks/e2e-ci.md` (coming in Task 20.7a).
