#!/usr/bin/env bash
# Open/close/inspect the hackathon judging window's CF Access bypass
# (work item A.3 — docs/plans/2026-06-12-hackathon-judge-readiness-design.md).
#
#   demo-window.sh on      create the Everyone-bypass policy (edge gate OPEN)
#   demo-window.sh off     delete it (edge gate restored)
#   demo-window.sh status  policy + live edge state, no changes
#   demo-window.sh probe   anonymous edge probe only, exit 0 iff fully OPEN
#                          (no CF API token needed — used by demo-health.yml)
#
# The bypass policy only opens the EDGE. What anonymous traffic can then do
# is decided by the Worker's DEMO_MODE (infra/cloudflare/worker/) — with
# DEMO_MODE="0" anonymous visitors reach the origin but every authed route
# 401s (no token is injected; a caller who already knows the static operator
# token could still present it, exactly as they always can on the public
# run.app URL). Full window = autonomy pinned + DEMO_MODE=1 + bypass on,
# in that order; this script prints the ordering checklist on every flip.
#
# Known accepted caveat (Codex, A.1 review): the Everyone-bypass strips the
# CF Access JWT for OPERATORS on this hostname too, so POST /iac-approvals/{n}
# 401s for everyone mid-window. Flip `off` briefly if an approve is needed.
#
# Required env var (load via `set -a; source .env; set +a` at repo root) —
# needed for on/off/status only; `probe` runs credential-free:
#   CLOUDFLARE_DRIFTSCRIBE_API_TOKEN   (Account:Access Apps+Policies:Edit)

set -euo pipefail

ACCOUNT_ID="7e8265bd122c779322afe1b236623346"
HOST="driftscribe.adp-app.com"
TEAM_DOMAIN="adp-app.cloudflareaccess.com"
BYPASS_NAME="driftscribe-demo-bypass"

API="https://api.cloudflare.com/client/v4"
JSON=(-H "Content-Type: application/json")

# The CF API token is only needed by subcommands that read/mutate Access
# policies (on/off/status). `probe` is anonymous-curl only, so it must run
# with no credentials at all (that is what lets demo-health.yml call it
# from a zero-secret scheduled job).
require_cf_token() {
  : "${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN:?load it first: set -a; source .env; set +a}"
  AUTH=(-H "Authorization: Bearer ${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN}")
}

# Strict call: any non-success response is fatal.
cf_call() {
  local out
  out=$(curl -sS "${AUTH[@]}" "${JSON[@]}" "$@")
  if [[ "$(jq -r .success <<<"$out")" != "true" ]]; then
    echo "Cloudflare API call failed:" >&2
    printf '  curl %s\n' "$*" >&2
    echo "$out" | jq . >&2
    exit 1
  fi
  echo "$out"
}

# Exactly-one lookup: 0 matches → empty, 1 → the value, >1 → fatal.
# This drives a live safety toggle; never guess between duplicates.
exactly_one() { # $1 = label for the error message; stdin = newline-separated ids
  local ids n
  ids=$(cat)
  n=$(printf '%s' "$ids" | grep -c . || true)
  if (( n > 1 )); then
    echo "FATAL: multiple $1 matched — refusing to guess:" >&2
    printf '%s\n' "$ids" >&2
    exit 1
  fi
  printf '%s' "$ids"
}

# --- CF Access state ---------------------------------------------------------

find_app_id() {
  cf_call "$API/accounts/$ACCOUNT_ID/access/apps" \
    | jq -r --arg host "$HOST" '.result[] | select(.domain==$host) | .id' \
    | exactly_one "Access apps for $HOST"
}

# Fetched once per run; reused by the policy helpers below.
APP_ID=""
POLICIES=""
load_policies() {
  require_cf_token
  APP_ID=$(find_app_id)
  if [[ -z "$APP_ID" ]]; then
    echo "FATAL: no Access app gates $HOST — run setup-access.sh first" >&2
    exit 1
  fi
  POLICIES=$(cf_call "$API/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies")
}

bypass_policy_id() {
  jq -r --arg n "$BYPASS_NAME" '.result[] | select(.name==$n) | .id' <<<"$POLICIES" \
    | exactly_one "policies named $BYPASS_NAME"
}

# A pre-existing policy is only a valid no-op target if it is EXACTLY the
# policy we would create (Codex must-fix: "exists" alone is not "correct").
verify_bypass_shape() { # $1 = policy id
  local ok
  ok=$(jq -r --arg id "$1" '
    .result[] | select(.id==$id)
    | (.decision=="bypass" and .include==[{"everyone":{}}])' <<<"$POLICIES")
  if [[ "$ok" != "true" ]]; then
    echo "FATAL: policy $BYPASS_NAME exists but is NOT the expected" >&2
    echo "       {decision:bypass, include:[everyone]} — inspect manually:" >&2
    jq --arg id "$1" '.result[] | select(.id==$id)' <<<"$POLICIES" >&2
    exit 1
  fi
}

# Any OTHER bypass-decision policy is a louder alarm than ours: CF evaluates
# Bypass before Allow, so an unexpected one un-gates the host on its own.
warn_foreign_bypass() {
  local foreign
  foreign=$(jq -r --arg n "$BYPASS_NAME" \
    '.result[] | select(.decision=="bypass" and .name!=$n) | "  \(.name) (id=\(.id))"' \
    <<<"$POLICIES")
  if [[ -n "$foreign" ]]; then
    echo "WARNING: unexpected bypass-decision policies on this app:"
    printf '%s\n' "$foreign"
  fi
}

# --- Edge probes -------------------------------------------------------------
# Anonymous (no cookies), no redirect-following; cache-busting query +
# no-cache header so the propagation loop never reads a stale answer.

probe() { # $1 = path; echoes "<http_code> <redirect_url>"
  curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' \
    -H 'Cache-Control: no-cache' --max-time 15 \
    "https://$HOST$1?nocache=$(date +%s%N)" 2>/dev/null || echo "000 "
}

is_gated() { # gated = Access redirect to the team domain
  local code loc
  read -r code loc <<<"$(probe /)"
  [[ "$code" == "302" && "$loc" == *"$TEAM_DOMAIN"* ]]
}

# Retry up to ~30s for edge propagation after a flip.
wait_for_edge() { # $1 = "gated" | "open"
  local _i
  for _i in $(seq 1 15); do
    case "$1" in
      gated) is_gated && return 0 ;;
      open)  [[ "$(probe / | cut -d' ' -f1)" == "200" ]] && return 0 ;;
    esac
    sleep 2
  done
  return 1
}

edge_state() { # closed | half-open | open | odd:<codes>
  local root dec
  root=$(probe / | cut -d' ' -f1)
  if [[ "$root" == "302" ]]; then
    is_gated && { echo "closed"; return; }
    echo "odd:/=302-but-not-to-$TEAM_DOMAIN"
    return
  fi
  if [[ "$root" == "200" ]]; then
    dec=$(probe /decisions | cut -d' ' -f1)
    case "$dec" in
      200) echo "open" ;;
      401) echo "half-open" ;;
      *)   echo "odd:/=200,/decisions=$dec" ;;
    esac
    return
  fi
  echo "odd:/=$root"
}

describe_edge() {
  case "$1" in
    closed)    echo "CLOSED — anonymous / → Access login redirect (gate intact)" ;;
    half-open) echo "HALF-OPEN — bypass live but DEMO_MODE=0: anonymous visitors reach the SPA shell yet every API call 401s. Fine for a smoke test; WRONG for judging day." ;;
    open)      echo "OPEN — bypass live AND Worker demo mode injecting: anonymous /decisions serves data" ;;
    *)         echo "UNEXPECTED edge state ($1) — investigate before trusting the window" ;;
  esac
}

open_checklist() {
  cat <<EOF
Window-OPEN ordering (design doc runbook):
  1. operator: POST /autonomy {"mode":"propose"}        (pin the dial FIRST)
  2. worker/wrangler.toml DEMO_MODE="1" + wrangler deploy
     — deploy output MUST list env.CHAT_RATE_LIMIT as "Rate Limit (5 requests/60s)"
  3. this script: demo-window.sh on                      <- edge gate, LAST
EOF
}

close_checklist() {
  cat <<EOF
Window-CLOSE ordering (reverse of open):
  1. this script: demo-window.sh off                     <- edge gate, FIRST
  2. worker/wrangler.toml DEMO_MODE="0" + wrangler deploy
  3. operator: restore the autonomy dial if desired
EOF
}

# --- Subcommands -------------------------------------------------------------

cmd_on() {
  open_checklist
  echo
  load_policies
  warn_foreign_bypass
  local pol_id
  pol_id=$(bypass_policy_id)
  if [[ -n "$pol_id" ]]; then
    verify_bypass_shape "$pol_id"
    echo "Bypass policy already present and correctly shaped (id=$pol_id)."
  else
    # No explicit precedence: the API rejects duplicates and does not
    # auto-shift, so the policy lands after the allowlist numerically —
    # harmless, because CF evaluates Bypass-decision policies before
    # Allow ones regardless of precedence (verified live by the probe
    # below; docs: developers.cloudflare.com/cloudflare-one/access-controls/policies/).
    local body
    body=$(jq -nc --arg n "$BYPASS_NAME" '{
      name:$n, decision:"bypass", include:[{everyone:{}}]
    }')
    pol_id=$(cf_call -X POST "$API/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies" \
      --data "$body" | jq -r .result.id)
    echo "Bypass policy created (id=$pol_id) — the edge gate on $HOST is now OPEN."
  fi
  echo "Waiting for the edge to serve anonymously..."
  if ! wait_for_edge open; then
    echo "FATAL: policy is in place but anonymous / still does not return 200 after 30s" >&2
    exit 1
  fi
  local state
  state=$(edge_state)
  describe_edge "$state"
  if [[ "$state" == "half-open" ]]; then
    echo "WARNING: to fully open the window, deploy the Worker with DEMO_MODE=\"1\" (step 2 above)."
  fi
  # half-open is a legitimate staging state (June smoke); an odd:* edge
  # must not look like a successful toggle.
  if [[ "$state" == odd:* ]]; then
    exit 1
  fi
}

cmd_off() {
  close_checklist
  echo
  load_policies
  warn_foreign_bypass
  local pol_id
  pol_id=$(bypass_policy_id)
  if [[ -z "$pol_id" ]]; then
    echo "Bypass policy already absent."
  else
    cf_call -X DELETE \
      "$API/accounts/$ACCOUNT_ID/access/apps/$APP_ID/policies/$pol_id" >/dev/null
    echo "Bypass policy deleted (id=$pol_id)."
  fi
  echo "Waiting for the edge gate to come back..."
  if ! wait_for_edge gated; then
    echo "FATAL: anonymous / still not redirected to Access after 30s — the gate is NOT confirmed closed" >&2
    exit 1
  fi
  describe_edge closed
}

cmd_status() {
  load_policies
  local pol_id
  pol_id=$(bypass_policy_id)
  if [[ -n "$pol_id" ]]; then
    verify_bypass_shape "$pol_id"
    echo "Bypass policy: PRESENT (id=$pol_id, correctly shaped)"
  else
    echo "Bypass policy: absent"
  fi
  warn_foreign_bypass
  jq -r '.result[] | "  policy \(.name): decision=\(.decision) precedence=\(.precedence)"' \
    <<<"$POLICIES"
  describe_edge "$(edge_state)"
}

cmd_probe() {
  # Anonymous edge probe only — no Cloudflare API token, no side effects.
  # Exit 0 iff the window is fully OPEN (bypass live + Worker DEMO_MODE=1
  # injecting); closed / half-open / odd:* exit 1 so schedulers
  # (.github/workflows/demo-health.yml) can alert on any of them.
  local state
  state=$(edge_state)
  describe_edge "$state"
  [[ "$state" == "open" ]]
}

case "${1:-}" in
  on)     cmd_on ;;
  off)    cmd_off ;;
  status) cmd_status ;;
  probe)  cmd_probe ;;
  *)
    echo "usage: $0 <on|off|status|probe>" >&2
    exit 2
    ;;
esac
