#!/usr/bin/env bash
# Idempotent bootstrap for a fresh DriftScribe multi-agent deployment.
#
# Usage:
#   setup_secrets.sh PROJECT GITHUB_TOKEN GOOGLE_API_KEY [DOCS_AGENT_PAT] [WEBHOOK_URL]
#
# Arguments:
#   PROJECT           GCP project ID (e.g. driftscribe-hack-2026)
#   GITHUB_TOKEN      Classic PAT for the coordinator's read-only PR search
#                     (repo: contents:read + pull_requests:read on the demo repo)
#   GOOGLE_API_KEY    Gemini API key (https://aistudio.google.com)
#   DOCS_AGENT_PAT    (optional) Fine-grained PAT scoped to ONE repository, with
#                     Contents: write + Pull requests: write. If omitted, the
#                     script prints instructions and SKIPS creating the secret
#                     so the operator can re-run with the value later.
#   WEBHOOK_URL       (optional) Demo notifier webhook URL (e.g. webhook.site).
#                     If omitted, skipped — re-run with the value later.
#
# Safe to re-run: every gcloud create is gated by a describe-check, every IAM
# binding is idempotent server-side, and the two auto-generated secrets
# (coordinator-shared-token, approval-hmac-key) are ONLY created on first run
# (regenerating them would invalidate every running deploy).
#
# Two-phase usage (see docs/runbooks/deploy.md for the full operator runbook):
#   1. First run: stand up SAs, IAM, secrets BEFORE the first Cloud Build.
#      The per-worker run.invoker grants are no-ops because the worker
#      services do not yet exist; the script logs "skipping (service not
#      deployed yet)" and continues.
#   2. After the first `gcloud builds submit`: re-run this script. It now
#      detects the worker services exist and applies the run.invoker
#      grants to the coordinator SA on each worker.

set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT GITHUB_TOKEN GOOGLE_API_KEY [DOCS_AGENT_PAT] [WEBHOOK_URL]}"
GITHUB_TOKEN="${2:?}"
GOOGLE_API_KEY="${3:?}"
DOCS_AGENT_PAT="${4:-}"
WEBHOOK_URL="${5:-}"

REGION="asia-northeast1"

# --------------------------------------------------------------------------
# 1. APIs
# --------------------------------------------------------------------------
gcloud services enable --project "$PROJECT" \
  run.googleapis.com \
  eventarc.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com

# --------------------------------------------------------------------------
# 2. Artifact Registry
# --------------------------------------------------------------------------
gcloud artifacts repositories describe driftscribe \
  --project "$PROJECT" --location="$REGION" >/dev/null 2>&1 || \
gcloud artifacts repositories create driftscribe \
  --project "$PROJECT" --location="$REGION" --repository-format=docker \
  --description="DriftScribe agent + worker + payment-demo images"

# --------------------------------------------------------------------------
# 3. Cloud Build SA grants (project-wide)
# --------------------------------------------------------------------------
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/iam.serviceAccountUser \
; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${CLOUDBUILD_SA}" --role="$role" >/dev/null
done

# --------------------------------------------------------------------------
# 4. Service Accounts — 1 coordinator + 4 workers (idempotent)
# --------------------------------------------------------------------------
# driftscribe-agent replaces the default compute SA as the coordinator's
# runtime identity. Workers' ALLOWED_CALLERS env lists this SA's email.
for sa in driftscribe-agent reader-agent-sa docs-agent-sa rollback-agent-sa notifier-agent-sa; do
  gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$sa" \
      --project="$PROJECT" \
      --display-name="DriftScribe ${sa}"
done

COORD_SA="driftscribe-agent@${PROJECT}.iam.gserviceaccount.com"
READER_SA="reader-agent-sa@${PROJECT}.iam.gserviceaccount.com"
DOCS_SA="docs-agent-sa@${PROJECT}.iam.gserviceaccount.com"
ROLLBACK_SA="rollback-agent-sa@${PROJECT}.iam.gserviceaccount.com"
NOTIFIER_SA="notifier-agent-sa@${PROJECT}.iam.gserviceaccount.com"

# Cloud Build acts-as each runtime SA during `gcloud run deploy`.
for sa in "$COORD_SA" "$READER_SA" "$DOCS_SA" "$ROLLBACK_SA" "$NOTIFIER_SA"; do
  gcloud iam service-accounts add-iam-policy-binding "$sa" \
    --project="$PROJECT" \
    --member="serviceAccount:${CLOUDBUILD_SA}" \
    --role="roles/iam.serviceAccountUser" >/dev/null
done

# --------------------------------------------------------------------------
# 5. Per-SA project-level IAM grants
# --------------------------------------------------------------------------
# Coordinator: Firestore only (sessions/, approvals/ pending→denied flip).
# Phase 13: run.viewer removed — classifier path migrated to Reader Worker.
for role in \
  roles/datastore.user \
; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${COORD_SA}" --role="$role" >/dev/null
done

# Idempotent cleanup for pre-Phase-13 deploys: remove the legacy
# project-wide run.viewer grant that the classifier path no longer
# needs. The `|| true` makes this safe on a fresh project where the
# binding never existed.
gcloud projects remove-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${COORD_SA}" --role="roles/run.viewer" >/dev/null 2>&1 || true

# Reader: project-wide run.viewer (reads any service's revision+env).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${READER_SA}" --role="roles/run.viewer" >/dev/null

# Docs: NO project-level grants. Per-secret binding applied below.

# Rollback: project-wide datastore.user (acknowledged Firestore-IAM constraint
# — the approvals/ collection is the only thing the worker reads/writes).
# The resource-scoped run.developer on payment-demo is below (§7).
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${ROLLBACK_SA}" --role="roles/datastore.user" >/dev/null

# Notifier: NO project-level grants. Per-secret binding applied below.

# --------------------------------------------------------------------------
# 6. Secrets — create resources (idempotent) then bind per-SA accessors
# --------------------------------------------------------------------------
# Operator-supplied secrets (always present): github-pat, gemini-api-key
for secret in github-pat gemini-api-key; do
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --project "$PROJECT" --replication-policy=automatic
done
printf '%s' "$GITHUB_TOKEN"   | gcloud secrets versions add github-pat     --project "$PROJECT" --data-file=-
printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --project "$PROJECT" --data-file=-

# Auto-generated secrets — first-run only. Regenerating these would
# invalidate every running revision (coordinator-shared-token is the
# X-DriftScribe-Token header value; approval-hmac-key signs approval
# tokens with a 15-min TTL).
if ! gcloud secrets describe coordinator-shared-token --project "$PROJECT" >/dev/null 2>&1; then
  RANDOM_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  gcloud secrets create coordinator-shared-token \
    --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$RANDOM_TOKEN" | gcloud secrets versions add coordinator-shared-token \
    --project "$PROJECT" --data-file=-
  echo
  echo "================================================================"
  echo "SAVE THIS TOKEN — you need it to call /chat and /recheck:"
  echo "  X-DriftScribe-Token: ${RANDOM_TOKEN}"
  echo "================================================================"
  echo
else
  echo "coordinator-shared-token already exists — leaving untouched"
fi

if ! gcloud secrets describe approval-hmac-key --project "$PROJECT" >/dev/null 2>&1; then
  RANDOM_HMAC="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  gcloud secrets create approval-hmac-key \
    --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$RANDOM_HMAC" | gcloud secrets versions add approval-hmac-key \
    --project "$PROJECT" --data-file=-
  echo "approval-hmac-key created (no operator-facing surface)"
else
  echo "approval-hmac-key already exists — leaving untouched"
fi

# Docs Agent's fine-grained PAT — operator must supply. If omitted, print
# instructions and skip; re-run later with the arg.
if [ -n "$DOCS_AGENT_PAT" ]; then
  gcloud secrets describe docs-agent-github-pat --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create docs-agent-github-pat \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$DOCS_AGENT_PAT" | gcloud secrets versions add docs-agent-github-pat \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe docs-agent-github-pat --project "$PROJECT" >/dev/null 2>&1; then
    echo "docs-agent-github-pat already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "DOCS_AGENT_PAT arg not supplied — docs-agent-github-pat NOT created."
    echo
    echo "Create a fine-grained GitHub PAT manually:"
    echo "  https://github.com/settings/personal-access-tokens/new"
    echo "  Repository access: select ONE repo (adi-prasetyo/driftscribe)"
    echo "  Permissions:"
    echo "    Contents:      Read and write"
    echo "    Pull requests: Read and write"
    echo "Then re-run:"
    echo "  $0 $PROJECT <gh-pat> <gemini-key> <docs-pat> [webhook-url]"
    echo "----------------------------------------------------------------"
    echo
  fi
fi

# Notifier's webhook URL — operator must supply. Same pattern as docs.
if [ -n "$WEBHOOK_URL" ]; then
  gcloud secrets describe driftscribe-webhook-url --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create driftscribe-webhook-url \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$WEBHOOK_URL" | gcloud secrets versions add driftscribe-webhook-url \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe driftscribe-webhook-url --project "$PROJECT" >/dev/null 2>&1; then
    echo "driftscribe-webhook-url already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "WEBHOOK_URL arg not supplied — driftscribe-webhook-url NOT created."
    echo
    echo "Create a demo webhook URL at https://webhook.site (or any HTTPS"
    echo "endpoint you control), then re-run:"
    echo "  $0 $PROJECT <gh-pat> <gemini-key> <docs-pat> <webhook-url>"
    echo "----------------------------------------------------------------"
    echo
  fi
fi

# --------------------------------------------------------------------------
# 6b. Per-secret IAM bindings — every grant is scoped to a single secret
# resource. NO project-wide secretmanager.secretAccessor anywhere.
# --------------------------------------------------------------------------
bind_secret() {
  local secret="$1" member="$2"
  if gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$secret" \
      --project "$PROJECT" \
      --member="serviceAccount:${member}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
  else
    echo "  skipping bind: secret ${secret} not created yet"
  fi
}

# Coordinator: three secrets.
bind_secret coordinator-shared-token "$COORD_SA"
bind_secret github-pat                "$COORD_SA"
bind_secret gemini-api-key            "$COORD_SA"

# Docs worker: one secret (its fine-grained PAT).
bind_secret docs-agent-github-pat     "$DOCS_SA"

# Rollback worker: one secret (the HMAC key).
bind_secret approval-hmac-key         "$ROLLBACK_SA"

# Notifier worker: one secret (the outbound webhook URL).
bind_secret driftscribe-webhook-url   "$NOTIFIER_SA"

# --------------------------------------------------------------------------
# 7. Rollback worker — resource-scoped run.developer on payment-demo ONLY
# --------------------------------------------------------------------------
# The payment-demo service must already exist (created by an earlier
# `gcloud builds submit`). On first-ever run this binding is skipped.
if gcloud run services describe payment-demo \
   --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud run services add-iam-policy-binding payment-demo \
    --project="$PROJECT" --region="$REGION" \
    --member="serviceAccount:${ROLLBACK_SA}" \
    --role="roles/run.developer" >/dev/null
  echo "rollback-agent-sa: granted run.developer on payment-demo (resource-scoped)"
else
  echo "payment-demo not deployed yet — skipping rollback resource-scoped run.developer"
  echo "  re-run this script after the first 'gcloud builds submit' to apply it"
fi

# --------------------------------------------------------------------------
# 8. Coordinator → worker per-service run.invoker grants
# --------------------------------------------------------------------------
# Cloud Run inter-service auth (Phase 11.0 spike) requires the calling SA
# to hold run.invoker on the receiving service. These are PER-SERVICE
# bindings, not project-wide — even a compromised coordinator can only
# call the four services it has been granted access to.
#
# Gated on service existence: on first run (before any `gcloud builds
# submit`) the workers don't exist and this loop is a no-op. After the
# first build, re-running this script applies the grants.
for worker in driftscribe-reader driftscribe-docs driftscribe-rollback driftscribe-notifier; do
  if gcloud run services describe "$worker" \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud run services add-iam-policy-binding "$worker" \
      --project="$PROJECT" --region="$REGION" \
      --member="serviceAccount:${COORD_SA}" \
      --role="roles/run.invoker" >/dev/null
    echo "driftscribe-agent: granted run.invoker on ${worker}"
  else
    echo "${worker} not deployed yet — skipping coordinator run.invoker grant"
  fi
done

# --------------------------------------------------------------------------
# 9. Firestore Native
# --------------------------------------------------------------------------
gcloud firestore databases describe --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud firestore databases create --project "$PROJECT" --location="$REGION" --type=firestore-native

echo
echo "setup_secrets.sh: complete"
echo "  next: docs/runbooks/deploy.md (steps 2+)"
