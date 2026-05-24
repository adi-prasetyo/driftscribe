#!/usr/bin/env bash
# Idempotent provisioning for the DriftScribe E2E GCP project.
#
# Mirrors the prod ``setup_secrets.sh`` topology (8 service accounts,
# 8 Secret Manager resources, Firestore Native, log retention, IAM
# matrix) but on a SEPARATE GCP project — ``driftscribe-e2e`` by
# default — so end-to-end tests never touch the production tenant.
#
# Usage:
#   PROJECT_E2E=driftscribe-e2e infra/scripts/setup_e2e_project.sh
#
# After this script runs once and exits 0, the operator MUST:
#   1. Populate the 8 secrets with real values (gcloud secrets versions add).
#   2. Run the first ``gcloud builds submit --config infra/cloudbuild.yaml``
#      with the E2E substitution set (see runbook).
#   3. Manually apply the post-deploy IAM bindings printed at the end of
#      THIS script's output (rollback worker on payment-demo-e2e,
#      e2e-runner-sa on payment-demo-e2e + the runtime SA, coordinator
#      run.invoker on each worker) — those bindings reference resources
#      that only exist AFTER the first build, and the script PRINTS but
#      does not EXECUTE them. Re-running the script is safe and idempotent
#      but does NOT close the post-deploy loop.
#
# Documented in docs/runbooks/e2e-environment.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

PROJECT="${PROJECT_E2E:?usage: PROJECT_E2E=<project-id> $0 (e.g. PROJECT_E2E=driftscribe-e2e)}"
REGION="asia-northeast1"

# --------------------------------------------------------------------------
# 0. Pre-flight: confirm the project exists and the caller can act on it.
# --------------------------------------------------------------------------
# We DO NOT auto-create the project — that's an operator decision tied to
# billing-account selection. Fail loud if the project ID is wrong.
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: project ${PROJECT} does not exist or the active gcloud" >&2
  echo "       account does not have describe permission on it." >&2
  echo "       Create the project first (Console or gcloud projects create)" >&2
  echo "       and confirm the active account holds roles/owner on it." >&2
  exit 1
fi

CALLER_EMAIL="$(gcloud config get-value account 2>/dev/null)"
if [ -z "$CALLER_EMAIL" ]; then
  echo "ERROR: gcloud has no active account configured. Run 'gcloud auth login'." >&2
  exit 1
fi
echo "Provisioning ${PROJECT} as ${CALLER_EMAIL} (verifying owner role)..."
if ! gcloud projects get-iam-policy "$PROJECT" \
     --flatten='bindings[].members' \
     --format="value(bindings.role)" \
     --filter="bindings.members:${CALLER_EMAIL} AND bindings.role:roles/owner" \
     2>/dev/null | grep -q "roles/owner"; then
  echo "WARNING: caller ${CALLER_EMAIL} may not hold roles/owner on ${PROJECT}." >&2
  echo "         Some bindings below may fail. Continuing (re-run after granting role)." >&2
fi

# --------------------------------------------------------------------------
# 1. APIs — full prod parity (matches setup_secrets.sh:66-77).
# --------------------------------------------------------------------------
# Eventarc + Eventarc Publishing aren't required by the Phase 20 test set,
# but enabling them keeps the E2E project from drifting from prod's API
# surface (so a future test that depends on the auto-trigger path doesn't
# need a separate enable step).
enable_apis_idempotent "$PROJECT" \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  developerknowledge.googleapis.com \
  eventarc.googleapis.com \
  eventarcpublishing.googleapis.com \
  firestore.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  logging.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com

# Explicit MCP enable for the Developer Knowledge API. Matches
# setup_secrets.sh:86 — see that file's comment for the rationale.
enable_mcp_idempotent "$PROJECT" developerknowledge.googleapis.com

# --------------------------------------------------------------------------
# 2. Artifact Registry — docker repo the cloudbuild pushes to.
# --------------------------------------------------------------------------
create_artifact_repo_idempotent "$PROJECT" driftscribe "$REGION" \
  "DriftScribe E2E images (agent + workers + payment-demo-e2e)"

# --------------------------------------------------------------------------
# 3. Cloud Build SA grants — same matrix as prod.
# --------------------------------------------------------------------------
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
# Cloud Build's default service account is project-policy-dependent: on
# projects created after 2024-04-29 (and ones whose org policy opts in)
# the build runs as the Compute Engine default SA
# (${PROJECT_NUMBER}-compute@developer.gserviceaccount.com); older /
# opted-out projects still use ${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com.
# `gcloud builds get-default-service-account` is the source of truth at
# the project level. We grant the resolved value AND, defensively, the
# legacy one — bindings on a non-existent SA fail loudly, so the legacy
# branch is gated behind a describe.
CLOUDBUILD_SA=""
if CLOUDBUILD_SA="$(gcloud builds get-default-service-account \
      --project="$PROJECT" --format='value(serviceAccountEmail)' 2>/dev/null)" \
   && [ -n "$CLOUDBUILD_SA" ]; then
  # `get-default-service-account` returns
  # `projects/<num>/serviceAccounts/<email>` on some gcloud versions; strip.
  CLOUDBUILD_SA="${CLOUDBUILD_SA##*/}"
  echo "Cloud Build default SA resolved: ${CLOUDBUILD_SA}"
else
  CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
  echo "Cloud Build default SA fallback (gcloud lacks get-default-service-account): ${CLOUDBUILD_SA}"
fi
for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
; do
  grant_role_idempotent "$PROJECT" "serviceAccount:${CLOUDBUILD_SA}" "$role"
done
# Also grant the legacy cloudbuild SA if it exists — covers projects
# where the org policy was flipped mid-life. `gcloud iam
# service-accounts describe` returns nonzero if it doesn't exist.
LEGACY_CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
if [ "$CLOUDBUILD_SA" != "$LEGACY_CLOUDBUILD_SA" ] && \
   gcloud iam service-accounts describe "$LEGACY_CLOUDBUILD_SA" \
     --project="$PROJECT" >/dev/null 2>&1; then
  for role in \
    roles/artifactregistry.writer \
    roles/run.admin \
    roles/iam.serviceAccountUser \
  ; do
    grant_role_idempotent "$PROJECT" "serviceAccount:${LEGACY_CLOUDBUILD_SA}" "$role"
  done
fi

# --------------------------------------------------------------------------
# 4. Service Accounts — 7 prod SAs + 1 E2E-only runner SA.
# --------------------------------------------------------------------------
# E2E uses the same SA topology as prod (deploys the same workloads via
# the parameterized cloudbuild) plus one extra: e2e-runner-sa is the
# identity GitHub Actions impersonates via Workload Identity Federation
# to drive the E2E test runs.
for sa in \
  driftscribe-agent \
  reader-agent-sa \
  docs-agent-sa \
  rollback-agent-sa \
  notifier-agent-sa \
  upgrade-reader-sa \
  upgrade-docs-sa \
  e2e-runner-sa \
; do
  create_service_account_idempotent "$PROJECT" "$sa" "DriftScribe E2E ${sa}"
done

COORD_SA="driftscribe-agent@${PROJECT}.iam.gserviceaccount.com"
READER_SA="reader-agent-sa@${PROJECT}.iam.gserviceaccount.com"
DOCS_SA="docs-agent-sa@${PROJECT}.iam.gserviceaccount.com"
ROLLBACK_SA="rollback-agent-sa@${PROJECT}.iam.gserviceaccount.com"
NOTIFIER_SA="notifier-agent-sa@${PROJECT}.iam.gserviceaccount.com"
UPGRADE_READER_SA="upgrade-reader-sa@${PROJECT}.iam.gserviceaccount.com"
UPGRADE_DOCS_SA="upgrade-docs-sa@${PROJECT}.iam.gserviceaccount.com"
E2E_RUNNER_SA="e2e-runner-sa@${PROJECT}.iam.gserviceaccount.com"

# Cloud Build acts-as each runtime SA during ``gcloud run deploy``.
# Bind both the resolved default-build SA AND the legacy cloudbuild SA
# (if it exists) so the build succeeds regardless of which identity
# Cloud Build picks per the project's org policy at run time.
for sa in "$COORD_SA" "$READER_SA" "$DOCS_SA" "$ROLLBACK_SA" "$NOTIFIER_SA" "$UPGRADE_READER_SA" "$UPGRADE_DOCS_SA"; do
  gcloud iam service-accounts add-iam-policy-binding "$sa" \
    --project="$PROJECT" \
    --member="serviceAccount:${CLOUDBUILD_SA}" \
    --role="roles/iam.serviceAccountUser" \
    --condition=None --quiet >/dev/null
  if [ "$CLOUDBUILD_SA" != "$LEGACY_CLOUDBUILD_SA" ] && \
     gcloud iam service-accounts describe "$LEGACY_CLOUDBUILD_SA" \
       --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts add-iam-policy-binding "$sa" \
      --project="$PROJECT" \
      --member="serviceAccount:${LEGACY_CLOUDBUILD_SA}" \
      --role="roles/iam.serviceAccountUser" \
      --condition=None --quiet >/dev/null
  fi
done

# --------------------------------------------------------------------------
# 5. Per-SA project-level IAM grants (mirrors prod matrix).
# --------------------------------------------------------------------------
# Coordinator: Firestore + Vertex AI + logging.viewer (the latter for
# the /trace endpoint's logEntries.list calls).
for role in \
  roles/datastore.user \
  roles/aiplatform.user \
  roles/logging.viewer \
; do
  grant_role_idempotent "$PROJECT" "serviceAccount:${COORD_SA}" "$role"
done

# Reader: project-wide run.viewer.
grant_role_idempotent "$PROJECT" "serviceAccount:${READER_SA}" "roles/run.viewer"

# Rollback: project-wide datastore.user (resource-scoped run.developer
# on payment-demo-e2e is a post-deploy step; see "next steps" block).
grant_role_idempotent "$PROJECT" "serviceAccount:${ROLLBACK_SA}" "roles/datastore.user"

# e2e-runner-sa: read service URLs, write Firestore cleanup-tracker
# docs, and read AR images. The per-secret accessor bindings come
# after secrets are created below. roles/run.developer +
# roles/iam.serviceAccountUser are post-deploy bindings (need
# payment-demo-e2e to exist first).
#
# artifactregistry.reader: Cloud Run admin API validates the *caller*
# can pull the image referenced by a service when update_service is
# called (security: prevents image-existence leaks via deploy probing).
# Without this, the per-test env-mutator teardown 403s with
# "Permission 'artifactregistry.repositories.downloadArtifacts' denied"
# even though the runtime SA can pull fine — the failure is on the
# admin call, not the container pull.
grant_role_idempotent "$PROJECT" "serviceAccount:${E2E_RUNNER_SA}" "roles/run.viewer"
grant_role_idempotent "$PROJECT" "serviceAccount:${E2E_RUNNER_SA}" "roles/datastore.user"
grant_role_idempotent "$PROJECT" "serviceAccount:${E2E_RUNNER_SA}" "roles/artifactregistry.reader"

# --------------------------------------------------------------------------
# 6. Secrets — create resources (operator populates values via
#    `gcloud secrets versions add` after this script exits).
# --------------------------------------------------------------------------
# All 8 prod secrets are created as empty placeholders. The operator
# populates each via:
#   printf '%s' "<value>" | gcloud secrets versions add <name> \
#     --project "$PROJECT_E2E" --data-file=-
# Cloud Build will fail the first deploy with INVALID_ARGUMENT until
# every secret has at least one version — see the runbook for the
# per-secret instructions.
for secret in \
  coordinator-shared-token \
  github-pat \
  developer-knowledge-api-key \
  docs-agent-github-pat \
  approval-hmac-key \
  driftscribe-webhook-url \
  upgrade-reader-github-pat \
  upgrade-docs-github-pat \
; do
  create_secret_idempotent "$PROJECT" "$secret"
done

# --------------------------------------------------------------------------
# 6b. Per-secret accessor bindings (defense in depth — no project-wide
#     secretmanager.secretAccessor grant anywhere).
# --------------------------------------------------------------------------
bind_secret_accessor "$PROJECT" coordinator-shared-token       "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" github-pat                     "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" developer-knowledge-api-key    "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" docs-agent-github-pat          "serviceAccount:${DOCS_SA}"
bind_secret_accessor "$PROJECT" approval-hmac-key              "serviceAccount:${ROLLBACK_SA}"
bind_secret_accessor "$PROJECT" driftscribe-webhook-url        "serviceAccount:${NOTIFIER_SA}"
bind_secret_accessor "$PROJECT" upgrade-reader-github-pat      "serviceAccount:${UPGRADE_READER_SA}"
bind_secret_accessor "$PROJECT" upgrade-docs-github-pat        "serviceAccount:${UPGRADE_DOCS_SA}"

# e2e-runner-sa: per-secret accessor on the two secrets the test
# harness needs to drive the coordinator + verify the upgrade workload.
# Bound PER-SECRET to maintain the no-project-wide-accessor invariant.
bind_secret_accessor "$PROJECT" coordinator-shared-token       "serviceAccount:${E2E_RUNNER_SA}"
bind_secret_accessor "$PROJECT" upgrade-docs-github-pat        "serviceAccount:${E2E_RUNNER_SA}"

# --------------------------------------------------------------------------
# 7. Firestore Native — same region as prod (asia-northeast1).
# --------------------------------------------------------------------------
create_firestore_native_idempotent "$PROJECT" "$REGION"

# --------------------------------------------------------------------------
# 8. Log retention — extend _Default bucket to 365 days.
# --------------------------------------------------------------------------
extend_log_retention_idempotent "$PROJECT" 365

# --------------------------------------------------------------------------
# 9. Post-deploy reminders — these bindings only work AFTER the first
#    `gcloud builds submit` deploys the drift target service.
# --------------------------------------------------------------------------
cat <<EOF

================================================================
setup_e2e_project.sh: complete

Next steps (operator action required):

(1) Populate the 8 secrets (placeholders are empty):

    printf '%s' "<value>" | gcloud secrets versions add <name> \\
      --project "$PROJECT" --data-file=-

  For each of:
    coordinator-shared-token       (auto-generate: python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    approval-hmac-key              (auto-generate: same as above)
    github-pat                     (fine-grained PAT, Issues+Contents+PR write on driftscribe-e2e-target — coordinator side effects when _DRY_RUN=false)
    docs-agent-github-pat          (fine-grained PAT: Contents+PR write on driftscribe-e2e-target)
    upgrade-reader-github-pat      (fine-grained PAT: Contents+PR read on driftscribe-e2e-target)
    upgrade-docs-github-pat        (fine-grained PAT: Contents+PR write on driftscribe-e2e-target)
    developer-knowledge-api-key    (GCP API key restricted to developerknowledge.googleapis.com — see Console)
    driftscribe-webhook-url        (any 2xx-returning HTTPS endpoint; webhook.site is fine for E2E)

(2) Run the first parameterized E2E build:

    gcloud builds submit --config infra/cloudbuild.yaml \\
      --project=$PROJECT \\
      --substitutions=_TARGET_SERVICE=payment-demo-e2e,_TARGET_GITHUB_REPO=adi-prasetyo/driftscribe-e2e-target,_UPGRADE_TARGET_REPO=adi-prasetyo/driftscribe-e2e-target,_USE_ADK=true,_DRY_RUN=false

(3) Apply post-deploy IAM bindings (run these AFTER the build succeeds):

    # Rollback worker — resource-scoped run.developer on payment-demo-e2e:
    gcloud run services add-iam-policy-binding payment-demo-e2e \\
      --project=$PROJECT --region=$REGION \\
      --member="serviceAccount:${ROLLBACK_SA}" \\
      --role="roles/run.developer" --condition=None

    # E2E runner — resource-scoped run.developer on payment-demo-e2e:
    gcloud run services add-iam-policy-binding payment-demo-e2e \\
      --project=$PROJECT --region=$REGION \\
      --member="serviceAccount:${E2E_RUNNER_SA}" \\
      --role="roles/run.developer" --condition=None

    # E2E runner AND rollback worker — actAs on payment-demo-e2e's
    # runtime SA. The E2E runner needs it to drive update_service env
    # mutations during baseline fixtures. The rollback worker needs it
    # to call update_service when applying traffic shifts (the demo
    # rollback path mutates traffic on payment-demo-e2e, and Cloud Run
    # requires the caller to actAs the service's runtime SA). Without
    # the rollback-agent-sa binding, /approve 5xxs with
    # "Permission 'iam.serviceaccounts.actAs' denied".
    # If the deploy step pinned --service-account on payment-demo-e2e
    # (Phase 20+), target that SA. Otherwise the default is the project's
    # Compute Engine default SA (PROJECT_NUMBER-compute@developer.gserviceaccount.com):
    RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
    for member in "${E2E_RUNNER_SA}" "${ROLLBACK_SA}"; do
      gcloud iam service-accounts add-iam-policy-binding "\$RUNTIME_SA" \\
        --project=$PROJECT \\
        --member="serviceAccount:\$member" \\
        --role="roles/iam.serviceAccountUser" --condition=None
    done

    # E2E runner — artifactregistry.reader (project-wide). Cloud Run admin
    # API validates the *caller* can pull the image during update_service.
    # Without this, env-mutator teardown 403s. See e2e-environment.md §5.
    gcloud projects add-iam-policy-binding $PROJECT \\
      --member="serviceAccount:${E2E_RUNNER_SA}" \\
      --role="roles/artifactregistry.reader" --condition=None

    # Coordinator <-> worker run.invoker grants. These are NOT applied by
    # Cloud Build — they are printed here in the next-steps for the operator
    # to run manually after the first deploy (the worker services must exist
    # first). If you see 401s from /chat -> worker hop, apply per-service:
    for worker in driftscribe-reader driftscribe-docs driftscribe-rollback \\
                  driftscribe-notifier driftscribe-upgrade-reader \\
                  driftscribe-upgrade-docs; do
      gcloud run services add-iam-policy-binding "\$worker" \\
        --project=$PROJECT --region=$REGION \\
        --member="serviceAccount:${COORD_SA}" \\
        --role="roles/run.invoker" --condition=None
    done

(4) Seed driftscribe-e2e-target with the lodash 4.17.20 lockfile
    (see docs/runbooks/e2e-environment.md "Seed the upgrade target").

(5) Verify the deploy is healthy (see runbook "Verify the deploy").

================================================================
EOF
