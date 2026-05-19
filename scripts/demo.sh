#!/usr/bin/env bash
# DriftScribe live-demo scenario runner (Phase 16.2).
#
# Purpose:
#   Drives the six "beats" of the hackathon demo against a deployed
#   coordinator (Cloud Run `driftscribe-agent`). Each beat mutates the
#   `payment-demo` Cloud Run env, then invokes /recheck or /chat on the
#   coordinator and prints the result alongside the response's
#   X-Trace-Id header — the operator pastes that ID into Cloud Logging
#   to follow the agent's reasoning chain.
#
#   This is a DEMO runner, not a CI smoke test (that's
#   infra/scripts/e2e_smoke.sh). There are no pass/fail assertions —
#   the output is for humans to read at the keyboard.
#
# Usage:
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh all
#
# Required env:
#   PROJECT          GCP project ID hosting the deployed services.
# Optional env:
#   REGION           Cloud Run region (default: asia-northeast1).
#   TARGET_SERVICE   The drift target service (default: payment-demo).
#
# Requirements (assumed available on the operator's box):
#   - bash, curl, gcloud
#   - jq is optional; if missing, response bodies print raw.
#
# Notes:
#   - The coordinator URL and operator token are resolved once at
#     startup. If either lookup fails the script exits non-zero before
#     touching `payment-demo`.
#   - `gcloud run services update` calls use --quiet to avoid the
#     interactive confirmation prompt during a live demo.
#   - beats c and e exercise the ADK path on the coordinator. If the
#     deployed revision has USE_ADK=false, beat-c falls back to
#     classical classification (returns `escalate`) and beat-e returns
#     503 from /chat. Both are intentional teaching moments — the
#     script surfaces the 503 body rather than swallowing it.

set -uo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project ID}"
REGION="${REGION:-asia-northeast1}"
TARGET_SERVICE="${TARGET_SERVICE:-payment-demo}"

# --------------------------------------------------------------------------- #
# Startup: resolve coordinator URL + operator token, probe for jq.
# --------------------------------------------------------------------------- #

COORD_URL="$(gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || true)"
if [ -z "${COORD_URL:-}" ]; then
  echo "ERROR: could not resolve coordinator URL for driftscribe-agent" >&2
  echo "       (project=$PROJECT region=$REGION). Is the service deployed?" >&2
  exit 2
fi

TOKEN="$(gcloud secrets versions access latest \
  --secret=coordinator-shared-token \
  --project="$PROJECT" 2>/dev/null || true)"
if [ -z "${TOKEN:-}" ]; then
  echo "ERROR: could not read coordinator-shared-token secret from project $PROJECT" >&2
  echo "       (does the operator have roles/secretmanager.secretAccessor?)" >&2
  exit 2
fi

HAVE_JQ=0
if command -v jq >/dev/null 2>&1; then
  HAVE_JQ=1
else
  echo "[warn] jq not installed — response bodies will print raw" >&2
fi

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Pretty section header. The 60-char rule keeps banners legible on a
# ~80-col demo terminal without wrapping.
banner() {
  local title="$1"
  echo
  echo "============================================================"
  echo "  $title"
  echo "============================================================"
}

# Print a body either through jq or raw, depending on availability.
# Reads from stdin so the caller can pipe a curl response body in.
print_body() {
  if [ "$HAVE_JQ" = "1" ]; then
    # `jq .` exits non-zero on non-JSON input (e.g. a 503 plaintext
    # detail). Fall back to raw so the operator still sees the error.
    jq . 2>/dev/null || cat
  else
    cat
  fi
}

# Call the coordinator. Captures BOTH the headers and body via
# `curl -i`, parses out X-Trace-Id, prints status + trace ID on a
# dedicated line, then pretty-prints the body. Does NOT abort on
# non-2xx — the live demo includes deliberate failure cases (beat-e
# on USE_ADK=false returns 503, and the operator should see it).
#
# Args:
#   $1  endpoint path (e.g. /recheck)
#   $2  JSON body to POST
call_coordinator() {
  local endpoint="$1" body="$2"
  local tmp http_code trace_id

  tmp="$(mktemp)"
  # -i emits headers + blank line + body. -w '%{http_code}' captures the
  # status separately so we don't have to parse the HTTP/1.1 status line
  # out of the response.
  http_code="$(curl -sS -i \
    -o "$tmp" \
    -w '%{http_code}' \
    -X POST "${COORD_URL}${endpoint}" \
    -H "X-DriftScribe-Token: ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "$body" || echo "000")"

  # Trace-Id parsing: grep case-insensitive, take the first match (curl
  # may emit it twice for redirects, though /recheck and /chat don't
  # redirect), strip CRLF tail.
  trace_id="$(grep -i '^x-trace-id:' "$tmp" \
    | head -1 \
    | awk '{print $2}' \
    | tr -d '\r\n' || true)"
  [ -z "$trace_id" ] && trace_id="(missing)"

  echo "  POST ${endpoint}"
  echo "  body: ${body}"
  echo "  <- HTTP ${http_code}  X-Trace-Id: ${trace_id}"
  echo

  # Strip the headers + blank line; awk turns on `p` after the first
  # blank line (allowing optional CR for CRLF-terminated headers).
  awk '/^\r?$/{p=1;next}p' "$tmp" | print_body

  rm -f "$tmp"
}

# Set or update env vars on the target Cloud Run service. The
# --update-env-vars flag is additive (preserves other vars) — what we
# want for layered beats.
set_env() {
  local kv="$1"
  echo "  [gcloud] update-env-vars ${kv} on ${TARGET_SERVICE}"
  gcloud run services update "$TARGET_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --update-env-vars="$kv" \
    --quiet >/dev/null
}

# Remove a single env var. `|| true` makes cleanup idempotent — gcloud
# returns non-zero if the var isn't currently set.
unset_env() {
  local key="$1"
  echo "  [gcloud] remove-env-vars ${key} on ${TARGET_SERVICE}"
  gcloud run services update "$TARGET_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --remove-env-vars="$key" \
    --quiet >/dev/null 2>&1 || true
}

# Wait for the new revision to start serving traffic. 5s covers a
# normal Cloud Run revision rollout (~3s typical, occasional 4–5s) and
# avoids a brittle polling loop in a live demo. If the revision takes
# longer the operator just re-runs the beat — failure here is louder
# than a stuck polling loop and the audience will forgive a retry.
wait_for_revision() {
  echo "  [wait] sleeping 5s for revision rollout"
  sleep 5
}

# --------------------------------------------------------------------------- #
# Beats
# --------------------------------------------------------------------------- #

# Beat A — baseline. No env mutation, just /recheck. Confirms the agent
# sees the target service as in-spec.
# Expected: action=no_op.
beat_a() {
  banner "Beat A — baseline check"
  echo "Expectation: action=no_op (target env matches contract)."
  call_coordinator /recheck '{}'
}

# Beat B — drift on a no-manual variable (PAYMENT_MODE).
# Contract says PAYMENT_MODE=mock with allow_manual_change=false.
# The Reader Worker observes 'live' → drift → classical path emits
# drift_issue (PR/Issue) rather than docs_pr.
# Expected: action=drift_issue, target_revision pointing at PAYMENT_MODE.
beat_b() {
  banner "Beat B — PAYMENT_MODE=live (no-manual drift)"
  echo "Expectation: action=drift_issue (allow_manual_change=false)."
  set_env "PAYMENT_MODE=live"
  wait_for_revision
  call_coordinator /recheck '{}'
}

# Beat C — unknown variable. PAYMENT_MODE is back to baseline (assuming
# cleanup ran first, or beat-b already flipped it — for a clean
# demonstration of "unknown var" specifically, run cleanup before this).
# Behavior depends on USE_ADK:
#   - USE_ADK=true:  ADK may propose docs_pr if a corresponding doc
#                    section is inferable, else escalate.
#   - USE_ADK=false: classical classifier always escalates for unknowns.
# Expected: action=docs_pr OR escalate (script does not assert).
beat_c() {
  banner "Beat C — NEW_THING=test (unknown variable)"
  echo "Expectation: action=docs_pr (ADK) or escalate (classical)."
  set_env "NEW_THING=test"
  wait_for_revision
  call_coordinator /recheck '{}'
}

# Beat D — drift on an operator-toggleable variable.
# Contract: FEATURE_NEW_CHECKOUT=false, allow_manual_change=true.
# Drift on a manual-OK var → docs_pr proposal with a preview body and
# a target docs file under demo/docs/runbook.md.
# Expected: action=docs_pr, target_docs_file present in the response.
beat_d() {
  banner "Beat D — FEATURE_NEW_CHECKOUT=true (manual-OK drift)"
  echo "Expectation: action=docs_pr (allow_manual_change=true)."
  set_env "FEATURE_NEW_CHECKOUT=true"
  wait_for_revision
  call_coordinator /recheck '{}'
}

# Beat E — rollback via /chat. Requires USE_ADK=true on the coordinator
# revision. Flow:
#   1. We deliberately drift PAYMENT_MODE again (so the ADK has a real
#      drift to act on — a "roll us back" prompt with the env already
#      compliant would be a no-op).
#   2. /chat receives a natural-language operator request.
#   3. ADK calls the rollback worker → coordinator returns an
#      approval_url. The operator clicks the URL in a browser; that
#      step is OUT OF SCOPE for this headless runner.
# Expected: action=rollback, approval_url present in the response.
# Fallback: with USE_ADK=false, /chat returns 503 — the script surfaces
# the body so the audience sees it's an env-config issue.
beat_e() {
  banner "Beat E — combo drift + /chat rollback"
  echo "Expectation: action=rollback with approval_url (USE_ADK=true)."
  echo "             OR 503 'ADK not enabled' (USE_ADK=false)."
  set_env "PAYMENT_MODE=live"
  wait_for_revision
  call_coordinator /chat \
    '{"prompt":"payment mode drifted. please propose a rollback."}'
}

# Cleanup — restore the baseline declared in demo/ops-contract.yaml.
# Idempotent: unset_env swallows the "not currently set" error so
# running cleanup twice in a row is fine. Useful as a pre-flight step
# before the live demo.
cleanup() {
  banner "Cleanup — restore baseline env"
  set_env "PAYMENT_MODE=mock"
  set_env "FEATURE_NEW_CHECKOUT=false"
  unset_env "NEW_THING"
  echo "  [done] payment-demo env restored to contract baseline"
}

# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

usage() {
  cat <<EOF
Usage: $0 <beat>

Beats:
  beat-a    baseline check (expect no_op)
  beat-b    flip PAYMENT_MODE=live (expect drift_issue)
  beat-c    flip NEW_THING=test (expect docs_pr or escalate)
  beat-d    flip FEATURE_NEW_CHECKOUT=true (expect docs_pr)
  beat-e    combo drift + /chat rollback (expect rollback w/ approval URL)
  cleanup   restore baseline env on $TARGET_SERVICE
  all       run beat-a..beat-e then cleanup, sequentially

Environment:
  PROJECT          GCP project (required)
  REGION           Cloud Run region (default: asia-northeast1)
  TARGET_SERVICE   drift target (default: payment-demo)

Currently resolved:
  coordinator: $COORD_URL
  target:      $TARGET_SERVICE (region=$REGION)
EOF
}

case "${1:-}" in
  beat-a)   beat_a ;;
  beat-b)   beat_b ;;
  beat-c)   beat_c ;;
  beat-d)   beat_d ;;
  beat-e)   beat_e ;;
  cleanup)  cleanup ;;
  all)
    # `all` is for dry-runs and recording the demo end-to-end. Each
    # beat is independent; if one fails the rest still run.
    beat_a
    beat_b
    beat_c
    beat_d
    beat_e
    cleanup
    ;;
  ""|-h|--help)
    usage
    exit 1
    ;;
  *)
    echo "ERROR: unknown beat: $1" >&2
    echo >&2
    usage >&2
    exit 1
    ;;
esac
