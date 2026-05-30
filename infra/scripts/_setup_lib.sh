# shellcheck shell=bash
# Shared helpers for DriftScribe operator setup scripts.
#
# Sourced by:
#   - infra/scripts/setup_secrets.sh     (prod bootstrap)
#   - infra/scripts/setup_e2e_project.sh (E2E bootstrap, Phase 20)
#
# Every helper is idempotent: a describe-then-act gate skips the create
# call if the resource already exists, but still applies IAM bindings
# (which are server-side idempotent). Re-runs are safe and chatty.
#
# Conventions:
#   - Functions take positional args; required args panic on missing.
#   - Every gcloud invocation passes --project=<arg> explicitly — never
#     rely on the operator's active gcloud config.
#   - Stdout/stderr is left UN-redirected so the calling script can
#     decide whether to tee it; helpers do not silently swallow errors.

set -euo pipefail

# -----------------------------------------------------------------------------
# enable_apis_idempotent PROJECT API [API ...]
#
# `gcloud services enable` is server-side idempotent (re-enabling an
# already-enabled API is a no-op). Wrapping it here keeps the call shape
# uniform across both setup scripts.
# -----------------------------------------------------------------------------
enable_apis_idempotent() {
  local project="${1:?enable_apis_idempotent: PROJECT required}"
  shift
  gcloud services enable --project "$project" "$@"
}

# -----------------------------------------------------------------------------
# enable_mcp_idempotent PROJECT API
#
# Explicitly opts the project into the remote MCP server fronting the
# given API. Wrapped with `|| echo` so a stale gcloud (pre-2025-03-17
# beta surface) doesn't break bootstrap — the underlying API enable
# above auto-enables MCP on newer gcloud anyway. Idempotent server-side.
# -----------------------------------------------------------------------------
enable_mcp_idempotent() {
  local project="${1:?enable_mcp_idempotent: PROJECT required}"
  local api="${2:?enable_mcp_idempotent: API required}"
  gcloud beta services mcp enable "$api" \
    --project="$project" >/dev/null 2>&1 || \
    echo "  note: 'gcloud beta services mcp enable $api' skipped (already enabled or beta surface unavailable in this gcloud version)"
}

# -----------------------------------------------------------------------------
# create_artifact_repo_idempotent PROJECT REPO LOCATION [DESCRIPTION]
#
# Describe-then-create the docker Artifact Registry repo cloudbuild
# pushes to. Re-runs print nothing on the happy path.
# -----------------------------------------------------------------------------
create_artifact_repo_idempotent() {
  local project="${1:?create_artifact_repo_idempotent: PROJECT required}"
  local repo="${2:?create_artifact_repo_idempotent: REPO required}"
  local location="${3:?create_artifact_repo_idempotent: LOCATION required}"
  local description="${4:-DriftScribe images}"
  gcloud artifacts repositories describe "$repo" \
    --project "$project" --location="$location" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$repo" \
    --project "$project" --location="$location" --repository-format=docker \
    --description="$description"
}

# -----------------------------------------------------------------------------
# create_service_account_idempotent PROJECT SA_NAME [DISPLAY_NAME]
#
# Describe-then-create. Pass the SHORT name (e.g. ``reader-agent-sa``),
# not the email — the email is reconstructed.
# -----------------------------------------------------------------------------
create_service_account_idempotent() {
  local project="${1:?create_service_account_idempotent: PROJECT required}"
  local sa="${2:?create_service_account_idempotent: SA_NAME required}"
  local display="${3:-DriftScribe ${sa}}"
  gcloud iam service-accounts describe "${sa}@${project}.iam.gserviceaccount.com" \
    --project="$project" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$sa" \
      --project="$project" \
      --display-name="$display"
}

# -----------------------------------------------------------------------------
# grant_role_idempotent PROJECT MEMBER ROLE
#
# Server-side idempotent — gcloud silently returns the existing policy
# if the binding is already in place. ``--condition=None --quiet`` blocks
# the conditional-policy prompt and a y/N pop on re-runs.
# -----------------------------------------------------------------------------
grant_role_idempotent() {
  local project="${1:?grant_role_idempotent: PROJECT required}"
  local member="${2:?grant_role_idempotent: MEMBER required (e.g. serviceAccount:sa@proj.iam.gserviceaccount.com)}"
  local role="${3:?grant_role_idempotent: ROLE required}"
  gcloud projects add-iam-policy-binding "$project" \
    --member="$member" --role="$role" \
    --condition=None --quiet >/dev/null
}

# -----------------------------------------------------------------------------
# create_secret_idempotent PROJECT SECRET_NAME
#
# Describe-then-create. Caller adds the version separately via
# ``gcloud secrets versions add`` so this helper stays neutral on the
# auto-generated vs operator-supplied distinction.
# -----------------------------------------------------------------------------
create_secret_idempotent() {
  local project="${1:?create_secret_idempotent: PROJECT required}"
  local secret="${2:?create_secret_idempotent: SECRET_NAME required}"
  gcloud secrets describe "$secret" --project "$project" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --project "$project" --replication-policy=automatic
}

# -----------------------------------------------------------------------------
# bind_secret_accessor PROJECT SECRET_NAME MEMBER
#
# Per-secret roles/secretmanager.secretAccessor binding. Skips with a
# log line if the secret hasn't been created yet (e.g. operator hasn't
# supplied the PAT) — matches the prod ``setup_secrets.sh`` UX so the
# script can be re-run after the operator populates the secret.
# -----------------------------------------------------------------------------
bind_secret_accessor() {
  local project="${1:?bind_secret_accessor: PROJECT required}"
  local secret="${2:?bind_secret_accessor: SECRET_NAME required}"
  local member="${3:?bind_secret_accessor: MEMBER required}"
  if gcloud secrets describe "$secret" --project "$project" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$secret" \
      --project "$project" \
      --member="$member" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
  else
    echo "  skipping bind: secret ${secret} not created yet"
  fi
}

# -----------------------------------------------------------------------------
# create_firestore_native_idempotent PROJECT LOCATION
#
# Describe-then-create the (default) Firestore database in Native mode.
# -----------------------------------------------------------------------------
create_firestore_native_idempotent() {
  local project="${1:?create_firestore_native_idempotent: PROJECT required}"
  local location="${2:?create_firestore_native_idempotent: LOCATION required}"
  gcloud firestore databases describe --project "$project" >/dev/null 2>&1 || \
    gcloud firestore databases create --project "$project" --location="$location" --type=firestore-native
}

# -----------------------------------------------------------------------------
# create_named_firestore_db_idempotent PROJECT DB_ID LOCATION
#
# Describe-then-create a NAMED Firestore database in Native mode (Phase C5f —
# isolates ``plan_approvals`` from the coordinator's project-wide datastore.user
# via per-database IAM conditioning). MUST be the SAME location as (default).
# -----------------------------------------------------------------------------
create_named_firestore_db_idempotent() {
  local project="${1:?create_named_firestore_db_idempotent: PROJECT required}"
  local db_id="${2:?create_named_firestore_db_idempotent: DB_ID required}"
  local location="${3:?create_named_firestore_db_idempotent: LOCATION required}"
  gcloud firestore databases describe --database="$db_id" \
    --project "$project" >/dev/null 2>&1 || \
    gcloud firestore databases create --database="$db_id" \
      --project "$project" --location="$location" --type=firestore-native
}

# -----------------------------------------------------------------------------
# grant_datastore_user_for_db PROJECT MEMBER DB_ID
#
# Grant roles/datastore.user CONDITIONED to a single Firestore database via the
# documented per-database pattern (CEL resource.name == the database resource).
# A project-level roles/datastore.user reaches ALL databases; this condition
# scopes it to exactly one (REST/client libraries enforce it). Server-side
# idempotent; coexists with any UN-conditioned binding for the same member+role
# (that is the bind-before-remove intermediate state — remove the un-conditioned
# one separately with remove_unconditioned_datastore_user). The (default) DB is
# spelled literally, parens and all, inside the string.
# -----------------------------------------------------------------------------
grant_datastore_user_for_db() {
  local project="${1:?grant_datastore_user_for_db: PROJECT required}"
  local member="${2:?grant_datastore_user_for_db: MEMBER required}"
  local db_id="${3:?grant_datastore_user_for_db: DB_ID required}"
  # Title may only contain letters/digits/spaces/hyphens/underscores — sanitize
  # the DB id (e.g. "(default)" -> "-default-") so the literal stays valid.
  local title="datastore-user-${db_id//[^a-zA-Z0-9]/-}"
  gcloud projects add-iam-policy-binding "$project" \
    --member="$member" --role="roles/datastore.user" \
    --condition="expression=resource.name == \"projects/${project}/databases/${db_id}\",title=${title}" \
    --quiet >/dev/null
}

# -----------------------------------------------------------------------------
# remove_unconditioned_datastore_user PROJECT MEMBER
#
# Remove the UN-conditioned project-wide roles/datastore.user binding for a
# member (the pre-C5f all-databases grant), leaving any DB-conditioned binding
# intact (``--condition=None`` targets ONLY the no-condition binding). ``|| true``
# makes it a no-op when already removed. This is the deliberate cutover step that
# completes the isolation — gated by the caller behind an explicit flag.
# -----------------------------------------------------------------------------
remove_unconditioned_datastore_user() {
  local project="${1:?remove_unconditioned_datastore_user: PROJECT required}"
  local member="${2:?remove_unconditioned_datastore_user: MEMBER required}"
  gcloud projects remove-iam-policy-binding "$project" \
    --member="$member" --role="roles/datastore.user" \
    --condition=None --quiet >/dev/null 2>&1 || true
}

# -----------------------------------------------------------------------------
# extend_log_retention_idempotent PROJECT DAYS
#
# Update the project's ``_Default`` Cloud Logging bucket retention. The
# update call is server-side idempotent; the describe guard turns
# re-runs into a deterministic "skipping" log line.
# -----------------------------------------------------------------------------
extend_log_retention_idempotent() {
  local project="${1:?extend_log_retention_idempotent: PROJECT required}"
  local days="${2:?extend_log_retention_idempotent: DAYS required}"
  local current
  current="$(gcloud logging buckets describe _Default \
    --project="$project" --location=global \
    --format='value(retentionDays)' 2>/dev/null || echo 0)"
  if [[ "$current" != "$days" ]]; then
    gcloud logging buckets update _Default \
      --project="$project" --location=global \
      --retention-days="$days" >/dev/null
    echo "  log retention: _Default bucket extended from ${current} to ${days} days"
  else
    echo "  log retention: _Default bucket already at ${days} days — skipping"
  fi
}
