#!/usr/bin/env bash
# Idempotent provisioning for the DriftScribe PROD/portfolio GCP project
# (``driftscribe-hack-2026``).
#
# This is the full multi-worker topology that ``infra/cloudbuild.yaml``
# deploys: coordinator (driftscribe-agent) + 6 workers + payment-demo,
# each under its own runtime service account, with per-secret IAM and
# audience-bound coordinator->worker invocation.
#
# It mirrors ``setup_e2e_project.sh`` EXCEPT it does NOT create the
# ``e2e-runner-sa`` (the WIF identity GitHub Actions impersonates to
# drive E2E runs). Prod has no GitHub-Actions-impersonable SA — keeping
# that surface out of the production tenant is deliberate. The two
# scripts share ``_setup_lib.sh``; the only structural difference is
# the runner SA and the printed next-steps.
#
# Usage:
#   PROJECT_PROD=driftscribe-hack-2026 infra/scripts/setup_prod_project.sh
#
# After this script runs once and exits 0, the operator MUST:
#   1. Populate the secrets that don't have a version yet (this script
#      creates the resources empty; Cloud Run validates --set-secrets at
#      deploy time and fails INVALID_ARGUMENT if a referenced secret has
#      no version).
#   2. Run the full build (see printed next-steps).
#   3. Apply the printed post-deploy IAM bindings (coordinator
#      run.invoker on each worker; rollback run.developer + actAs on
#      payment-demo) — they reference resources that only exist AFTER the
#      first build, so the script PRINTS but does not EXECUTE them.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

PROJECT="${PROJECT_PROD:?usage: PROJECT_PROD=<project-id> $0 (e.g. PROJECT_PROD=driftscribe-hack-2026)}"
REGION="asia-northeast1"
# Prod deploy targets (cloudbuild.yaml substitution defaults). Hardcoded
# here only for the printed next-steps + PAT-scoping reminders.
TARGET_SERVICE="payment-demo"
TARGET_GITHUB_REPO="adi-prasetyo/driftscribe"

# --------------------------------------------------------------------------
# 0. Pre-flight: confirm the project exists and the caller can act on it.
# --------------------------------------------------------------------------
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: project ${PROJECT} does not exist or the active gcloud" >&2
  echo "       account does not have describe permission on it." >&2
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
# 1. APIs — full prod parity (matches setup_e2e_project.sh:70-82).
# --------------------------------------------------------------------------
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

enable_mcp_idempotent "$PROJECT" developerknowledge.googleapis.com

# --------------------------------------------------------------------------
# 2. Artifact Registry — docker repo the cloudbuild pushes to.
# --------------------------------------------------------------------------
create_artifact_repo_idempotent "$PROJECT" driftscribe "$REGION" \
  "DriftScribe prod images (agent + workers + payment-demo)"

# --------------------------------------------------------------------------
# 3. Cloud Build SA grants — same matrix as e2e.
# --------------------------------------------------------------------------
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
CLOUDBUILD_SA=""
if CLOUDBUILD_SA="$(gcloud builds get-default-service-account \
      --project="$PROJECT" --format='value(serviceAccountEmail)' 2>/dev/null)" \
   && [ -n "$CLOUDBUILD_SA" ]; then
  CLOUDBUILD_SA="${CLOUDBUILD_SA##*/}"
  echo "Cloud Build default SA resolved: ${CLOUDBUILD_SA}"
else
  CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
  echo "Cloud Build default SA fallback: ${CLOUDBUILD_SA}"
fi
for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
; do
  grant_role_idempotent "$PROJECT" "serviceAccount:${CLOUDBUILD_SA}" "$role"
done
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
# 4. Service Accounts — 7 runtime SAs (NO e2e-runner-sa).
# --------------------------------------------------------------------------
for sa in \
  driftscribe-agent \
  reader-agent-sa \
  docs-agent-sa \
  rollback-agent-sa \
  notifier-agent-sa \
  upgrade-reader-sa \
  upgrade-docs-sa \
; do
  create_service_account_idempotent "$PROJECT" "$sa" "DriftScribe ${sa}"
done

COORD_SA="driftscribe-agent@${PROJECT}.iam.gserviceaccount.com"
READER_SA="reader-agent-sa@${PROJECT}.iam.gserviceaccount.com"
DOCS_SA="docs-agent-sa@${PROJECT}.iam.gserviceaccount.com"
ROLLBACK_SA="rollback-agent-sa@${PROJECT}.iam.gserviceaccount.com"
NOTIFIER_SA="notifier-agent-sa@${PROJECT}.iam.gserviceaccount.com"
UPGRADE_READER_SA="upgrade-reader-sa@${PROJECT}.iam.gserviceaccount.com"
UPGRADE_DOCS_SA="upgrade-docs-sa@${PROJECT}.iam.gserviceaccount.com"

# Cloud Build acts-as each runtime SA during ``gcloud run deploy``.
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
# Coordinator: Vertex AI + logging.viewer (for /trace's logEntries.list) +
# Firestore datastore.user CONDITIONED to the (default) database (Phase C5f).
# aiplatform.user is what makes USE_ADK=true work via Vertex ADC
# (GOOGLE_GENAI_USE_VERTEXAI=true). The (default)-condition denies the coordinator
# the named plan-approvals DB (the C4 worker's sole-writer collection).
for role in \
  roles/aiplatform.user \
  roles/logging.viewer \
; do
  grant_role_idempotent "$PROJECT" "serviceAccount:${COORD_SA}" "$role"
done
grant_datastore_user_for_db "$PROJECT" "serviceAccount:${COORD_SA}" "(default)"

# Reader: project-wide run.viewer.
grant_role_idempotent "$PROJECT" "serviceAccount:${READER_SA}" "roles/run.viewer"

# Rollback: datastore.user CONDITIONED to (default) (C5f) — the approvals/
# collection lives in (default). Resource-scoped run.developer on payment-demo is a
# post-deploy step; see "next steps" block.
grant_datastore_user_for_db "$PROJECT" "serviceAccount:${ROLLBACK_SA}" "(default)"

# C5f cutover (gated): remove the pre-isolation UN-conditioned project-wide
# datastore.user so the (default)-conditioned grants above are the only datastore
# access (run with SETUP_PLAN_APPROVALS_DB=1 after the empirical CEL proof).
if [ "${SETUP_PLAN_APPROVALS_DB:-0}" = "1" ]; then
  remove_unconditioned_datastore_user "$PROJECT" "serviceAccount:${COORD_SA}"
  remove_unconditioned_datastore_user "$PROJECT" "serviceAccount:${ROLLBACK_SA}"
  echo "  C5f: removed UN-conditioned datastore.user from coordinator + rollback (isolation ACTIVE)"
fi

# --------------------------------------------------------------------------
# 6. Secrets — create resources (operator populates values afterward).
# --------------------------------------------------------------------------
# github-pat already exists in prod; create_secret_idempotent no-ops it.
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
# 6b. Per-secret accessor bindings (no project-wide secretAccessor).
# --------------------------------------------------------------------------
bind_secret_accessor "$PROJECT" coordinator-shared-token       "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" github-pat                     "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" developer-knowledge-api-key    "serviceAccount:${COORD_SA}"
bind_secret_accessor "$PROJECT" docs-agent-github-pat          "serviceAccount:${DOCS_SA}"
bind_secret_accessor "$PROJECT" approval-hmac-key              "serviceAccount:${ROLLBACK_SA}"
bind_secret_accessor "$PROJECT" driftscribe-webhook-url        "serviceAccount:${NOTIFIER_SA}"
bind_secret_accessor "$PROJECT" upgrade-reader-github-pat      "serviceAccount:${UPGRADE_READER_SA}"
bind_secret_accessor "$PROJECT" upgrade-docs-github-pat        "serviceAccount:${UPGRADE_DOCS_SA}"

# --------------------------------------------------------------------------
# 7. Firestore Native — same region as e2e (asia-northeast1).
# --------------------------------------------------------------------------
create_firestore_native_idempotent "$PROJECT" "$REGION"

# --------------------------------------------------------------------------
# 8. Log retention — extend _Default bucket to 365 days.
# --------------------------------------------------------------------------
extend_log_retention_idempotent "$PROJECT" 365

# --------------------------------------------------------------------------
# 9. Post-deploy reminders — these only work AFTER the first build.
# --------------------------------------------------------------------------
cat <<EOF

================================================================
setup_prod_project.sh: complete

Next steps (operator action required):

(1) Populate any secret that has no version yet:

    printf '%s' "<value>" | gcloud secrets versions add <name> \\
      --project "$PROJECT" --data-file=-

  For each of:
    coordinator-shared-token       (auto-generate: python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    approval-hmac-key              (auto-generate: same as above)
    github-pat                     (ALREADY POPULATED in prod — leave as-is)
    docs-agent-github-pat          (fine-grained PAT: Contents+PR write on ${TARGET_GITHUB_REPO})
    upgrade-reader-github-pat      (fine-grained PAT: Contents+PR read on ${TARGET_GITHUB_REPO})
    upgrade-docs-github-pat        (fine-grained PAT: Contents+PR write on ${TARGET_GITHUB_REPO})
    developer-knowledge-api-key    (GCP API key restricted to developerknowledge.googleapis.com)
    driftscribe-webhook-url        (any 2xx-returning HTTPS endpoint; webhook.site works)

(2) Run the full build (USE_ADK=false FIRST so the live coordinator
    keeps its current behavior during the deploy; flip to true in step 4
    once workers + IAM are in place):

    gcloud builds submit --config infra/cloudbuild.yaml \\
      --project=$PROJECT \\
      --substitutions=_TAG=\$(git rev-parse --short HEAD),_USE_ADK=false,_DRY_RUN=false

    (TARGET_SERVICE/GITHUB_REPO/UPGRADE_TARGET_REPO default to prod values:
     ${TARGET_SERVICE}, ${TARGET_GITHUB_REPO}.)

(3) Apply post-deploy IAM (AFTER the build succeeds — services must exist):

    # Coordinator -> worker run.invoker (else /chat -> worker hop 401s):
    for worker in driftscribe-reader driftscribe-docs driftscribe-rollback \\
                  driftscribe-notifier driftscribe-upgrade-reader \\
                  driftscribe-upgrade-docs; do
      gcloud run services add-iam-policy-binding "\$worker" \\
        --project=$PROJECT --region=$REGION \\
        --member="serviceAccount:${COORD_SA}" \\
        --role="roles/run.invoker" --condition=None
    done

    # Rollback worker — resource-scoped run.developer on ${TARGET_SERVICE}
    # + actAs on its runtime SA (required for real HITL-approved traffic
    # shifts). Phase C5f: the dedicated minimal runtime SA payment-demo-runtime
    # (provisioned by setup_secrets.sh §7b) becomes ${TARGET_SERVICE}'s identity
    # once the repoint is applied through the gated pipeline. The default compute
    # SA was RETIRED (2026-05-31), so on a FRESH bootstrap LIVE_RUNTIME_SA falls
    # back to the dedicated payment-demo-runtime SA — NOT the retired compute SA;
    # we grant actAs on BOTH the LIVE-resolved runtime SA AND the dedicated SA, so
    # a rollback works whether the service is pre- or post-repoint.
    gcloud run services add-iam-policy-binding ${TARGET_SERVICE} \\
      --project=$PROJECT --region=$REGION \\
      --member="serviceAccount:${ROLLBACK_SA}" \\
      --role="roles/run.developer" --condition=None
    LIVE_RUNTIME_SA="\$(gcloud run services describe ${TARGET_SERVICE} \\
      --project=$PROJECT --region=$REGION \\
      --format='value(template.serviceAccount)' 2>/dev/null)"
    : "\${LIVE_RUNTIME_SA:=payment-demo-runtime@$PROJECT.iam.gserviceaccount.com}"
    for RUNTIME_SA in "\$LIVE_RUNTIME_SA" "payment-demo-runtime@$PROJECT.iam.gserviceaccount.com"; do
      gcloud iam service-accounts add-iam-policy-binding "\$RUNTIME_SA" \\
        --project=$PROJECT \\
        --member="serviceAccount:${ROLLBACK_SA}" \\
        --role="roles/iam.serviceAccountUser" --condition=None
    done

(4) Flip ADK on:

    gcloud run services update driftscribe-agent \\
      --project=$PROJECT --region=$REGION \\
      --update-env-vars=USE_ADK=true

(5) Verify: /health, then a /chat drift prompt returns a real ADK reply
    (worker hop succeeds). Browser-test the UI behind Cloudflare Access.

================================================================
EOF
