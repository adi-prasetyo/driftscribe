#!/usr/bin/env bash
# Synthetic judge-path health probe for the public demo window
# (docs/plans/2026-07-08-demo-health-monitor.md).
#
# Probes driftscribe.adp-app.com EXACTLY as an anonymous judge reaches it:
# through Cloudflare Access + the demo-proxy Worker. Three layers:
#   1. edge state              demo-window.sh probe (OPEN = bypass + DEMO_MODE=1)
#   2. GET /infra/graph        the Infra panel's data path (infra-reader chain
#                              + a canary for DEMO_ALLOWLIST regressions)
#   3. GET /decisions -> /trace/{id}  the reasoning-trace path (the 2026-07-06
#                              outage class: every /trace 503'd while / was 200)
#
# GET-only by design: POST /chat costs real Gemini money per run and the
# Worker rate-limits it per IP — a monitor must never spend or throttle.
# The whole sequence retries to ride out coordinator cold starts and CF
# blips before declaring failure (a 30-min-cadence monitor that cries wolf
# gets ignored by judging week).
#
# Env: RETRIES (default 3), RETRY_SLEEP seconds between attempts (default 40).
# Exit 0 = healthy, 1 = unhealthy after all retries.
set -euo pipefail

HOST="driftscribe.adp-app.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RETRIES="${RETRIES:-3}"
RETRY_SLEEP="${RETRY_SLEEP:-40}"

BODY="$(mktemp)"
trap 'rm -f "$BODY"' EXIT

get() { # $1 = path; body lands in $BODY, echoes the HTTP code (000 on error)
  local code
  code=$(curl -sS -o "$BODY" -w '%{http_code}' \
    -H 'Cache-Control: no-cache' --max-time 30 \
    "https://${HOST}$1?nocache=$(date +%s%N)" 2>/dev/null) || code="000"
  echo "$code"
}

probe_once() {
  echo "--- edge state (demo-window.sh probe)"
  if ! "$SCRIPT_DIR/demo-window.sh" probe; then
    echo "FAIL: edge is not fully OPEN"
    return 1
  fi

  echo "--- GET /infra/graph"
  local code
  code=$(get /infra/graph)
  if [[ "$code" != "200" ]] || ! jq -e . "$BODY" >/dev/null 2>&1; then
    echo "FAIL: /infra/graph code=${code} (want 200 + parseable JSON)"
    return 1
  fi

  echo "--- GET /decisions -> GET /trace/{id}"
  code=$(get /decisions)
  # Validate SHAPE, not just status: a 200 HTML error page (or truncated
  # JSON) must be a controlled, retryable FAIL. Without this check, jq's
  # extraction below would just come back empty and the missing-trace_id
  # branch would mis-report the run — and note that `set -e` does NOT
  # save us here: probe_once runs inside an `if`, where bash suppresses
  # errexit entirely.
  if [[ "$code" != "200" ]] || ! jq -e '(.decisions | type) == "array"' "$BODY" >/dev/null 2>&1; then
    echo "FAIL: /decisions code=${code} (want 200 + {decisions: [...]})"
    return 1
  fi
  local trace_id
  trace_id=$(jq -r 'first(.decisions[]?.trace_id | select(. != null and . != "")) // empty' "$BODY")
  if [[ -z "$trace_id" ]]; then
    # On this deployment the decisions log is never empty (months of
    # runs), and every row carries trace_id — so no traceable decision
    # means the store itself is broken or wiped. During the judging
    # window that is an incident, not a skip. (Codex review finding:
    # a SKIP here would silently disarm the /trace canary forever.)
    echo "FAIL: no decision carries a trace_id (decisions store empty or wiped?)"
    return 1
  fi
  code=$(get "/trace/${trace_id}")
  if [[ "$code" != "200" ]]; then
    echo "FAIL: /trace/${trace_id} code=${code} (want 200)"
    return 1
  fi
  echo "all probes passed"
}

for attempt in $(seq 1 "$RETRIES"); do
  echo "=== attempt ${attempt}/${RETRIES} — $(date -u +%FT%TZ)"
  if probe_once; then
    echo "HEALTHY"
    exit 0
  fi
  if [[ "$attempt" -lt "$RETRIES" ]]; then
    sleep "$RETRY_SLEEP"
  fi
done
echo "UNHEALTHY after ${RETRIES} attempts"
exit 1
