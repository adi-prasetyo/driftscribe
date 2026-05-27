#!/usr/bin/env bash
# OPERATOR-RUN: creates live GCP infra/IAM in driftscribe-hack-2026. Review before running. Do NOT run in CI.
#
# Bootstraps the out-of-band prerequisites for DriftScribe's OpenTofu layer
# (iac/). OpenTofu's gcs backend never creates its own state bucket and the
# gcp_kms key provider never creates its own key — both MUST pre-exist before
# the first `tofu init`. This script provisions them (idempotently), plus the
# Workload Identity Federation (WIF) plumbing the Phase C plan-builder will use.
#
# What it creates (all idempotent — safe to re-run):
#   1. State bucket    gs://driftscribe-hack-2026-tofu-state
#        - Object Versioning ON  (recover a clobbered/corrupt state generation)
#        - Uniform bucket-level access (no per-object ACLs; IAM-only)
#   2. Artifact bucket gs://driftscribe-hack-2026-tofu-artifacts
#        - versioned; reserved for Phase C plan artifacts. Created now so the
#          self-protection denylist (design doc §5) has a real target to name.
#   3. Cloud KMS keyring + key for OpenTofu state/plan encryption. Prints the
#      full key resource path for `var.tofu_state_kms_key` in iac/.
#   4. Workload Identity Federation pool + GitHub OIDC provider with attribute
#      conditions pinning repository + workflow + ref + event_name, and a CI
#      service account bound with LEAST privilege (see §4 below).
#
# Phasing note (design doc §6, §11.8 decision 8): the buckets + KMS key ARE a
# Phase A prerequisite — the operator's `tofu init`/`import` against the gcs
# backend with encryption enforced cannot run without them. The WIF half is
# scripted here for completeness but is a **Phase C activation** step, NOT a
# Phase A done-condition: Phase A CI runs an unauthenticated
# `init -backend=false` + fmt + validate only (no plan, no GCP creds). Wiring
# the WIF provider + CI SA into a workflow happens in Phase C.
#
# Why scoped-WRITE (not read-only) for the CI plan-builder (design doc §3.2):
# the gcs backend acquires a state LOCK by writing a lock object, so even
# `tofu plan` needs object write on the state bucket. "Plan is read-only" is
# false with this backend. The CI SA therefore gets roles/storage.objectAdmin
# on the STATE bucket only — never project-wide, never the artifact bucket's
# admin (Phase C grants artifact write separately when the apply pipeline lands).
#
# Authenticated planning runs only on a TRUSTED TRIGGER: a push to the trusted
# branch, or a maintainer-initiated workflow_dispatch. Fork PRs — and the
# `pull_request` event in general — are deliberately NOT granted credentials.
# This is because `repository ==` cannot filter fork PRs: GitHub runs the
# `pull_request` event in the BASE repo, so the `repository` OIDC claim is the
# base repo (the canonical repo) even for a PR opened from a fork. The provider's
# attribute-condition therefore pins repository + workflow_ref AND restricts the
# event to push-to-trusted-branch or workflow_dispatch; the CI SA's WIF binding
# is further restricted by a principalSet on the repository attribute.
#
# Usage:
#   infra/scripts/setup_iac_backend.sh
#   PROJECT=driftscribe-hack-2026 REGION=asia-northeast1 \
#     infra/scripts/setup_iac_backend.sh
#
# All knobs default to the real prod values; override via env var to dry-run
# against a throwaway project. NEVER run this against a project you don't own.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=infra/scripts/_setup_lib.sh
source "${SCRIPT_DIR}/_setup_lib.sh"

# --------------------------------------------------------------------------
# Parameters — defaults are the real prod values; override via env to test
# against a throwaway project. No positional args (matches setup_e2e_project.sh
# which reads PROJECT_E2E from the env).
# --------------------------------------------------------------------------
PROJECT="${PROJECT:-driftscribe-hack-2026}"
REGION="${REGION:-asia-northeast1}"

# State + artifact buckets. KMS keyring/key + WIF pool/provider names. The
# GitHub repo + workflow file the OIDC provider trusts. All overridable so the
# script is not littered with literals.
STATE_BUCKET="${STATE_BUCKET:-${PROJECT}-tofu-state}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-${PROJECT}-tofu-artifacts}"

KMS_KEYRING="${KMS_KEYRING:-driftscribe-tofu}"
KMS_KEY="${KMS_KEY:-tofu-state}"
# KMS location: keep the key co-located with the state it encrypts. The state
# bucket below is created in $REGION (asia-northeast1), so the keyring uses the
# same single region — not "global" — to minimize cross-region exposure and
# latency. KMS keyrings are immutable in location; if you ever move the bucket
# you must create a new keyring.
KMS_LOCATION="${KMS_LOCATION:-${REGION}}"

# Workload Identity Federation — pool + GitHub OIDC provider.
WIF_POOL="${WIF_POOL:-github-actions}"
WIF_PROVIDER="${WIF_PROVIDER:-github-oidc}"
# The canonical repo the OIDC provider trusts (owner/repo). Fork PRs run under a
# different `repository` claim and are rejected by the attribute condition.
GITHUB_REPO="${GITHUB_REPO:-adi-prasetyo/driftscribe}"
# The workflow file the plan-builder runs from (the `workflow_ref` claim's path
# component). Pinning this means only THIS workflow can mint GCP creds, so a new
# attacker-authored workflow in the same repo cannot impersonate the CI SA.
GITHUB_WORKFLOW="${GITHUB_WORKFLOW:-.github/workflows/iac.yml}"
# The trusted branch whose PUSHES may obtain creds. Specified as the BARE branch
# name (e.g. "main"); the push gate uses the FULL ref ("refs/heads/main"), built
# below as GITHUB_PUSH_REF. PRs (incl. fork PRs) deliberately do NOT obtain
# creds — see the CEL condition in §6 and the header banner above.
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
GITHUB_PUSH_REF="refs/heads/${GITHUB_BRANCH}"

# CI plan-builder service account (Phase C identity).
CI_SA_NAME="${CI_SA_NAME:-tofu-plan-builder}"
CI_SA="${CI_SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

# --------------------------------------------------------------------------
# 0. Pre-flight: confirm the project exists and the caller can act on it.
#    Mirrors setup_e2e_project.sh — we DO NOT auto-create the project.
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
echo "Provisioning IaC backend for ${PROJECT} (region ${REGION}) as ${CALLER_EMAIL}..."

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

# --------------------------------------------------------------------------
# 1. APIs — KMS + IAM Credentials + Storage are required by the backend,
#    encryption, and WIF token exchange. `gcloud services enable` is
#    server-side idempotent (helper from _setup_lib.sh).
# --------------------------------------------------------------------------
# - cloudkms:        the gcp_kms key provider's encrypt/decrypt at init/plan.
# - iamcredentials:  WIF token exchange (STS -> short-lived SA access token).
# - sts:             the WIF token-exchange endpoint itself.
# - storage:         the gcs backend + artifact bucket APIs.
# - run:             read-only describe surface `tofu plan` walks for the
#                    payment-demo google_cloud_run_v2_service refresh.
# (M-2) compute.googleapis.com is intentionally NOT enabled: the only plan
# refresh target is google_cloud_run_v2_service (Cloud Run admin API), there are
# no compute resources in iac/, and the google provider initializes without the
# Compute API. Keeping the enabled-API surface minimal; add it later if/when a
# compute resource is imported.
enable_apis_idempotent "$PROJECT" \
  cloudkms.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  storage.googleapis.com \
  sts.googleapis.com

# --------------------------------------------------------------------------
# 2. State bucket — Object Versioning ON + uniform bucket-level access.
# --------------------------------------------------------------------------
# Versioning lets the operator recover a clobbered/corrupt state generation;
# UBLA disables per-object ACLs so access is IAM-only (no legacy ACL bypass).
# `gcloud storage buckets describe` is the idempotency gate; `buckets create`
# fails if the global bucket name is taken by someone else, which is the
# correct loud failure (do not silently continue).
if gcloud storage buckets describe "gs://${STATE_BUCKET}" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "  state bucket gs://${STATE_BUCKET} already exists — ensuring versioning + UBLA"
else
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="$PROJECT" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
  echo "  state bucket gs://${STATE_BUCKET}: created"
fi
# `buckets update` is idempotent server-side; apply unconditionally so an
# adopted/legacy bucket converges to the required config on re-run. PAP
# (enforced) is re-applied here too so a PRE-EXISTING bucket converges to the
# public-access-prevention the runbook claims is enforced — not just newly
# created ones.
gcloud storage buckets update "gs://${STATE_BUCKET}" \
  --project="$PROJECT" \
  --versioning \
  --uniform-bucket-level-access \
  --public-access-prevention >/dev/null
echo "  state bucket gs://${STATE_BUCKET}: versioning + UBLA + PAP enforced"

# --------------------------------------------------------------------------
# 3. Artifact bucket — versioned. Reserved for Phase C plan artifacts.
# --------------------------------------------------------------------------
# Separate from state (design doc §6): the plan-builder writes plan.tfplan +
# plan.json here, never into the state bucket. Created now so the
# self-protection denylist has a concrete target; no IAM write grant is added
# in Phase A (the apply pipeline grants artifact write in Phase C).
if gcloud storage buckets describe "gs://${ARTIFACT_BUCKET}" \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "  artifact bucket gs://${ARTIFACT_BUCKET} already exists — ensuring versioning + UBLA"
else
  gcloud storage buckets create "gs://${ARTIFACT_BUCKET}" \
    --project="$PROJECT" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
  echo "  artifact bucket gs://${ARTIFACT_BUCKET}: created"
fi
gcloud storage buckets update "gs://${ARTIFACT_BUCKET}" \
  --project="$PROJECT" \
  --versioning \
  --uniform-bucket-level-access \
  --public-access-prevention >/dev/null
echo "  artifact bucket gs://${ARTIFACT_BUCKET}: versioning + UBLA + PAP enforced"

# --------------------------------------------------------------------------
# 4. Cloud KMS — keyring + key for OpenTofu state/plan encryption.
# --------------------------------------------------------------------------
# Bootstrapped out-of-band (design doc §3.2) to avoid the chicken-and-egg of
# managing the encryption key inside the very state it encrypts. The key is a
# symmetric encrypt/decrypt key; OpenTofu's gcp_kms provider derives a 32-byte
# (AES-256) data key per the iac/versions.tf `key_length = 32`.
#
# REGION-CHANGE FOOTGUN (M-1): the describe-gate below keys on keyring
# name + location. KMS keyring location is IMMUTABLE. If you ever re-run with a
# different REGION/KMS_LOCATION after the keyring exists, this would silently
# create a SECOND keyring in the new location while existing state stays
# encrypted under the OLD key — `tofu init` would then fail to decrypt. Do NOT
# change KMS_LOCATION once state exists; migrate deliberately (decrypt with the
# old key, re-encrypt with the new) instead.
if gcloud kms keyrings describe "$KMS_KEYRING" \
     --project="$PROJECT" --location="$KMS_LOCATION" >/dev/null 2>&1; then
  echo "  KMS keyring ${KMS_KEYRING} (${KMS_LOCATION}) already exists — skipping"
else
  gcloud kms keyrings create "$KMS_KEYRING" \
    --project="$PROJECT" --location="$KMS_LOCATION"
  echo "  KMS keyring ${KMS_KEYRING} (${KMS_LOCATION}): created"
fi

if gcloud kms keys describe "$KMS_KEY" \
     --project="$PROJECT" --location="$KMS_LOCATION" \
     --keyring="$KMS_KEYRING" >/dev/null 2>&1; then
  echo "  KMS key ${KMS_KEY} already exists — skipping"
else
  gcloud kms keys create "$KMS_KEY" \
    --project="$PROJECT" --location="$KMS_LOCATION" \
    --keyring="$KMS_KEYRING" \
    --purpose=encryption
  echo "  KMS key ${KMS_KEY}: created"
fi

# Full key resource path — this is the value for `var.tofu_state_kms_key`.
KMS_KEY_PATH="projects/${PROJECT}/locations/${KMS_LOCATION}/keyRings/${KMS_KEYRING}/cryptoKeys/${KMS_KEY}"

# --------------------------------------------------------------------------
# 5. CI plan-builder service account (Phase C identity, scripted now).
# --------------------------------------------------------------------------
# Distinct from every runtime/worker SA — this identity exists ONLY to run
# `tofu plan` in CI via WIF. It holds NO project-wide grants; every binding
# below is resource-scoped and justified.
create_service_account_idempotent "$PROJECT" "$CI_SA_NAME" \
  "DriftScribe OpenTofu plan-builder (CI via WIF)"

# 5a. State bucket: roles/storage.objectAdmin — REQUIRED, not optional.
# The gcs backend acquires a lock by WRITING a lock object even during plan
# (design doc §3.2), so a read-only role would make `tofu plan` fail to lock.
# objectAdmin is scoped to the STATE bucket resource only (bucket-level IAM),
# never project-wide and never on the artifact bucket.
gcloud storage buckets add-iam-policy-binding "gs://${STATE_BUCKET}" \
  --project="$PROJECT" \
  --member="serviceAccount:${CI_SA}" \
  --role="roles/storage.objectAdmin" >/dev/null
echo "  ${CI_SA}: storage.objectAdmin on gs://${STATE_BUCKET} (state lock needs object write)"

# 5b. KMS: encrypt/decrypt on the single key — to read/write encrypted state.
# roles/cloudkms.cryptoKeyEncrypterDecrypter is the minimal role that grants
# encrypt + decrypt without key management (no create/destroy/setIamPolicy).
# Scoped to the single key resource, not the keyring and not the project.
gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
  --project="$PROJECT" --location="$KMS_LOCATION" --keyring="$KMS_KEYRING" \
  --member="serviceAccount:${CI_SA}" \
  --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" >/dev/null
echo "  ${CI_SA}: cryptoKeyEncrypterDecrypter on ${KMS_KEY} (state/plan encryption)"

# 5c. Per-API READ roles for `tofu plan` refresh of the imported Cloud Run
# service. The plan refreshes google_cloud_run_v2_service.payment_demo, which
# the google provider reads via the Cloud Run admin API. roles/run.viewer is
# the smallest role granting run.services.get/list. NO blanket roles/viewer —
# honors the "per-API viewer, no project-wide grant" invariant
# (project_structure.md; design doc §3.3, §11.8 decision 5). Project-scoped
# because the provider may also list to resolve the resource; resource-scoped
# IAM on a single service does not cover provider list calls.
grant_role_idempotent "$PROJECT" "serviceAccount:${CI_SA}" "roles/run.viewer"
echo "  ${CI_SA}: run.viewer (project) — read-only refresh of payment-demo for tofu plan"

# Note: storage.objectAdmin (5a) already covers reading the backend state
# object; no extra storage read role is needed for the plan refresh.

# --------------------------------------------------------------------------
# 6. Workload Identity Federation — pool + GitHub OIDC provider.
# --------------------------------------------------------------------------
# WIF lets a GitHub Actions OIDC token be exchanged for a short-lived token for
# CI_SA — NO long-lived service-account JSON key anywhere (design doc §11.8
# decision 8). This is the Phase C plan-builder's auth path.
if gcloud iam workload-identity-pools describe "$WIF_POOL" \
     --project="$PROJECT" --location=global >/dev/null 2>&1; then
  echo "  WIF pool ${WIF_POOL} already exists — skipping"
else
  gcloud iam workload-identity-pools create "$WIF_POOL" \
    --project="$PROJECT" --location=global \
    --display-name="GitHub Actions" \
    --description="OIDC federation for DriftScribe GitHub Actions (Phase C plan-builder)"
  echo "  WIF pool ${WIF_POOL}: created"
fi

# Attribute MAPPING: surface the GitHub OIDC claims we condition on. google.subject
# is mandatory; the rest are mapped so the attribute CONDITION below can reference
# them and so the SA's principalSet binding can pin attribute.repository.
# base_ref is intentionally NOT mapped: we no longer gate on the pull_request
# event at all (see the condition below), so the PR target branch is unused.
WIF_ATTR_MAPPING="google.subject=assertion.sub"
WIF_ATTR_MAPPING+=",attribute.repository=assertion.repository"
WIF_ATTR_MAPPING+=",attribute.ref=assertion.ref"
WIF_ATTR_MAPPING+=",attribute.event_name=assertion.event_name"
WIF_ATTR_MAPPING+=",attribute.workflow_ref=assertion.workflow_ref"

# Attribute CONDITION (CEL): tokens are accepted ONLY when ALL hold:
#   - repository == the canonical repo             -> see fork note below
#   - workflow_ref starts with "<repo>/<workflow>" -> only THIS workflow file mints creds
#   - AND the event is a TRUSTED TRIGGER, one of:
#       * push to the trusted branch (ref == "refs/heads/<branch>"), OR
#       * workflow_dispatch (a maintainer manually running the workflow)
#     -> the `pull_request` event is NOT granted creds at all.
#
# FORK-PR FIX (verified against GitHub's OIDC docs, 2026-05-27): `repository ==`
# does NOT exclude fork PRs. For a PR opened from a fork, GitHub runs the
# `pull_request` event in the BASE repo, so the `repository` claim ("the
# repository from where the workflow is running") is the BASE repo — the
# canonical repo — even for fork PRs. Gating `pull_request` on `repository ==`
# would therefore admit fork PRs. The fix is to drop the `pull_request` clause
# entirely and grant ONLY trusted triggers: a push to the trusted branch (whose
# `ref` is the FULL "refs/heads/<branch>", verified against the docs) or a
# maintainer-initiated workflow_dispatch (`event_name == 'workflow_dispatch'`).
# The Phase C plan-builder must run from a trusted trigger, never fork-PR OIDC.
# Enforced at the provider BEFORE any SA binding — a token failing the condition
# never even maps to a principal. workflow_ref looks like
# "owner/repo/.github/workflows/iac.yml@refs/heads/main"; we match its prefix.
WIF_ATTR_CONDITION="assertion.repository == '${GITHUB_REPO}'"
WIF_ATTR_CONDITION+=" && assertion.workflow_ref.startsWith('${GITHUB_REPO}/${GITHUB_WORKFLOW}@')"
WIF_ATTR_CONDITION+=" && ((assertion.event_name == 'push' && assertion.ref == '${GITHUB_PUSH_REF}')"
WIF_ATTR_CONDITION+=" || assertion.event_name == 'workflow_dispatch')"

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
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="$WIF_ATTR_MAPPING" \
    --attribute-condition="$WIF_ATTR_CONDITION"
  echo "  WIF provider ${WIF_PROVIDER}: created"
fi

# 6a. Bind the CI SA so the federated GitHub identity may impersonate it.
# roles/iam.workloadIdentityUser is granted to a principalSet RESTRICTED to the
# canonical repository attribute — defense in depth on top of the provider's
# attribute condition. The pool's full resource name is needed for the
# principalSet member string.
WIF_POOL_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}"
# principalSet pins attribute.repository as defense in depth: even if the
# provider condition were ever loosened, only tokens carrying THIS repository
# claim can impersonate the SA. (Note this alone does NOT exclude fork PRs — the
# fork-PR `repository` claim is the base repo; the real fork-PR exclusion lives
# in the provider condition above, which grants only push/workflow_dispatch.)
gcloud iam service-accounts add-iam-policy-binding "$CI_SA" \
  --project="$PROJECT" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${WIF_POOL_NAME}/attribute.repository/${GITHUB_REPO}" >/dev/null
echo "  ${CI_SA}: workloadIdentityUser for principalSet repository=${GITHUB_REPO}"

WIF_PROVIDER_NAME="${WIF_POOL_NAME}/providers/${WIF_PROVIDER}"

# --------------------------------------------------------------------------
# 7. Summary — values the operator wires in.
# --------------------------------------------------------------------------
cat <<EOF

================================================================
setup_iac_backend.sh: complete

PHASE A — wire this NOW (needed for the operator import in iac/):

  var.tofu_state_kms_key =
    ${KMS_KEY_PATH}

  Pass it at init/plan/apply time, e.g.:
    cd iac
    tofu init  -var "tofu_state_kms_key=${KMS_KEY_PATH}"
    tofu plan  -var "tofu_state_kms_key=${KMS_KEY_PATH}"
  (or keep it in a local tofu.tfvars you do NOT commit). State + plan
  encryption is enforced from t=0; the backend bucket below already exists.

  State backend bucket:    gs://${STATE_BUCKET}   (prefix "prod", versioned)
  Artifact bucket:         gs://${ARTIFACT_BUCKET} (Phase C plan artifacts)

PHASE C — wire this LATER (NOT a Phase A done-condition; the plan-builder
workflow + authenticated plan land in Phase C):

  WIF provider resource name (google-github-actions/auth workload_identity_provider):
    ${WIF_PROVIDER_NAME}

  CI plan-builder service account (google-github-actions/auth service_account):
    ${CI_SA}

  The provider only accepts tokens from repo ${GITHUB_REPO}, workflow
  ${GITHUB_WORKFLOW}, on a TRUSTED TRIGGER: a push to ${GITHUB_PUSH_REF} (ref)
  or a maintainer-initiated workflow_dispatch. The pull_request event gets NO
  credentials -- fork PRs included -- because the repository OIDC claim cannot
  distinguish a fork PR from a base-repo run on the pull_request event.

Operator MUST customize before running if any default is wrong:
  - GITHUB_REPO     (currently ${GITHUB_REPO})
  - GITHUB_WORKFLOW (currently ${GITHUB_WORKFLOW})
  - GITHUB_BRANCH   (currently ${GITHUB_BRANCH})    <- trusted branch (BARE name)
  - KMS_LOCATION    (currently ${KMS_LOCATION}) <- IMMUTABLE once the keyring
                    exists; changing it after state exists strands the old key
                    (see the KMS section's REGION-CHANGE FOOTGUN note)
================================================================
EOF
