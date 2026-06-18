#!/usr/bin/env bash
# Idempotent bootstrap for a fresh DriftScribe multi-agent deployment.
#
# Usage:
#   setup_secrets.sh PROJECT GITHUB_TOKEN [DOCS_AGENT_PAT] [WEBHOOK_URL] [DEVELOPER_KNOWLEDGE_API_KEY] [UPGRADE_READER_PAT] [UPGRADE_DOCS_PAT] [TOFU_EDITOR_PAT]
#
# Arguments:
#   PROJECT                       GCP project ID (e.g. driftscribe-hack-2026)
#   GITHUB_TOKEN                  Fine-grained PAT for the coordinator (Phase C5f), scoped to
#                                 the single repo adi-prasetyo/driftscribe with Contents: write
#                                 (C5e merges approved IaC PRs), Pull requests: read, Checks: read
#                                 (check-runs only). NOT a classic PAT, NOT multi-repo, no admin.
#                                 (Legacy: was described as a classic read-only PR-search PAT — the
#                                 coordinator now merges PRs, so Contents: write is required.)
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
#   TOFU_EDITOR_PAT               (optional, Phase D / D3-1) Fine-grained PAT scoped to ONE
#                                 repository (adi-prasetyo/driftscribe) with
#                                 Contents: read + write AND Pull requests: read + write.
#                                 Backs the tofu-editor worker's iac/-PR-opening flow.
#                                 DISTINCT from the docs / upgrade-docs PATs (separate
#                                 secret, separate SA). NO admin, NO Issues, NO Actions,
#                                 NO second repo. If omitted, skipped — re-run later.
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

# Phase 20: shared idempotent helpers (create_secret_idempotent,
# grant_role_idempotent, bind_secret_accessor, ...) live in _setup_lib.sh
# so both this script and the new infra/scripts/setup_e2e_project.sh can
# call them. Sourcing is a no-op for behavior — the original inline
# gcloud calls below are unchanged — but the helpers are available for
# any future site that wants to deduplicate.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

PROJECT="${1:?usage: $0 PROJECT GITHUB_TOKEN [DOCS_AGENT_PAT] [WEBHOOK_URL] [DEVELOPER_KNOWLEDGE_API_KEY] [UPGRADE_READER_PAT] [UPGRADE_DOCS_PAT] [TOFU_EDITOR_PAT]}"
GITHUB_TOKEN="${2:?}"
DOCS_AGENT_PAT="${3:-}"
WEBHOOK_URL="${4:-}"
DEVELOPER_KNOWLEDGE_API_KEY="${5:-}"
UPGRADE_READER_PAT="${6:-}"
UPGRADE_DOCS_PAT="${7:-}"
TOFU_EDITOR_PAT="${8:-}"

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
# 3. Cloud Build SA grants — RETIRED (default-compute-SA retirement, Phase 4)
# --------------------------------------------------------------------------
# Historically this granted the LEGACY Cloud Build service-agent SA
# (${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com) artifactregistry.writer +
# run.admin + iam.serviceAccountUser. That SA was never the identity builds
# actually ran as (Cloud Build defaulted to the compute SA), so the grants were
# dead config. The dedicated cloudbuild-deploy-sa@ (§4c) is now the build
# identity, pinned via `serviceAccount:` in the cloudbuild*.yaml files. The
# legacy block was removed here; any residual live @cloudbuild bindings are
# inert and can be pruned in a later hygiene pass. See
# docs/plans/2026-05-30-default-compute-sa-retirement.md.

# --------------------------------------------------------------------------
# 4. Service Accounts — 1 coordinator + 6 workers (idempotent)
# --------------------------------------------------------------------------
# driftscribe-agent replaces the default compute SA as the coordinator's
# runtime identity. Workers' ALLOWED_CALLERS env lists this SA's email.
# Phase 17.E.2: upgrade-reader-sa and upgrade-docs-sa added for the
# upgrade workload (one SA per worker, distinct from the drift workers).
# Phase D (D3-1): tofu-editor-sa added for the provision-workload HCL-authoring
# worker. Like upgrade-docs-sa it is a worker SA this stack OWNS (created here),
# NOT externally provisioned — distinct from tofu-apply-sa / infra-reader-sa
# which are created by setup_iac_backend.sh / the infra-reader runbook and only
# gated-for on existence below. tofu-editor-sa holds exactly ONE secret (its
# write-scoped GitHub PAT) and NO project-level GCP role: it authors PR text
# only and never runs tofu.
# Note: infra-reader-sa (the Phase infra-iac B explore worker) is NOT created
# here — it is provisioned operator-side per docs/runbooks/infra-reader.md;
# this script only grants the coordinator run.invoker on it once deployed.
for sa in driftscribe-agent reader-agent-sa docs-agent-sa rollback-agent-sa notifier-agent-sa upgrade-reader-sa upgrade-docs-sa tofu-editor-sa; do
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
TOFU_EDITOR_SA="tofu-editor-sa@${PROJECT}.iam.gserviceaccount.com"  # Phase D (D3-1): provision-workload HCL-authoring worker — one PAT, no GCP role

# Cloud Build's per-runtime-SA actAs grants now target the dedicated
# cloudbuild-deploy-sa@ (§4c below), derived from the live deploy configs. The
# legacy loop that granted actAs to ${PROJECT_NUMBER}@cloudbuild was removed
# here as part of the default-compute-SA retirement (Phase 4) — that SA was not
# the identity builds ran as, so the grants were dead config.

# --------------------------------------------------------------------------
# 4c. Dedicated Cloud Build deploy SA (default-compute-SA retirement, Phase 3)
# --------------------------------------------------------------------------
# cloudbuild-deploy-sa@ replaces the default compute SA (PROJECT_NUMBER-compute@)
# as the identity Cloud Build runs as, so the compute SA's roles/editor can
# eventually be stripped. See docs/plans/2026-05-30-default-compute-sa-retirement.md.
# This block is ADDITIVE + idempotent and stays INERT until the cloudbuild*.yaml
# files pin `serviceAccount:` to this SA + add `options.logging` (plan Phase 4);
# the legacy §3/§4 grants above remain load-bearing until that cutover. The
# actAs list is the union of every `--service-account=` across
# infra/cloudbuild*.yaml plus payment-demo-runtime@ (the payment-demo deploy
# preserves that runtime SA). NO secretmanager grant: every --set-secrets is
# deploy-time and runtime-SA-scoped, not read by the build SA.
BUILD_DEPLOY_SA="cloudbuild-deploy-sa@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$BUILD_DEPLOY_SA" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create cloudbuild-deploy-sa \
       --project="$PROJECT" \
       --display-name="Cloud Build deploy SA (default-compute retirement)" \
       --description="Dedicated Cloud Build runtime identity replacing the default compute SA. Inert until cloudbuild*.yaml serviceAccount: pins (Phase 4)."

# Repo-scoped AR push (writer) + pull-at-deploy-admission (reader — the C5g
# image-pull-admission prereq); NOT project-level.
for role in roles/artifactregistry.writer roles/artifactregistry.reader; do
  gcloud artifacts repositories add-iam-policy-binding driftscribe \
    --project="$PROJECT" --location="$REGION" \
    --member="serviceAccount:${BUILD_DEPLOY_SA}" --role="$role" --condition=None >/dev/null
done

# Project-level: run.admin (NOT run.developer — the --allow-unauthenticated
# deploys call run.services.setIamPolicy) + logging.logWriter (mandatory once a
# user-specified build SA runs with options.logging: CLOUD_LOGGING_ONLY).
for role in roles/run.admin roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${BUILD_DEPLOY_SA}" --role="$role" --condition=None >/dev/null
done

# Source fetch: the build (running as this SA) reads the uploaded source tarball
# from the Cloud Build staging bucket; scoped to that one bucket. `gcloud builds
# submit` auto-creates gs://[PROJECT]_cloudbuild on first use, so on a fresh
# project the bucket may not exist yet — guard + skip (re-run after first build).
if gcloud storage buckets describe "gs://${PROJECT}_cloudbuild" >/dev/null 2>&1; then
  gcloud storage buckets add-iam-policy-binding "gs://${PROJECT}_cloudbuild" \
    --member="serviceAccount:${BUILD_DEPLOY_SA}" --role="roles/storage.objectViewer" >/dev/null
else
  echo "  staging bucket gs://${PROJECT}_cloudbuild absent — skipping build-SA objectViewer (re-run after first 'gcloud builds submit', or pre-create it)"
fi

# actAs on each runtime SA the build deploys services as. The coordinator + 6
# workers exist by now (§4). tofu-apply-sa (setup_iac_backend.sh) + infra-reader-sa
# (infra-reader runbook) are external to this script → gate on existence (re-run
# picks them up). payment-demo-runtime's build-SA actAs is granted in §7b instead —
# right after THIS script creates that SA — so a single pass is complete for
# everything this script owns.
for sa in driftscribe-agent reader-agent-sa docs-agent-sa rollback-agent-sa notifier-agent-sa upgrade-reader-sa upgrade-docs-sa tofu-editor-sa; do
  gcloud iam service-accounts add-iam-policy-binding "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --project="$PROJECT" \
    --member="serviceAccount:${BUILD_DEPLOY_SA}" --role="roles/iam.serviceAccountUser" --condition=None >/dev/null
done
for sa in tofu-apply-sa infra-reader-sa; do
  if gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts add-iam-policy-binding "${sa}@${PROJECT}.iam.gserviceaccount.com" \
      --project="$PROJECT" \
      --member="serviceAccount:${BUILD_DEPLOY_SA}" --role="roles/iam.serviceAccountUser" --condition=None >/dev/null
    echo "  cloudbuild-deploy-sa: actAs on ${sa}@"
  fi
done
echo "  cloudbuild-deploy-sa provisioned (inert until cloudbuild serviceAccount: pins — plan Phase 4)"

# --------------------------------------------------------------------------
# 5. Per-SA project-level IAM grants
# --------------------------------------------------------------------------
# Coordinator: Vertex AI (Phase 14.5 — the ADK path calls gemini-2.5-flash via
# Vertex AI's generate-content endpoint; ADC routes through this SA) + Firestore
# datastore.user CONDITIONED to the (default) database (Phase C5f).
# Phase 13: run.viewer removed — classifier path migrated to Reader Worker.
# C5f: the coordinator's Firestore access is now scoped to (default) only
# (events/decisions/sessions/approvals). The condition DENIES it the named
# plan-approvals DB (the C4 tofu-apply worker's sole-writer collection), closing
# the B3 status-flip-and-replay risk that project-wide datastore.user opened.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${COORD_SA}" --role="roles/aiplatform.user" >/dev/null
grant_datastore_user_for_db "$PROJECT" "serviceAccount:${COORD_SA}" "(default)"

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

# Rollback: datastore.user CONDITIONED to (default) (C5f) — the approvals/
# collection lives in (default); the condition denies the named plan-approvals DB.
# The resource-scoped run.developer on payment-demo is below (§7).
grant_datastore_user_for_db "$PROJECT" "serviceAccount:${ROLLBACK_SA}" "(default)"

# C5f cutover: remove the pre-isolation UN-conditioned project-wide datastore.user
# from the coordinator + rollback so the (default)-conditioned grants above are
# their ONLY datastore access (completing plan_approvals isolation). GATED — a
# default re-run only ADDS the conditioned grant (harmless union-of-allows); the
# removal is the deliberate, verified cutover (run with SETUP_PLAN_APPROVALS_DB=1
# AFTER the empirical CEL proof — see docs/runbooks/c5f-hardening.md). The
# conditioned grants above are asserted first, every run (bind-before-remove).
if [[ "${SETUP_PLAN_APPROVALS_DB:-0}" == "1" ]]; then
  remove_unconditioned_datastore_user "$PROJECT" "serviceAccount:${COORD_SA}"
  remove_unconditioned_datastore_user "$PROJECT" "serviceAccount:${ROLLBACK_SA}"
  echo "  C5f: removed UN-conditioned datastore.user from coordinator + rollback (isolation ACTIVE)"
else
  echo "  C5f: (default)-conditioned datastore.user asserted; UN-conditioned removal gated (set SETUP_PLAN_APPROVALS_DB=1)"
fi

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

# plan-hmac-key (Phase C4): the plan-bound approval HMAC key for the tofu-apply
# worker. SEPARATE from approval-hmac-key (the C3 plan-approval HMAC is
# domain-separated) so the apply worker never holds the rollback key — clean
# per-worker key separation. First-run-only auto-generation, like the keys above.
if ! gcloud secrets describe plan-hmac-key --project "$PROJECT" >/dev/null 2>&1; then
  RANDOM_PLAN_HMAC="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  gcloud secrets create plan-hmac-key \
    --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$RANDOM_PLAN_HMAC" | gcloud secrets versions add plan-hmac-key \
    --project "$PROJECT" --data-file=-
  echo "plan-hmac-key created (no operator-facing surface)"
else
  echo "plan-hmac-key already exists — leaving untouched"
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

# tofu-editor's fine-grained PAT (Phase D / D3-1) — operator must supply. DISTINCT
# from docs-agent-github-pat AND upgrade-docs-github-pat: the tofu-editor worker
# authors infra/ (iac/) PRs on adi-prasetyo/driftscribe and holds its OWN write
# PAT (Contents: read+write + Pull requests: read+write), bound to tofu-editor-sa
# ONLY. Same operator-supplied describe-then-create + skip-with-instructions
# pattern as the docs / upgrade PATs above. The worker never runs tofu — it
# emits HCL text; the PAT's only authority is opening PRs on the single repo.
if [ -n "$TOFU_EDITOR_PAT" ]; then
  gcloud secrets describe tofu-editor-github-pat --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create tofu-editor-github-pat \
      --project "$PROJECT" --replication-policy=automatic
  printf '%s' "$TOFU_EDITOR_PAT" | gcloud secrets versions add tofu-editor-github-pat \
    --project "$PROJECT" --data-file=-
else
  if gcloud secrets describe tofu-editor-github-pat --project "$PROJECT" >/dev/null 2>&1; then
    echo "tofu-editor-github-pat already exists — leaving untouched (no arg supplied)"
  else
    echo
    echo "----------------------------------------------------------------"
    echo "TOFU_EDITOR_PAT arg not supplied — tofu-editor-github-pat NOT created."
    echo
    echo "Create a READ+WRITE fine-grained GitHub PAT (separate from"
    echo "docs-agent-github-pat AND upgrade-docs-github-pat — same repo,"
    echo "distinct token for the provision-workload editor):"
    echo "  https://github.com/settings/personal-access-tokens/new"
    echo "  Repository access: select ONE repo (adi-prasetyo/driftscribe)"
    echo "  Permissions:"
    echo "    Contents:      Read and write"
    echo "    Pull requests: Read and write"
    echo "  NO Issues, NO Actions, NO admin, NO second repo."
    echo "Then re-run with the value as the 8th positional arg:"
    echo "  $0 \$PROJECT \$GH_TOKEN \$DOCS_PAT \$WEBHOOK_URL \$DEV_KEY \$UPGRADE_READER_PAT \$UPGRADE_DOCS_PAT <tofu-editor-pat>"
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

# tofu-apply worker (Phase C4): one secret (the plan-bound HMAC key). Scoped to
# plan-hmac-key ONLY — the apply worker cannot read approval-hmac-key or any
# other secret (clean per-worker key separation; see setup_iac_backend.sh §6.5).
APPLY_SA="${APPLY_SA:-tofu-apply-sa@${PROJECT}.iam.gserviceaccount.com}"
bind_secret plan-hmac-key             "$APPLY_SA"

# Notifier worker: one secret (the outbound webhook URL).
bind_secret driftscribe-webhook-url   "$NOTIFIER_SA"

# Upgrade workers (Phase 17.E.2): one secret each, distinct fine-grained
# PATs. The upgrade-reader's PAT is read-only; the upgrade-docs' PAT is
# read+write. Defense in depth: neither SA can read the other's PAT, so
# a compromise of the read-only worker cannot escalate to PR creation.
bind_secret upgrade-reader-github-pat "$UPGRADE_READER_SA"
bind_secret upgrade-docs-github-pat   "$UPGRADE_DOCS_SA"

# tofu-editor worker (Phase D / D3-1): one secret (its write-scoped fine-grained
# PAT). PER-SECRET accessor ONLY — scoped to tofu-editor-github-pat. The editor SA
# holds NO project-level GCP role and NO other secret: it authors infra/ PR text
# and never runs tofu, so its entire authority is this single GitHub PAT.
bind_secret tofu-editor-github-pat    "$TOFU_EDITOR_SA"

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

# 7b. Dedicated payment-demo runtime SA (Phase C5f) + the apply/rollback actAs grants.
# Replace the default compute SA with a MINIMAL dedicated runtime identity. The
# SERVICE-SIDE repoint (template.service_account in iac/cloudrun.tf) is applied
# THROUGH the gated pipeline (the C5g positive in-place UPDATE), so THIS script only
# provisions the SA and the actAs grants every mutator of payment-demo needs on it.
# Cloud Run requires the caller to actAs the service's runtime SA for ANY update
# (incl. a traffic-only update), so BOTH of these need it (the C4 plan named only
# tofu-apply-sa — rollback was a gap):
#   - tofu-apply-sa      : `tofu apply` updates the service (sets the new runtime SA)
#   - rollback-agent-sa  : /execute traffic-shift update_service (else `actAs denied`)
#   - cloudbuild-deploy-sa : `gcloud run deploy payment-demo` (default-compute
#                            retirement Phase 3; BUILD_DEPLOY_SA defined in §4c).
#                            Granted here (not §4c) so a single pass works — this
#                            SA is created in §7b just above this loop.
# Grants target the KNOWN dedicated SA name (not the live-resolved one) so the actAs
# exists BEFORE the apply that first wires service_account=payment-demo-runtime.
# (The pre-existing actAs on the default compute SA is left live for the transition
# window and removed as a documented post-cutover cleanup — see the C5f runbook.)
PD_RUNTIME_SA_NAME="${PD_RUNTIME_SA_NAME:-payment-demo-runtime}"
PD_RUNTIME_SA_DEDICATED="${PD_RUNTIME_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
create_service_account_idempotent "$PROJECT" "$PD_RUNTIME_SA_NAME" \
  "DriftScribe payment-demo runtime (minimal)"
APPLY_SA="${APPLY_SA:-tofu-apply-sa@${PROJECT}.iam.gserviceaccount.com}"
BUILD_DEPLOY_SA="${BUILD_DEPLOY_SA:-cloudbuild-deploy-sa@${PROJECT}.iam.gserviceaccount.com}"
for member in "$APPLY_SA" "$ROLLBACK_SA" "$BUILD_DEPLOY_SA"; do
  if gcloud iam service-accounts describe "$member" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts add-iam-policy-binding "$PD_RUNTIME_SA_DEDICATED" \
      --project="$PROJECT" \
      --member="serviceAccount:${member}" \
      --role="roles/iam.serviceAccountUser" >/dev/null
    echo "  ${member}: actAs on dedicated runtime SA ${PD_RUNTIME_SA_DEDICATED}"
  else
    echo "  ${member} not present yet — skipping actAs on ${PD_RUNTIME_SA_DEDICATED}"
  fi
done
# 7b-AR. Repo-scoped artifactregistry.reader for BOTH the apply mutator and the
# dedicated runtime SA. When the gated pipeline applies an in-place UPDATE to
# payment-demo, Cloud Run validates image-pull at DEPLOY ADMISSION for BOTH the
# deploying identity (tofu-apply-sa, which issues run.services.update) AND the
# new RUNTIME SA (payment-demo-runtime) — both pull payment-demo's image from
# the `driftscribe` repo. Missing reader on either was the C5g 502 root-cause
# ("Permission 'artifactregistry.repositories.downloadArtifacts' denied" at
# admission); it was granted live during C5g and is codified here so a fresh
# bootstrap is reproducible. Repo-scoped (NOT project-level); run.developer does
# NOT include AR reader. (cloudbuild-deploy-sa@ gets its own repo reader in §4c.)
for member in "$APPLY_SA" "$PD_RUNTIME_SA_DEDICATED"; do
  if gcloud iam service-accounts describe "$member" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud artifacts repositories add-iam-policy-binding driftscribe \
      --project="$PROJECT" --location="$REGION" \
      --member="serviceAccount:${member}" \
      --role="roles/artifactregistry.reader" --condition=None >/dev/null
    echo "  ${member}: artifactregistry.reader on repo driftscribe (image-pull admission)"
  else
    echo "  ${member} not present yet — skipping artifactregistry.reader on repo driftscribe"
  fi
done
# tofu-apply-sa also needs resource-scoped run.developer on the service itself.
if gcloud run services describe payment-demo --region="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
   && gcloud iam service-accounts describe "$APPLY_SA" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud run services add-iam-policy-binding payment-demo \
    --project="$PROJECT" --region="$REGION" \
    --member="serviceAccount:${APPLY_SA}" \
    --role="roles/run.developer" >/dev/null
  echo "tofu-apply-sa: granted run.developer on payment-demo (resource-scoped)"
else
  echo "payment-demo or tofu-apply-sa not present yet — skipping run.developer on payment-demo"
  echo "  re-run this script after deploying payment-demo + running setup_iac_backend.sh"
fi

# --------------------------------------------------------------------------
# 8. Coordinator → worker per-service run.invoker grants
# --------------------------------------------------------------------------
# Cloud Run inter-service auth (Phase 11.0 spike) requires the calling SA
# to hold run.invoker on the receiving service. These are PER-SERVICE
# bindings, not project-wide — even a compromised coordinator can only
# call the specific worker services listed in the loop below (currently
# seven, across the drift, upgrade, and explore workloads).
#
# Gated on service existence: on first run (before any `gcloud builds
# submit`) the workers don't exist and this loop is a no-op. After the
# first build, re-running this script applies the grants.
# Phase 17.E.2: upgrade workers (driftscribe-upgrade-reader,
# driftscribe-upgrade-docs) added to the loop. The grants stay per-service
# — the coordinator's run.invoker on a drift worker does NOT extend to
# an upgrade worker (workload-scoped IAM invariant, pinned in
# docs/architecture/iam-matrix.md).
# Phase infra-iac B: driftscribe-infra-reader (the read-only explore-workload
# inventory reader) added to the loop. It is --no-allow-unauthenticated like
# the other workers, so the coordinator's in-app ALLOWED_CALLERS allowlist is
# NOT sufficient on its own — the coordinator SA also needs this Cloud Run
# platform invoker grant, or the call 403s at the admission layer. (This is the
# grant that had to be applied by hand during the first Phase B deploy.)
# Phase infra-iac C4: driftscribe-tofu-apply (the sole-mutator apply worker)
# added so the coordinator (C5) can drive /propose + /apply. NOTE: C4 deploys
# the worker --no-allow-unauthenticated for the live smoke, then redeploys
# --ingress=internal; under internal ingress the coordinator must reach it from
# inside the VPC (a C5 egress concern) — this invoker grant is necessary but not
# sufficient there. See docs/runbooks/tofu-apply.md.
# Phase D (D3-1): driftscribe-tofu-editor (the provision-workload HCL-authoring
# worker) added so the coordinator can invoke /open-pr to open infra/ PRs. Like the
# others it is --no-allow-unauthenticated, so the coordinator SA needs this Cloud
# Run platform invoker grant IN ADDITION to the worker's in-app ALLOWED_CALLERS
# allowlist. Still per-service — invoker on the editor does NOT extend to any other
# workload's workers. See docs/runbooks/tofu-editor.md.
for worker in driftscribe-reader driftscribe-docs driftscribe-rollback driftscribe-notifier driftscribe-upgrade-reader driftscribe-upgrade-docs driftscribe-infra-reader driftscribe-tofu-apply driftscribe-tofu-editor; do
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
# 10. Eventarc drift triggers (payment-demo mutations → coordinator /eventarc)
# --------------------------------------------------------------------------
# Phase-gated: `SETUP_EVENTARC=0` skips this whole block. A Phase C4 re-run
# (which only needs the §8 coordinator invoker grant) must NOT touch drift
# triggers — run it as `SETUP_EVENTARC=0 infra/scripts/setup_secrets.sh ...`.
# Default is ON so a fresh bootstrap still wires drift detection.
#
# TWO triggers are created, both → driftscribe-agent /eventarc, because Cloud
# Run emits DIFFERENT audit-log methodNames depending on who mutates the service:
#   - gcloud / CI deploys / older clients → google.cloud.run.v1.Services.ReplaceService
#       resourceName: namespaces/<proj>/services/<svc>
#   - the rollback worker (run_v2 client) / console / newer clients
#                                         → google.cloud.run.v2.Services.UpdateService
#       resourceName: projects/<proj>/locations/<region>/services/<svc>
# An Eventarc audit-log trigger filters EXACTLY ONE methodName, so it takes one
# trigger per variant. The /eventarc handler is methodName-agnostic (it whitelists
# on resource.labels.service_name/location), so both feed the same recheck. A
# single mutation emits exactly one methodName, so the two triggers do not
# double-fire on one event. The canonical demo drift-injection
# (`gcloud run services update payment-demo`) emits the v1 ReplaceService variant
# — the v2-only filter the original design shipped produced an ACTIVE-but-DEAD
# trigger that silently delivered nothing. See docs/runbooks/deploy.md step 7.
if [[ "${SETUP_EVENTARC:-1}" == "1" ]]; then
  EVENTARC_SA="eventarc-trigger-sa@${PROJECT}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$EVENTARC_SA" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud iam service-accounts create eventarc-trigger-sa \
      --project "$PROJECT" --display-name="DriftScribe Eventarc trigger SA"

  # The Eventarc SERVICE AGENT (Google-managed) provisions the trigger's Pub/Sub
  # plumbing. On projects where it was never provisioned, `triggers create` fails
  # FAILED_PRECONDITION "Permission denied while using the Eventarc Service Agent".
  # Force-create the identity + grant its role explicitly (the auto-grant on API
  # enablement is not reliably present); the create retry loop below then absorbs
  # the (documented, multi-minute) permission-propagation delay.
  gcloud beta services identity create --service=eventarc.googleapis.com \
    --project="$PROJECT" >/dev/null 2>&1 || true
  EVENTARC_AGENT="service-$(gcloud projects describe "$PROJECT" \
    --format='value(projectNumber)')@gcp-sa-eventarc.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${EVENTARC_AGENT}" \
    --role="roles/eventarc.serviceAgent" --condition=None >/dev/null

  # The trigger SA receives events (eventReceiver) and invokes the coordinator.
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${EVENTARC_SA}" \
    --role="roles/eventarc.eventReceiver" --condition=None >/dev/null

  # create-with-retry: retry ONLY the Eventarc-service-agent propagation
  # FAILED_PRECONDITION (bounded). Any other error (bad filter, deployer
  # permission, malformed resourceName) fails fast — we never paper over real
  # misconfiguration with blind retries.
  _eventarc_create() {
    local name="$1" method="$2" resource="$3" out rc attempt
    for attempt in 1 2 3 4 5 6; do
      # `if out=$(...)` — NOT `out=$(...); rc=$?`. Under `set -e` a bare
      # assignment whose command-substitution fails exits the whole script
      # before rc can be read, making this entire retry loop dead code for
      # the failure path it exists to handle.
      if out="$(gcloud eventarc triggers create "$name" \
        --project="$PROJECT" --location="$REGION" \
        --destination-run-service=driftscribe-agent \
        --destination-run-region="$REGION" \
        --destination-run-path=/eventarc \
        --event-filters="type=google.cloud.audit.log.v1.written" \
        --event-filters="serviceName=run.googleapis.com" \
        --event-filters="methodName=${method}" \
        --event-filters="resourceName=${resource}" \
        --service-account="${EVENTARC_SA}" 2>&1)"; then
        rc=0
      else
        rc=$?
      fi
      if [ "$rc" -eq 0 ] || echo "$out" | grep -qE "already exists|ALREADY_EXISTS"; then
        echo "  eventarc trigger ${name}: ready"; return 0
      fi
      if echo "$out" | grep -qF "Permission denied while using the Eventarc Service Agent"; then
        echo "  eventarc service agent perms propagating (attempt ${attempt}/6) — waiting 60s..." >&2
        sleep 60; continue
      fi
      echo "$out" >&2
      echo "  ERROR: eventarc trigger ${name} create failed (non-retryable)" >&2
      return "$rc"
    done
    echo "  ERROR: eventarc trigger ${name} still failing after retries" >&2; return 1
  }

  # ensure: create if absent; if present, verify methodName/resourceName/path/SA
  # and recreate on drift. This REPAIRS a project carrying the old
  # ACTIVE-but-dead v2-only `driftscribe-cloudrun-changes` instead of skipping it
  # forever on a describe-and-skip.
  _eventarc_ensure() {
    local name="$1" method="$2" resource="$3" filters meta dsvc dregion dpath dsa
    if gcloud eventarc triggers describe "$name" --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
      filters="$(gcloud eventarc triggers describe "$name" --location="$REGION" --project="$PROJECT" --format='value(eventFilters)' 2>/dev/null || true)"
      # Scalar destination/SA fields are tab-separated and contain no
      # tabs/spaces, so a single describe + IFS=tab read extracts them for
      # EXACT comparison (a substring check on the path would wrongly accept
      # e.g. /eventarc-upgrade, and would not verify service/region at all).
      meta="$(gcloud eventarc triggers describe "$name" --location="$REGION" --project="$PROJECT" --format='value(destination.cloudRun.service,destination.cloudRun.region,destination.cloudRun.path,serviceAccount)' 2>/dev/null || true)"
      IFS=$'\t' read -r dsvc dregion dpath dsa <<<"$meta"
      if [ "$dsvc" = "driftscribe-agent" ] && [ "$dregion" = "$REGION" ] \
         && [ "$dpath" = "/eventarc" ] && [ "$dsa" = "$EVENTARC_SA" ] \
         && echo "$filters" | grep -qF "$method" && echo "$filters" | grep -qF "$resource"; then
        echo "  eventarc trigger ${name}: already correct — skipping"
        return 0
      fi
      echo "  eventarc trigger ${name}: config drifted — recreating"
      gcloud eventarc triggers delete "$name" --location="$REGION" --project="$PROJECT" --quiet >/dev/null 2>&1 || true
    fi
    _eventarc_create "$name" "$method" "$resource"
  }

  # run.invoker grant + trigger create both depend on the coordinator existing.
  # On the first-ever run (before Cloud Build) this is a no-op; re-run after the
  # build to apply.
  if gcloud run services describe driftscribe-agent \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud run services add-iam-policy-binding driftscribe-agent \
      --project="$PROJECT" --region="$REGION" \
      --member="serviceAccount:${EVENTARC_SA}" --role="roles/run.invoker" >/dev/null
    echo "driftscribe-agent: granted run.invoker on eventarc-trigger-sa"

    _eventarc_ensure driftscribe-cloudrun-changes \
      "google.cloud.run.v1.Services.ReplaceService" \
      "namespaces/${PROJECT}/services/payment-demo"
    _eventarc_ensure driftscribe-cloudrun-changes-v2-update \
      "google.cloud.run.v2.Services.UpdateService" \
      "projects/${PROJECT}/locations/${REGION}/services/payment-demo"

    echo
    echo "  verify a mutation fires the handler — see docs/runbooks/deploy.md →"
    echo "    'confirm Eventarc trigger fires' (mutate payment-demo; expect /eventarc 200 ~30s later)"
  else
    echo "driftscribe-agent not deployed yet — skipping run.invoker grant + trigger create"
  fi
else
  echo "SETUP_EVENTARC != 1 — skipping eventarc drift-trigger setup"
fi

# --------------------------------------------------------------------------
# 10b. OPTIONAL infra-graph pre-warm (Cloud Scheduler) — gated, OFF by default.
# --------------------------------------------------------------------------
# Tier 3 of the /infra/graph cache (docs/plans/2026-06-18-infra-graph-l2-
# firestore-cache.md). The coordinator's L2 Firestore cache ALREADY survives a
# scale-to-zero cold start within INFRA_GRAPH_L2_CACHE_TTL_S on its own. This
# OPTIONAL block adds a Cloud Scheduler job that POSTs
# /internal/infra-graph/refresh every ~10 min so the L2 doc is ALWAYS fresh —
# making even a true cold open (fresh instance, empty L1, no prior render)
# instant instead of paying the ~25-35s live CAI enumeration.
#
# Costs/consequences (Codex review #7): a 10-min job sends enough traffic to keep
# the coordinator — and likely infra_reader — from scaling to zero, an effective
# warm-floor with a small always-on cost. Skip this block (the default) if that
# tradeoff isn't wanted; the L2 cache still helps cold starts without it.
#
# OFF by default: set SETUP_INFRA_PREWARM=1 to provision (mirrors SETUP_EVENTARC).
if [[ "${SETUP_INFRA_PREWARM:-0}" == "1" ]]; then
  # Cloud Scheduler API is enabled only inside this gate (not in the always-on
  # API list) so the default surface stays minimal — pre-warm is opt-in.
  gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT"

  PREWARM_SA="infra-prewarm-sa@${PROJECT}.iam.gserviceaccount.com"
  create_service_account_idempotent "$PROJECT" infra-prewarm-sa \
    "DriftScribe infra-graph pre-warm scheduler SA"

  # The SA grant + scheduler job both need the coordinator to exist (its URL is
  # the scheduler target + OIDC audience). On a first-ever run this is a no-op;
  # re-run after the coordinator is deployed.
  if gcloud run services describe driftscribe-agent \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
    COORD_URL="$(gcloud run services describe driftscribe-agent \
      --region="$REGION" --project="$PROJECT" --format='value(status.url)')"
    REFRESH_URL="${COORD_URL}/internal/infra-graph/refresh"

    # The scheduler SA invokes the coordinator (run.invoker), exactly like the
    # eventarc trigger SA. The endpoint ALSO verifies the OIDC email claim is
    # this SA — defense-in-depth on top of the IAM binding.
    gcloud run services add-iam-policy-binding driftscribe-agent \
      --project="$PROJECT" --region="$REGION" \
      --member="serviceAccount:${PREWARM_SA}" --role="roles/run.invoker" >/dev/null
    echo "driftscribe-agent: granted run.invoker on infra-prewarm-sa"

    # Wire the endpoint's expected OIDC audience to the FULL refresh URL — the
    # exact value the Scheduler stamps as --oidc-token-audience (the /eventarc
    # path-suffix lesson: verify_oauth2_token does an EXACT aud match). Set via
    # --update-env-vars (NOT the deploy baseline) so a full coordinator deploy
    # can't clobber it with an empty value (Codex review #2).
    #
    # Describe-then-act: the coordinator's traffic is PINNED to a specific
    # revision, so EVERY `gcloud run services update` lands a NEW revision at 0%
    # traffic. Skip the update (and its spurious dead revision) when the value is
    # already correct on the live revision.
    current_aud="$(gcloud run services describe driftscribe-agent \
      --region="$REGION" --project="$PROJECT" \
      --flatten="spec.template.spec.containers[0].env" \
      --filter="spec.template.spec.containers[0].env.name=INFRA_PREWARM_AUDIENCE" \
      --format="value(spec.template.spec.containers[0].env.value)" 2>/dev/null || true)"
    if [ "$current_aud" != "$REFRESH_URL" ]; then
      gcloud run services update driftscribe-agent \
        --project="$PROJECT" --region="$REGION" \
        --update-env-vars="INFRA_PREWARM_AUDIENCE=${REFRESH_URL}" >/dev/null
      # Traffic is pinned, so the update created the new revision at 0% — promote
      # it, else the scheduler keeps POSTing the OLD (serving) revision, which
      # still has INFRA_PREWARM_AUDIENCE unset and 503s every tick. The traffic
      # shift is MANDATORY (see MEMORY: coordinator-deploy traffic pinning).
      NEW_REV="$(gcloud run services describe driftscribe-agent \
        --region="$REGION" --project="$PROJECT" \
        --format='value(status.latestCreatedRevisionName)')"
      gcloud run services update-traffic driftscribe-agent \
        --project="$PROJECT" --region="$REGION" \
        --to-revisions="${NEW_REV}=100" >/dev/null
      echo "driftscribe-agent: INFRA_PREWARM_AUDIENCE set; traffic shifted to ${NEW_REV}"
    else
      echo "driftscribe-agent: INFRA_PREWARM_AUDIENCE already ${REFRESH_URL} — skipping update"
    fi

    # Create-or-update the job. --max-retry-attempts=1: auth/config bugs surface
    # as 401/403/503 we DON'T want retried into a storm (a transient CAI/worker
    # blip is a soft 200 anyway). --attempt-deadline=120s covers the ~30s fetch.
    if gcloud scheduler jobs describe infra-graph-prewarm \
       --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
      SCHED_VERB=update
    else
      SCHED_VERB=create
    fi
    gcloud scheduler jobs "$SCHED_VERB" http infra-graph-prewarm \
      --location="$REGION" --project="$PROJECT" \
      --schedule="*/10 * * * *" \
      --uri="$REFRESH_URL" \
      --http-method=POST \
      --oidc-service-account-email="$PREWARM_SA" \
      --oidc-token-audience="$REFRESH_URL" \
      --max-retry-attempts=1 \
      --attempt-deadline=120s >/dev/null
    echo "  infra-graph-prewarm scheduler job: ${SCHED_VERB}d (every 10 min)"
  else
    echo "driftscribe-agent not deployed yet — skipping pre-warm SA grant + scheduler job"
  fi
else
  echo "SETUP_INFRA_PREWARM != 1 — skipping infra-graph pre-warm (Cloud Scheduler) setup"
fi

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
