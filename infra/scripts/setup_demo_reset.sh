#!/usr/bin/env bash
# OPERATOR-RUN: creates live GCP IAM/WIF in driftscribe-hack-2026. Review before running. Do NOT run in CI.
#
# Bootstraps the identity the scheduled `.github/workflows/demo-reset.yml`
# workflow uses to self-heal the public judging-window demo: a narrow SA
# (`demo-reset-sa`) plus a NEW WIF provider on the EXISTING `github-actions`
# pool, scoped to demo-reset.yml only.
#
# Why a NEW SA + provider, not reuse of an existing one (see
# docs/plans/2026-07-07-demo-daily-reset-and-notice.md "Auth" section):
#   - The E2E runner SA (e2e-runner-sa) lives in a SEPARATE project
#     (driftscribe-e2e / driftscribe-e2e-target's home project) and its WIF
#     secrets point there — it cannot reach driftscribe-hack-2026 at all.
#   - The existing `github-oidc` provider (infra/scripts/setup_iac_backend.sh
#     §6) is intentionally scoped to ONE workflow file (`iac.yml`) via its
#     attribute condition's `workflow_ref.startsWith(...)` clause — see that
#     script's header for the full fork-PR security rationale this mirrors.
#     Reusing it for demo-reset.yml would either widen `iac.yml`'s trusted
#     surface (bad: that provider mints creds for the tofu-plan-builder SA,
#     which must stay narrowly scoped) or require demo-reset.yml to pretend
#     to be iac.yml (worse). A second provider on the SAME pool, scoped to
#     THIS workflow file, keeps the two blast radii separate while reusing
#     the one-time OIDC trust relationship with GitHub (the pool itself).
#
# What this script creates/binds (all idempotent — safe to re-run):
#   1. Service account  demo-reset-sa@<PROJECT>.iam.gserviceaccount.com
#   2. roles/run.viewer                    (project-wide)
#   3. roles/run.developer                 (payment-demo service ONLY)
#   4. roles/iam.serviceAccountUser         (payment-demo's RUNTIME SA)
#   5. roles/artifactregistry.reader       (the `driftscribe` AR repo — see
#      step 5's comment; NOT in the original task list, added because the
#      exact operation this SA performs — an env-var-only `services update`
#      on payment-demo — is documented to need it, see setup_secrets.sh §7b-AR
#      and docs/runbooks/e2e-environment.md §5)
#   6. roles/pubsub.viewer                 (adopt-probe-topic + adopt-probe-sub
#      ONLY, resource-scoped; adopt-fixture describes)
#   7. roles/secretmanager.secretAccessor  (PER SECRET: coordinator-shared-token,
#      upgrade-docs-github-pat — no project-wide accessor grant anywhere)
#   8. WIF provider github-oidc-demo-reset on the EXISTING github-actions pool
#   9. roles/iam.workloadIdentityUser on demo-reset-sa, for a principalSet
#      pinned to attribute.repository (mirrors setup_iac_backend.sh §6a)
#
# Usage:
#   infra/scripts/setup_demo_reset.sh
#   PROJECT=driftscribe-hack-2026 REGION=asia-northeast1 \
#     infra/scripts/setup_demo_reset.sh
#
# All knobs default to the real prod values; override via env var to dry-run
# against a throwaway project. NEVER run this against a project you don't own.
# After this script exits 0, the operator MUST (printed again at the end):
#   1. Set repo secrets GCP_WIF_PROVIDER_DEMO_RESET + GCP_DEMO_RESET_SA to the
#      two values printed below.
#   2. Enable the repo setting `allow_auto_merge` (currently false) — the
#      lodash-repin job's `gh pr merge --auto` depends on it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

# --------------------------------------------------------------------------
# Parameters — defaults are the real prod values; override via env to test
# against a throwaway project (matches setup_iac_backend.sh's convention).
# --------------------------------------------------------------------------
PROJECT="${PROJECT:-driftscribe-hack-2026}"
REGION="${REGION:-asia-northeast1}"

SA_NAME="${SA_NAME:-demo-reset-sa}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

TARGET_SERVICE="${TARGET_SERVICE:-payment-demo}"
AR_REPO="${AR_REPO:-driftscribe}"

# Workload Identity Federation — a SECOND provider on the EXISTING pool. The
# pool itself (`github-actions`) is created by setup_iac_backend.sh and is
# NOT created here — this script fails loudly if it is missing (see the
# pre-flight check below) rather than silently creating a duplicate.
WIF_POOL="${WIF_POOL:-github-actions}"
WIF_PROVIDER="${WIF_PROVIDER:-github-oidc-demo-reset}"
GITHUB_REPO="${GITHUB_REPO:-adi-prasetyo/driftscribe}"
GITHUB_WORKFLOW="${GITHUB_WORKFLOW:-.github/workflows/demo-reset.yml}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
GITHUB_PUSH_REF="refs/heads/${GITHUB_BRANCH}"

# --------------------------------------------------------------------------
# 0. Pre-flight: confirm the project exists, the caller can act on it, and
#    the EXISTING WIF pool is really there (mirrors setup_iac_backend.sh §0).
# --------------------------------------------------------------------------
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
  echo "ERROR: project ${PROJECT} does not exist or the active gcloud" >&2
  echo "       account lacks describe permission on it. Authenticate with an" >&2
  echo "       owner of ${PROJECT} (gcloud auth login) and re-run." >&2
  exit 1
fi

CALLER_EMAIL="$(gcloud config get-value account 2>/dev/null)"
if [ -z "$CALLER_EMAIL" ]; then
  echo "ERROR: gcloud has no active account configured. Run 'gcloud auth login'." >&2
  exit 1
fi
echo "Provisioning demo-reset IAM/WIF for ${PROJECT} (region ${REGION}) as ${CALLER_EMAIL}..."

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

if ! gcloud iam workload-identity-pools describe "$WIF_POOL" \
     --project="$PROJECT" --location=global >/dev/null 2>&1; then
  echo "ERROR: WIF pool '${WIF_POOL}' does not exist in ${PROJECT}." >&2
  echo "       This script deliberately does NOT create the pool — it is" >&2
  echo "       bootstrapped once by infra/scripts/setup_iac_backend.sh. Run" >&2
  echo "       that script first (or set WIF_POOL to an existing pool)." >&2
  exit 1
fi

# --------------------------------------------------------------------------
# 1. Service account.
# --------------------------------------------------------------------------
create_service_account_idempotent "$PROJECT" "$SA_NAME" "Demo reset workflow"
echo "  ${SA}: service account ready"

# --------------------------------------------------------------------------
# 2. roles/run.viewer — project-wide. Needed to describe payment-demo AND to
#    resolve the coordinator (driftscribe-agent) URL for the autonomy/pause
#    safety-net reads.
# --------------------------------------------------------------------------
grant_role_idempotent "$PROJECT" "serviceAccount:${SA}" "roles/run.viewer"
echo "  ${SA}: run.viewer (project) — describe payment-demo + resolve coordinator URL"

# --------------------------------------------------------------------------
# 3. roles/run.developer — resource-scoped to payment-demo ONLY (never
#    project-wide). Mirrors the rollback worker's identical grant
#    (setup_secrets.sh §7 / setup_e2e_project.sh's post-deploy next-steps):
#    describe-gated so a fresh bootstrap where payment-demo doesn't exist yet
#    doesn't hard-fail — re-run this script after payment-demo is deployed.
# --------------------------------------------------------------------------
if gcloud run services describe "$TARGET_SERVICE" \
     --project="$PROJECT" --region="$REGION" >/dev/null 2>&1; then
  gcloud run services add-iam-policy-binding "$TARGET_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="serviceAccount:${SA}" \
    --role="roles/run.developer" >/dev/null
  echo "  ${SA}: run.developer on ${TARGET_SERVICE} (resource-scoped)"
else
  echo "  ${TARGET_SERVICE} not deployed yet — skipping resource-scoped run.developer"
  echo "    re-run this script after ${TARGET_SERVICE} exists"
fi

# --------------------------------------------------------------------------
# 4. roles/iam.serviceAccountUser — on payment-demo's RUNTIME service
#    account (NOT demo-reset-sa itself). Cloud Run requires the caller to
#    actAs the service's runtime identity for ANY `services update` call,
#    including an env-var-only one (mirrors setup_e2e_project.sh's post-
#    deploy RUNTIME_SA resolution: resolve live, fall back to the project's
#    default compute SA if payment-demo isn't pinned to a dedicated one).
# --------------------------------------------------------------------------
if gcloud run services describe "$TARGET_SERVICE" \
     --project="$PROJECT" --region="$REGION" >/dev/null 2>&1; then
  RUNTIME_SA="$(gcloud run services describe "$TARGET_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(template.serviceAccount)' 2>/dev/null)"
  : "${RUNTIME_SA:=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
  gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
    --project="$PROJECT" \
    --member="serviceAccount:${SA}" \
    --role="roles/iam.serviceAccountUser" --condition=None >/dev/null
  echo "  ${SA}: iam.serviceAccountUser on ${RUNTIME_SA} (actAs — required for services update)"
else
  echo "  ${TARGET_SERVICE} not deployed yet — skipping actAs on its runtime SA"
fi

# --------------------------------------------------------------------------
# 5. roles/artifactregistry.reader — repo-scoped on the `driftscribe` AR
#    repo. NOT in the original task spec, added after cross-checking why:
#    Cloud Run's admin API validates that the CALLER can pull the image
#    referenced by a service on ANY `services.update` call that creates a
#    new revision — which an env-var change does, even though the image
#    itself doesn't change. Without this, the very first live env-drift fix
#    would 403 with "Permission 'artifactregistry.repositories.
#    downloadArtifacts' denied" (same root cause documented for e2e-runner-sa
#    in docs/runbooks/e2e-environment.md §5, and for tofu-apply-sa /
#    payment-demo-runtime in setup_secrets.sh §7b-AR after the C5g incident).
#    Repo-scoped (not project-wide) to match prod's convention in
#    setup_secrets.sh §7b-AR, not the E2E project's project-wide shortcut.
# --------------------------------------------------------------------------
if gcloud artifacts repositories describe "$AR_REPO" \
     --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
    --project="$PROJECT" --location="$REGION" \
    --member="serviceAccount:${SA}" \
    --role="roles/artifactregistry.reader" --condition=None >/dev/null
  echo "  ${SA}: artifactregistry.reader on repo ${AR_REPO} (image-pull admission for services update)"
else
  echo "  AR repo ${AR_REPO} not found in ${PROJECT}/${REGION} — skipping artifactregistry.reader"
  echo "    (foundational infra normally created by setup_secrets.sh/setup_prod_project.sh;"
  echo "    re-run this script after it exists, or the first live env-drift fix will 403)"
fi

# --------------------------------------------------------------------------
# 6. roles/pubsub.viewer — RESOURCE-SCOPED to the two adopt fixtures the
#    workflow describes (never project-wide: the workflow only ever calls
#    `topics/subscriptions describe` on these two exact names, and Pub/Sub
#    supports per-topic/per-subscription IAM, so a project grant would be
#    gratuitous breadth — matching the resource-scoping discipline of the
#    run.developer/actAs grants above). Describe-gated like those grants so
#    a throwaway project without the fixtures doesn't hard-fail; re-run
#    after the fixtures exist.
# --------------------------------------------------------------------------
ADOPT_TOPIC="${ADOPT_TOPIC:-adopt-probe-topic}"
ADOPT_SUB="${ADOPT_SUB:-adopt-probe-sub}"
if gcloud pubsub topics describe "$ADOPT_TOPIC" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud pubsub topics add-iam-policy-binding "$ADOPT_TOPIC" \
    --project="$PROJECT" \
    --member="serviceAccount:${SA}" \
    --role="roles/pubsub.viewer" >/dev/null
  echo "  ${SA}: pubsub.viewer on topic ${ADOPT_TOPIC} (resource-scoped)"
else
  echo "  topic ${ADOPT_TOPIC} not found — skipping pubsub.viewer (re-run after the adopt fixture exists)"
fi
if gcloud pubsub subscriptions describe "$ADOPT_SUB" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud pubsub subscriptions add-iam-policy-binding "$ADOPT_SUB" \
    --project="$PROJECT" \
    --member="serviceAccount:${SA}" \
    --role="roles/pubsub.viewer" >/dev/null
  echo "  ${SA}: pubsub.viewer on subscription ${ADOPT_SUB} (resource-scoped)"
else
  echo "  subscription ${ADOPT_SUB} not found — skipping pubsub.viewer (re-run after the adopt fixture exists)"
fi

# --------------------------------------------------------------------------
# 7. Per-secret secretmanager.secretAccessor — defense in depth, no
#    project-wide accessor grant anywhere (matches every other setup script
#    in this repo). Both secrets already exist in ${PROJECT}; bind_secret_accessor
#    skips with a log line (not an error) if either is somehow absent.
# --------------------------------------------------------------------------
bind_secret_accessor "$PROJECT" coordinator-shared-token "serviceAccount:${SA}"
bind_secret_accessor "$PROJECT" upgrade-docs-github-pat  "serviceAccount:${SA}"
echo "  ${SA}: secretAccessor on coordinator-shared-token + upgrade-docs-github-pat"

# --------------------------------------------------------------------------
# 8. WIF provider — github-oidc-demo-reset, on the EXISTING github-actions
#    pool. SAME attribute mapping as github-oidc (setup_iac_backend.sh §6).
# --------------------------------------------------------------------------
WIF_ATTR_MAPPING="google.subject=assertion.sub"
WIF_ATTR_MAPPING+=",attribute.repository=assertion.repository"
WIF_ATTR_MAPPING+=",attribute.ref=assertion.ref"
WIF_ATTR_MAPPING+=",attribute.event_name=assertion.event_name"
WIF_ATTR_MAPPING+=",attribute.workflow_ref=assertion.workflow_ref"

# Attribute CONDITION — SAME SHAPE as github-oidc (repository pin +
# workflow_ref prefix pin + ref pin + an event_name allowlist), but the
# event_name allowlist is DELIBERATELY DIFFERENT from github-oidc's
# ('push' || 'workflow_dispatch'): demo-reset.yml has NO `push:` trigger at
# all — it runs on `schedule` and `workflow_dispatch` only. Copying
# github-oidc's condition verbatim (as a literal "same shape" reading might
# suggest) would admit workflow_dispatch but reject EVERY scheduled cron run
# (their event_name claim is 'schedule', not 'push'), which would silently
# break the entire self-heal mechanism — the one thing this workflow exists
# to do. GitHub always runs a `schedule`-triggered workflow from the version
# on the default branch, so `ref` is genuinely `refs/heads/main` for those
# runs too, and the ref==main pin below is intentionally kept (mirrors
# github-oidc restricting workflow_dispatch to the trusted branch only — see
# setup_iac_backend.sh §6's fork-PR rationale for why `pull_request` is
# excluded entirely from every provider on this pool).
WIF_ATTR_CONDITION="assertion.repository == '${GITHUB_REPO}'"
WIF_ATTR_CONDITION+=" && assertion.workflow_ref.startsWith('${GITHUB_REPO}/${GITHUB_WORKFLOW}@')"
WIF_ATTR_CONDITION+=" && assertion.ref == '${GITHUB_PUSH_REF}'"
WIF_ATTR_CONDITION+=" && (assertion.event_name == 'schedule' || assertion.event_name == 'workflow_dispatch')"

if gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
     --project="$PROJECT" --location=global \
     --workload-identity-pool="$WIF_POOL" >/dev/null 2>&1; then
  echo "  WIF provider ${WIF_PROVIDER} already exists — updating attribute mapping + condition"
  gcloud iam workload-identity-pools providers update-oidc "$WIF_PROVIDER" \
    --project="$PROJECT" --location=global \
    --workload-identity-pool="$WIF_POOL" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$WIF_ATTR_MAPPING" \
    --attribute-condition="$WIF_ATTR_CONDITION" >/dev/null
  echo "  WIF provider ${WIF_PROVIDER}: attribute mapping + condition updated"
else
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
    --project="$PROJECT" --location=global \
    --workload-identity-pool="$WIF_POOL" \
    --display-name="GitHub OIDC (demo-reset)" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$WIF_ATTR_MAPPING" \
    --attribute-condition="$WIF_ATTR_CONDITION"
  echo "  WIF provider ${WIF_PROVIDER}: created"
fi

# --------------------------------------------------------------------------
# 9. Bind demo-reset-sa so the federated GitHub identity may impersonate it —
#    a principalSet pinned to attribute.repository (mirrors
#    setup_iac_backend.sh §6a; see that script for why this alone does NOT
#    exclude fork PRs — the real exclusion is the provider condition above,
#    which admits only schedule/workflow_dispatch, never pull_request).
# --------------------------------------------------------------------------
WIF_POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}"
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project="$PROJECT" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${WIF_POOL_NAME}/attribute.repository/${GITHUB_REPO}" >/dev/null
echo "  ${SA}: workloadIdentityUser for principalSet repository=${GITHUB_REPO}"

WIF_PROVIDER_NAME="${WIF_POOL_NAME}/providers/${WIF_PROVIDER}"

# --------------------------------------------------------------------------
# 10. Summary — values the operator wires into the repo.
# --------------------------------------------------------------------------
cat <<EOF

================================================================
setup_demo_reset.sh: complete

Set these as repo secrets (Settings -> Secrets and variables -> Actions):

  GCP_WIF_PROVIDER_DEMO_RESET =
    ${WIF_PROVIDER_NAME}

  GCP_DEMO_RESET_SA =
    ${SA}

  gh secret set GCP_WIF_PROVIDER_DEMO_RESET --repo ${GITHUB_REPO} --body "${WIF_PROVIDER_NAME}"
  gh secret set GCP_DEMO_RESET_SA           --repo ${GITHUB_REPO} --body "${SA}"

REMINDER — the lodash-repin job's \`gh pr merge --auto\` will fail until this
repo setting is flipped ON (currently false per the plan doc's live-state
snapshot):

  gh api -X PATCH repos/${GITHUB_REPO} -f allow_auto_merge=true

The provider only accepts tokens from repo ${GITHUB_REPO}, workflow
${GITHUB_WORKFLOW}, on ref=${GITHUB_PUSH_REF}, with event_name in
{schedule, workflow_dispatch} — pull_request (fork or same-repo) gets NO
credentials, matching the github-oidc provider's stance.

Next: trigger a workflow_dispatch run of demo-reset.yml with
force-lodash=true to verify the auth path end-to-end and fix any
already-rotted lodash state in one shot.
================================================================
EOF
