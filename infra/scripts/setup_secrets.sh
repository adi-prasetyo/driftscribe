#!/usr/bin/env bash
# Idempotent bootstrap for a fresh DriftScribe deployment target.
#
# Usage: setup_secrets.sh PROJECT GITHUB_TOKEN GOOGLE_API_KEY
#
# Run once when standing up a new GCP project. Safe to re-run (gcloud commands
# are idempotent; secret writes append a new version each call).
set -euo pipefail

PROJECT="${1:?usage: $0 PROJECT GITHUB_TOKEN GOOGLE_API_KEY}"
GITHUB_TOKEN="${2:?}"
GOOGLE_API_KEY="${3:?}"

# Enable APIs (artifactregistry is required — Cloud Build pushes to AR, not the
# legacy gcr.io bucket).
gcloud services enable --project "$PROJECT" \
  run.googleapis.com \
  eventarc.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# Artifact Registry repo for agent + demo images.
gcloud artifacts repositories describe driftscribe \
  --project "$PROJECT" --location=asia-northeast1 >/dev/null 2>&1 || \
gcloud artifacts repositories create driftscribe \
  --project "$PROJECT" --location=asia-northeast1 --repository-format=docker \
  --description="DriftScribe agent + payment-demo images"

# IAM grants. Cloud Build SA pushes to AR, deploys to Cloud Run, and acts-as
# the default compute SA (which runs the deployed services).
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

# Default compute SA (the deployed agent's runtime identity) reads Secret
# Manager + Firestore + Cloud Run state.
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for role in \
  roles/secretmanager.secretAccessor \
  roles/datastore.user \
  roles/run.viewer \
; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${COMPUTE_SA}" --role="$role" >/dev/null
done

# Secrets — names match cloudbuild.yaml's --set-secrets lines.
for secret in github-pat gemini-api-key; do
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --project "$PROJECT" --replication-policy=automatic
done

echo -n "$GITHUB_TOKEN"   | gcloud secrets versions add github-pat     --project "$PROJECT" --data-file=-
echo -n "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --project "$PROJECT" --data-file=-

# Firestore Native — single DB per project, location-bound.
gcloud firestore databases describe --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud firestore databases create --project "$PROJECT" --location=asia-northeast1 --type=firestore-native

echo "secrets + firestore ready"
