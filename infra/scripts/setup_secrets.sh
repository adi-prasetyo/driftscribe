#!/usr/bin/env bash
# Idempotent bootstrap for a fresh DriftScribe multi-agent deployment.
#
# Usage:
#   setup_secrets.sh PROJECT GITHUB_TOKEN [DOCS_AGENT_PAT] [WEBHOOK_URL] [DEVELOPER_KNOWLEDGE_API_KEY] [UPGRADE_READER_PAT] [UPGRADE_DOCS_PAT]
#
# Arguments:
#   PROJECT                       GCP project ID (e.g. driftscribe-hack-2026)
#   GITHUB_TOKEN                  Classic PAT for the coordinator's read-only PR search
#                                 (repo: contents:read + pull_requests:read on the demo repo)
#   DOCS_AGENT_PAT                (optional) Fine-grained PAT scoped to ONE repository, with
#                                 Contents: write + Pull requests: write. If omitted, the
#                                 script prints instructions and SKIPS creating the secret
#                                 so the operator can re-run with the value later.
#   WEBHOOK_URL                   (optional) Demo notifier webhook URL (e.g. webhook.site).
#                                 If omitted, skipped — re-run with the value later.
#   DEVELOPER_KNOWLEDGE_API_KEY   (optional, Phase 17.B) GCP API key restricted to
#                                 `developerknowledge.googleapis.com`. Operator MUST
#                                 create this in the Console (see runbook Step 2b);
#                                 paste here to populate Secret Manager. If omitted,
#                                 the script prints instructions and skips creating
#                                 the secret — re-run with the value later.
#   UPGRADE_READER_PAT            (optional, Phase 17.E.2) Fine-grained PAT scoped to ONE
#                                 repository (adi-prasetyo/driftscribe) with
#                                 Contents: read + Pull requests: read ONLY. NO write.
#                                 Backs the upgrade-reader worker's GitHub API calls.
#                                 If omitted, skipped — re-run with the value later.
#   UPGRADE_DOCS_PAT              (optional, Phase 17.E.2) Fine-grained PAT scoped to ONE
#                                 repository (adi-prasetyo/driftscribe) with
#                                 Contents: read + write AND Pull requests: read + write.
#                                 Backs the upgrade-docs worker's PR-opening flow.
#                                 If omitted, skipped — re-run with the value later.
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

PROJECT="${1:?usage: $0 PROJECT GITHUB_TOKEN [DOCS_AGENT_PAT] [WEBHOOK_URL] [DEVELOPER_KNOWLEDGE_API_KEY] [UPGRADE_READER_PAT] [UPGRADE_DOCS_PAT]}"
GITHUB_TOKEN="${2:?}"
DOCS_AGENT_PAT="${3:-}"
WEBHOOK_URL="${4:-}"
DEVELOPER_KNOWLEDGE_API_KEY="${5:-}"
UPGRADE_READER_PAT="${6:-}"
UPGRADE_DOCS_PAT="${7:-}"

REGION="asia-northeast1"

# --------------------------------------------------------------------------
# 1. APIs
# --------------------------------------------------------------------------
# `developerknowledge.googleapis.com` (Phase 17.B) backs the Developer
# Knowledge MCP server the coordinator's ADK agent queries for authoritative
# Google docs grounding. `gcloud services enable` is idempotent.
gcloud services enable --project "$PROJECT" \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  developerknowledge.googleapis.com \
  eventarc.googleapis.com \
  eventarcpublishing.googleapis.com \
  firestore.googleapis.com \
  iamcredentials.googleapis.com \
  logging.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com

# `gcloud beta services mcp enable` (Phase 17.B) explicitly opts the project
# into the remote MCP server fronting the Developer Knowledge API. After
# 2025-03-17 Google auto-enables this when the underlying API is enabled,
# but the explicit call keeps fresh-project bootstrap deterministic and
# survives older `gcloud` versions that haven't picked up the auto-enable
# default. The call is idempotent server-side; `|| true` guards against
# transient `beta` surface churn so a stale gcloud doesn't break bootstrap.
gcloud beta services mcp enable developerknowledge.googleapis.com \
  --project="$PROJECT" >/dev/null 2>&1 || \
  echo "  note: 'gcloud beta services mcp enable' skipped (already enabled or beta surface unavailable in this gcloud version) — verify in Console if MCP requests 404"

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
# Phase 17.E.2: upgrade-reader-sa and upgrade-docs-sa added for the
# upgrade workload (one SA per worker, distinct from the drift workers).
for sa in driftscribe-agent reader-agent-sa docs-agent-sa rollback-agent-sa notifier-agent-sa upgrade-reader-sa upgrade-docs-sa; do
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
UPGRADE_READER_SA="upgrade-reader-sa@${PROJECT}.iam.gserviceaccount.com"
UPGRADE_DOCS_SA="upgrade-docs-sa@${PROJECT}.iam.gserviceaccount.com"

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
# Coordinator: Firestore (sessions/, approvals/ pending→denied flip) +
# Vertex AI (Phase 14.5 — the ADK path calls gemini-2.5-flash via Vertex
# AI's generate-content endpoint; ADC routes through this SA).
# Phase 13: run.viewer removed — classifier path migrated to Reader Worker.
for role in \
  roles/datastore.user \
  roles/aiplatform.user \
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
# Operator-supplied secrets (always present): github-pat
# Phase 14.5: gemini-api-key removed — the coordinator now reaches Gemini
# via Vertex AI ADC (no API key on either path). If an orphaned
# gemini-api-key secret exists from a pre-14.5 deploy, the operator can
# delete it manually:
#   gcloud secrets delete gemini-api-key --project=$PROJECT
# The script intentionally does NOT auto-delete it (rollback safety).
for secret in github-pat; do
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --project "$PROJECT" --replication-policy=automatic
done
printf '%s' "$GITHUB_TOKEN"   | gcloud secrets versions add github-pat     --project "$PROJECT" --data-file=-

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
    echo "  $0 $PROJECT <gh-pat> <docs-pat> [webhook-url]"
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
    echo "  $0 $PROJECT <gh-pat> <docs-pat> <webhook-url>"
    echo "----------------------------------------------------------------"
    echo
  fi
fi

# Upgrade workload's PATs (Phase 17.E.2) — operator must supply. Both are
# distinct from the drift docs-agent-github-pat: the upgrade-reader holds
# a READ-ONLY fine-grained PAT (Contents:read + Pull requests:read), the
# upgrade-docs holds a READ+WRITE fine-grained PAT (Contents:read+write +
# Pull requests:read+write). Same operator-supplied pattern as the docs
# PAT above: if omitted, the script prints instructions and SKIPS creating
# the secret so the operator can re-run with the value later.
if [ -n "$UPGRADE_READER_PAT" ]; then
  gcloud secrets describe upgrade-reader-github-pat --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create upgrade-reader-github-pat \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$UPGRADE_READER_PAT" | gcloud secrets versions add upgrade-reader-github-pat \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe upgrade-reader-github-pat --project "$PROJECT" >/dev/null 2>&1; then
    echo "upgrade-reader-github-pat already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "UPGRADE_READER_PAT arg not supplied — upgrade-reader-github-pat NOT created."
    echo
    echo "Create a READ-ONLY fine-grained GitHub PAT:"
    echo "  https://github.com/settings/personal-access-tokens/new"
    echo "  Repository access: select ONE repo (adi-prasetyo/driftscribe)"
    echo "  Permissions:"
    echo "    Contents:      Read"
    echo "    Pull requests: Read"
    echo "  NO write scopes — defense in depth (upgrade-reader is read-only)."
    echo "Then re-run with the value as the 6th positional arg:"
    echo "  $0 \$PROJECT \$GH_TOKEN \$DOCS_PAT \$WEBHOOK_URL \$DEV_KEY <upgrade-reader-pat> [upgrade-docs-pat]"
    echo "----------------------------------------------------------------"
    echo
  fi
fi

if [ -n "$UPGRADE_DOCS_PAT" ]; then
  gcloud secrets describe upgrade-docs-github-pat --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create upgrade-docs-github-pat \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$UPGRADE_DOCS_PAT" | gcloud secrets versions add upgrade-docs-github-pat \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe upgrade-docs-github-pat --project "$PROJECT" >/dev/null 2>&1; then
    echo "upgrade-docs-github-pat already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "UPGRADE_DOCS_PAT arg not supplied — upgrade-docs-github-pat NOT created."
    echo
    echo "Create a READ+WRITE fine-grained GitHub PAT (separate from"
    echo "docs-agent-github-pat — same repo, different scopes):"
    echo "  https://github.com/settings/personal-access-tokens/new"
    echo "  Repository access: select ONE repo (adi-prasetyo/driftscribe)"
    echo "  Permissions:"
    echo "    Contents:      Read and write"
    echo "    Pull requests: Read and write"
    echo "Then re-run with the value as the 7th positional arg:"
    echo "  $0 \$PROJECT \$GH_TOKEN \$DOCS_PAT \$WEBHOOK_URL \$DEV_KEY \$UPGRADE_READER_PAT <upgrade-docs-pat>"
    echo "----------------------------------------------------------------"
    echo
  fi
fi

# Developer Knowledge API key (Phase 17.B) — operator must supply. The key
# itself MUST be created by the operator in the GCP Console with an
# API-restriction binding it to `developerknowledge.googleapis.com` only
# (see docs/runbooks/deploy.md Step 3b). Console flow is preferred over
# `gcloud services api-keys create` because the Console UI enforces the
# API-restriction selection inline; the gcloud equivalent is fragile across
# versions. Same operator-supplied pattern as the GitHub PAT and webhook
# URL above.
if [ -n "$DEVELOPER_KNOWLEDGE_API_KEY" ]; then
  gcloud secrets describe developer-knowledge-api-key --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create developer-knowledge-api-key \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$DEVELOPER_KNOWLEDGE_API_KEY" | gcloud secrets versions add developer-knowledge-api-key \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe developer-knowledge-api-key --project "$PROJECT" >/dev/null 2>&1; then
    echo "developer-knowledge-api-key already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "DEVELOPER_KNOWLEDGE_API_KEY arg not supplied — developer-knowledge-api-key NOT created."
    echo
    echo "Create the API key in the GCP Console (recommended for correct"
    echo "API restriction):"
    echo "  1. https://console.cloud.google.com/apis/credentials?project=${PROJECT}"
    echo "  2. + Create credentials → API key"
    echo "  3. After creation, Edit API key → Restrict key → API restrictions"
    echo "     → 'Restrict key' → select ONLY 'Developer Knowledge API'"
    echo "  4. Optional: under Application restrictions, set 'None' (no IP /"
    echo "     referrer pin is needed — Cloud Run's egress IPs aren't stable)"
    echo "Then re-run:"
    echo "  $0 $PROJECT <gh-pat> <docs-pat> <webhook-url> <dev-knowledge-key>"
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

# Coordinator: three secrets (Phase 14.5: gemini-api-key removed — Vertex
# AI ADC replaces the API-key auth path; Phase 17.B: developer-knowledge-api-key
# added for the Developer Knowledge MCP toolset). Each binding is scoped to
# the single secret resource — the coordinator has NO project-wide
# secretmanager.secretAccessor grant.
bind_secret coordinator-shared-token       "$COORD_SA"
bind_secret github-pat                     "$COORD_SA"
bind_secret developer-knowledge-api-key    "$COORD_SA"

# Docs worker: one secret (its fine-grained PAT).
bind_secret docs-agent-github-pat     "$DOCS_SA"

# Rollback worker: one secret (the HMAC key).
bind_secret approval-hmac-key         "$ROLLBACK_SA"

# Notifier worker: one secret (the outbound webhook URL).
bind_secret driftscribe-webhook-url   "$NOTIFIER_SA"

# Upgrade workers (Phase 17.E.2): one secret each, distinct fine-grained
# PATs. The upgrade-reader's PAT is read-only; the upgrade-docs' PAT is
# read+write. Defense in depth: neither SA can read the other's PAT, so
# a compromise of the read-only worker cannot escalate to PR creation.
bind_secret upgrade-reader-github-pat "$UPGRADE_READER_SA"
bind_secret upgrade-docs-github-pat   "$UPGRADE_DOCS_SA"

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
# Phase 17.E.2: upgrade workers (driftscribe-upgrade-reader,
# driftscribe-upgrade-docs) added to the loop. The grants stay per-service
# — the coordinator's run.invoker on a drift worker does NOT extend to
# an upgrade worker (workload-scoped IAM invariant, pinned in
# docs/architecture/iam-matrix.md).
for worker in driftscribe-reader driftscribe-docs driftscribe-rollback driftscribe-notifier driftscribe-upgrade-reader driftscribe-upgrade-docs; do
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

# --------------------------------------------------------------------------
# 10. Eventarc trigger SA + driftscribe-cloudrun-changes trigger
# --------------------------------------------------------------------------
# Both `eventarc triggers describe` and the trigger create are regional —
# the gates below pass --location=$REGION explicitly.
EVENTARC_SA="eventarc-trigger-sa@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$EVENTARC_SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create eventarc-trigger-sa \
    --project "$PROJECT" --display-name="DriftScribe Eventarc trigger SA"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${EVENTARC_SA}" \
  --role="roles/eventarc.eventReceiver" >/dev/null

# run.invoker grant AND trigger create both depend on the coordinator
# existing. On first-ever run (before Cloud Build) both are no-ops; re-run
# after the build to apply.
if gcloud run services describe driftscribe-agent \
   --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud run services add-iam-policy-binding driftscribe-agent \
    --project="$PROJECT" --region="$REGION" \
    --member="serviceAccount:${EVENTARC_SA}" --role="roles/run.invoker" >/dev/null
  echo "driftscribe-agent: granted run.invoker on eventarc-trigger-sa"

  if gcloud eventarc triggers describe driftscribe-cloudrun-changes \
       --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    echo "eventarc trigger driftscribe-cloudrun-changes already exists — skipping create"
  else
    gcloud eventarc triggers create driftscribe-cloudrun-changes \
      --project="$PROJECT" --location="$REGION" \
      --destination-run-service=driftscribe-agent \
      --destination-run-region="$REGION" \
      --destination-run-path=/eventarc \
      --event-filters="type=google.cloud.audit.log.v1.written" \
      --event-filters="serviceName=run.googleapis.com" \
      --event-filters="methodName=google.cloud.run.v2.Services.UpdateService" \
      --event-filters="resourceName=projects/${PROJECT}/locations/${REGION}/services/payment-demo" \
      --service-account="${EVENTARC_SA}"
    echo "eventarc trigger driftscribe-cloudrun-changes: created"
  fi
else
  echo "driftscribe-agent not deployed yet — skipping run.invoker grant + trigger create"
fi

echo
echo "  next: confirm the trigger filter matches what your env emits —"
echo "    see docs/runbooks/deploy.md → 'confirm Eventarc trigger fires' (mutate payment-demo, check audit log + handler logs)"

# --------------------------------------------------------------------------
# 11. Log retention — extend `_Default` bucket to 365 days (Phase 18.A)
# --------------------------------------------------------------------------
# Default Cloud Logging `_Default` bucket retention is 30 days. After that,
# every DriftScribe log line (including the thought-summary, tool-call,
# and LLM-usage records emitted by Phase 18.B) ages out and is unrecoverable.
#
# Extending retention is the cheapest, simplest durable-copy option for the
# hackathon's volume profile (<1 GiB/month): no sink, no BigQuery dataset,
# no GCS bucket, no IAM grants. Storage beyond the first 30 days is billed
# at $0.01/GiB-month. The Logs Explorer query surface stays identical.
#
# Describe-then-act, matching the rest of the script: re-runs on a
# project already at 365 days print a "skipping" line instead of falsely
# claiming the bucket was just extended. `--location=global` is explicit
# so gcloud does not prompt for it on a fresh shell. The update itself
# is also idempotent server-side, so the guard is purely a UX win.
current="$(gcloud logging buckets describe _Default \
  --project="$PROJECT" --location=global \
  --format='value(retentionDays)' 2>/dev/null || echo 0)"
if [[ "$current" != "365" ]]; then
  gcloud logging buckets update _Default \
    --project="$PROJECT" --location=global \
    --retention-days=365 >/dev/null
  echo "  log retention: _Default bucket extended from ${current} to 365 days"
else
  echo "  log retention: _Default bucket already at 365 days — skipping"
fi

# --------------------------------------------------------------------------
# 12. Cloud Logging read access for /trace endpoint (Phase 19.A.0)
# --------------------------------------------------------------------------
# The coordinator's `/trace` endpoint calls `logEntries.list` to replay
# thought-summary, tool-call, and llm-usage events out of the `_Default`
# bucket extended above (§11). On a developer workstation this works
# under ADC because the operator already holds project-wide read access;
# on Cloud Run the runtime SA has no logging read role by default, so
# every `/trace` request 403s with `PERMISSION_DENIED`.
#
# `roles/logging.viewer` is the smallest role that grants
# `logging.logEntries.list` + `logging.logs.list` project-wide. It is
# strictly read-only — no write, no admin, no sink management — and
# scoped to the coordinator SA only (NO project-wide grant to humans
# or to worker SAs).
#
# Describe-then-act, matching §11 (log retention) and the rest of the
# script: the filter pulls the existing binding (if any) for this exact
# (role, member) pair. If the lookup returns empty, we add the binding
# with `--condition=None` (explicit no-condition to avoid the unbound-
# condition warning gcloud emits on conditional-policy projects) and
# `--quiet` so a re-run prints a deterministic single-line skip instead
# of a y/N prompt. The `add-iam-policy-binding` call is also idempotent
# server-side, so the guard is purely a UX win — re-runs on an already-
# bound project print "skipping" instead of falsely claiming a fresh
# grant.
sa_email="${COORD_SA}"
role="roles/logging.viewer"
# Note: `|| true` collapses lookup-failed and lookup-empty into the same
# "treat as missing" branch. Under transient `get-iam-policy` failure
# (network blip, 5xx, missing read perm) a re-run that *should* print
# `already bound … — skipping` will print `granted to …` instead. The
# `add-iam-policy-binding` call is server-side idempotent, so this is a
# logging-truthfulness regression, not a correctness bug. Matches the
# `|| true` pattern in §11 above.
existing="$(gcloud projects get-iam-policy "$PROJECT" \
  --flatten='bindings[].members' \
  --format="value(bindings.members)" \
  --filter="bindings.role=${role} AND bindings.members=serviceAccount:${sa_email}" \
  2>/dev/null || true)"
if [[ -z "$existing" ]]; then
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${sa_email}" \
    --role="${role}" --condition=None --quiet >/dev/null
  echo "  logging.viewer: granted to ${sa_email}"
else
  echo "  logging.viewer: already bound to ${sa_email} — skipping"
fi

echo
echo "setup_secrets.sh: complete"
echo "  next: docs/runbooks/deploy.md (steps 2+)"
