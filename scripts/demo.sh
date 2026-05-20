#!/usr/bin/env bash
# DriftScribe live-demo scenario runner (Phase 16.2; upgrade beats added 17.C.6).
#
# Purpose:
#   Drives the demo "beats" against a deployed coordinator (Cloud Run
#   `driftscribe-agent`). Drift beats (a-e) mutate the `payment-demo`
#   Cloud Run env, then invoke /recheck or /chat. Upgrade beats (a-c)
#   exercise the `workload=upgrade` /chat path against the
#   `demo/upgrade-target/package.json` already committed to GitHub.
#
#   Each beat prints the response alongside the X-Trace-Id header —
#   the operator pastes that ID into Cloud Logging to follow the
#   agent's reasoning chain.
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
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-a
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-b
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-c
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh all
#   PROJECT=driftscribe-hack-2026 ./scripts/demo.sh all-upgrade
#
# Required env:
#   PROJECT          GCP project ID hosting the deployed services.
# Optional env:
#   REGION           Cloud Run region (default: asia-northeast1).
#   TARGET_SERVICE   The drift target service (default: payment-demo).
#   GITHUB_REPO      Repo slug used in upgrade-b's warning banner
#                    (default: adi-prasetyo/driftscribe — matches the
#                    coordinator's GITHUB_REPO env var; the agent will
#                    open the PR against whichever repo the deployed
#                    coordinator was configured with, not this var).
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
#   - Upgrade beats (a/b/c) require USE_ADK=true AND
#     UPGRADE_READER_URL + UPGRADE_DOCS_URL set on the coordinator
#     (the 17.E deploy infra wires these). Against a pre-17.E
#     coordinator the /chat call returns 503 "workload 'upgrade' is
#     not deployed" — itself a clean demonstration of the workload
#     pre-resolve guard added in Phase 17.A.3.
#   - Unlike the drift beats, upgrade beats do NOT mutate Cloud Run
#     env. The "baseline" for upgrade is the current GitHub state of
#     `demo/upgrade-target/package.json` on `main`. To change it the
#     operator must commit and push — see docs/demo-script.md.
#   - upgrade-b opens a REAL pull request on $GITHUB_REPO when the
#     coordinator is fully wired. Close/delete the PR after recording.

set -uo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project ID}"
REGION="${REGION:-asia-northeast1}"
TARGET_SERVICE="${TARGET_SERVICE:-payment-demo}"
GITHUB_REPO="${GITHUB_REPO:-adi-prasetyo/driftscribe}"

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
  # RETURN trap cleans up the tempfile on every exit path from this
  # function — including Ctrl-C during the curl, which docs/demo-script.md
  # tells operators to use on a hang. Without it the tempfile leaks into
  # /tmp on every interrupted beat. Function-local scope (RETURN, not EXIT)
  # so it doesn't interfere with any global trap a caller might install.
  # shellcheck disable=SC2064  # intentional expansion at trap-define time
  trap "rm -f '$tmp'" RETURN
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
  # Tempfile cleanup is handled by the RETURN trap above.
  awk '/^\r?$/{p=1;next}p' "$tmp" | print_body
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

# Reset payment-demo env to the contract baseline before applying a
# beat's own drift. Without this, beats compound (e.g. beat-c after
# beat-b would still have PAYMENT_MODE=live), so beat-c would no longer
# cleanly demonstrate "unknown variable only" and beat-d would no longer
# cleanly demonstrate "operator-safe flip" — the prior PAYMENT_MODE
# drift would dominate the decision.
#
# Idempotent: --update-env-vars overwrites existing values, and
# --remove-env-vars is wrapped in `|| true` inside unset_env so an
# already-absent var doesn't fail. Safe to call at the top of every
# non-baseline beat — each beat becomes independent and re-runnable in
# any order.
reset_baseline() {
  echo "  [reset] restoring baseline before applying beat drift"
  set_env "PAYMENT_MODE=mock"
  set_env "FEATURE_NEW_CHECKOUT=false"
  unset_env "NEW_THING"
}

# Upgrade workload "reset" — documentation-as-code analog of
# reset_baseline. The upgrade workload reads dependencies from the
# repo's package.json on GitHub via the Contents API (PyGithub), NOT
# from a Cloud Run env. There is no gcloud knob to flip mid-demo.
# This function is intentionally a no-op that echoes a reminder so
# the operator does not expect the upgrade beats to "reset" anything
# via this script.
#
# To change the upgrade baseline, edit
# demo/upgrade-target/package.json on `main` and push — that is the
# observation source. Phase 17.C.6 documents the pre-stage in
# docs/demo-script.md.
reset_baseline_upgrade() {
  echo "  [reset] upgrade baseline = current GitHub state of"
  echo "          demo/upgrade-target/package.json on \`main\`."
  echo "          Change it by committing + pushing, not by this script."
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
  reset_baseline
  set_env "PAYMENT_MODE=live"
  wait_for_revision
  call_coordinator /recheck '{}'
}

# Beat C — unknown variable. reset_baseline at the top guarantees
# PAYMENT_MODE=mock and FEATURE_NEW_CHECKOUT=false regardless of which
# beat ran before, so this beat cleanly isolates "unknown variable" as
# the only drift dimension.
# Behavior depends on USE_ADK:
#   - USE_ADK=true:  ADK may propose docs_pr if a corresponding doc
#                    section is inferable, else escalate.
#   - USE_ADK=false: classical classifier always escalates for unknowns.
# Expected: action=docs_pr OR escalate (script does not assert).
beat_c() {
  banner "Beat C — NEW_THING=test (unknown variable)"
  echo "Expectation: action=docs_pr (ADK) or escalate (classical)."
  reset_baseline
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
  reset_baseline
  set_env "FEATURE_NEW_CHECKOUT=true"
  wait_for_revision
  call_coordinator /recheck '{}'
}

# Beat E — rollback via /chat. Requires USE_ADK=true on the coordinator
# revision. Flow:
#   1. reset_baseline + then deliberately drift PAYMENT_MODE again (so
#      the ADK has a real drift to act on — a "roll us back" prompt
#      with the env already compliant would be a no-op).
#   2. /chat receives a natural-language operator request that names a
#      specific target revision (see below).
#   3. ADK calls the rollback worker → coordinator returns an
#      approval_url. The operator clicks the URL in a browser; that
#      step is OUT OF SCOPE for this headless runner.
# Expected: action=rollback, approval_url present in the response.
# Fallback: with USE_ADK=false, /chat returns 503 — the script surfaces
# the body so the audience sees it's an env-config issue.
#
# Why BEAT_E_TARGET_REVISION is required:
#   propose_rollback_tool needs a concrete `target_revision` string.
#   The Reader Worker's /read only returns the *active* revision; there
#   is no tool that enumerates revisions. So the operator must discover
#   a previous revision name via gcloud and pass it in via env before
#   running this beat — otherwise the ADK has nothing concrete to act
#   on and will most likely escalate or fail mid-demo.
beat_e() {
  banner "Beat E — combo drift + /chat rollback"
  if [ -z "${BEAT_E_TARGET_REVISION:-}" ]; then
    cat >&2 <<EOF
ERROR: BEAT_E_TARGET_REVISION is not set.

beat-e needs a concrete previous revision name to roll back to. Pick
one from the revisions list before running this beat:

  gcloud run revisions list --service=payment-demo \\
    --project="$PROJECT" --region="$REGION" \\
    --format='value(metadata.name)'

Then export it and re-run:

  export BEAT_E_TARGET_REVISION=<revision-name-from-the-list>
  ./scripts/demo.sh beat-e

See docs/demo-script.md "beat-e" section for the full pre-flight.
EOF
    exit 2
  fi
  echo "Expectation: action=rollback with approval_url (USE_ADK=true)."
  echo "             OR 503 'ADK not enabled' (USE_ADK=false)."
  echo "  target revision: ${BEAT_E_TARGET_REVISION}"
  reset_baseline
  set_env "PAYMENT_MODE=live"
  wait_for_revision
  call_coordinator /chat \
    "{\"prompt\":\"payment mode drifted. roll us back to revision ${BEAT_E_TARGET_REVISION}.\"}"
}

# --------------------------------------------------------------------------- #
# Upgrade-workload beats (Phase 17.C.6)
#
# All three use /chat with workload="upgrade". The deployed coordinator
# must have USE_ADK=true and UPGRADE_READER_URL + UPGRADE_DOCS_URL set
# (17.E deploy infra). Against an earlier coordinator the /chat call
# returns 503 "workload 'upgrade' is not deployed" — the script surfaces
# that body rather than swallowing it (same pattern as beat-e on
# USE_ADK=false).
#
# Pre-stage requirement: demo/upgrade-target/package.json on `main`
# must be at the demonstration baseline (lodash@4.17.20). Confirm with:
#   git show main:demo/upgrade-target/package.json
# See docs/demo-script.md "Upgrade workload beats" for the full
# pre-flight.
# --------------------------------------------------------------------------- #

# Upgrade-A — discovery / read-only. Asks the agent to enumerate the
# demo target's dependencies and any matched advisories WITHOUT
# proposing action. Exercises the upgrade_read_dependencies tool path
# end-to-end (coordinator → upgrade-reader worker → GitHub Contents
# API + Advisory query).
# Expected: a human-readable summary mentioning lodash@4.17.20 and
# GHSA-35jh-r3h4-6jhm. The LLM may pick `no_op` or describe the
# advisory inline; either is fine — the beat showcases the READ path.
upgrade_a() {
  banner "Upgrade-A — discover dependencies (read-only)"
  echo "Expectation: agent describes lodash@4.17.20 + GHSA-35jh-r3h4-6jhm."
  echo "             No PR is opened, no mutation occurs."
  reset_baseline_upgrade
  call_coordinator /chat \
    '{"prompt":"Read the dependencies in the upgrade demo target. Summarize what you find, including any matched advisories. Do NOT propose any action yet — just report.","workload":"upgrade"}'
}

# Upgrade-B — patch bump → upgrade_pr. Asks the agent to act on the
# known patch-level advisory (lodash 4.17.20 → 4.17.21, CVE-2021-23337).
#
# Expected agent tool sequence:
#   1. upgrade_read_dependencies (confirms the advisory + version)
#   2. search_developer_docs (per the chat prompt's citation rule)
#   3. upgrade_propose_pr (package_name="lodash", target_version="4.17.21",
#      advisory_url="https://github.com/advisories/GHSA-35jh-r3h4-6jhm")
#   4. notify (alert channel)
#
# This beat OPENS A REAL PR. The warning banner makes that loud so the
# operator can decide before hitting Enter. Cleanup steps after the
# demo: close the PR and delete the branch (see docs/demo-script.md).
upgrade_b() {
  banner "Upgrade-B — propose upgrade PR (lodash 4.17.20 -> 4.17.21)"
  echo "Expectation: action=upgrade_pr with a PR URL in the response."
  echo
  echo "  !!! LIVE DEMO WARNING !!!"
  echo "  This beat will open a REAL pull request on ${GITHUB_REPO:-the demo target repo}."
  echo "  Close + delete the PR after the demo. See docs/demo-script.md"
  echo "  ('Upgrade workload beats' -> 'Cleanup after upgrade-b') for the"
  echo "  recommended cleanup sequence."
  echo
  # Confirmation gate: must be re-armed each call. Without CONFIRM_UPGRADE_PR=1
  # the beat refuses to fire — Codex 2026-05-20 follow-up. The echo above
  # is operator-facing, but echoing alone doesn't pause a script; the
  # original implementation would fire the curl immediately on `upgrade-b`
  # or `all-upgrade`. The env-var gate forces an explicit opt-in.
  if [ "${CONFIRM_UPGRADE_PR:-}" != "1" ]; then
    cat >&2 <<'EOF'

ERROR: upgrade-b refuses to fire without explicit confirmation.

This beat opens a REAL pull request. To run it, re-invoke with:

  CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh upgrade-b

Or for the bundled all-upgrade target:

  CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh all-upgrade

The gate is required EACH time so an operator can't accidentally
re-fire the PR-creating beat from shell history.
EOF
    return 2
  fi
  reset_baseline_upgrade
  call_coordinator /chat \
    '{"prompt":"lodash 4.17.20 in demo/upgrade-target has CVE-2021-23337 (GHSA-35jh-r3h4-6jhm, prototype pollution). Please propose an upgrade PR to 4.17.21, citing the advisory in the PR body. Then notify the alert channel that the PR is open.","workload":"upgrade"}'
}

# Upgrade-C — major-bump → escalation. Demonstrates the layered safety
# property: the chat system prompt instructs the LLM that major
# bumps must route to `escalation` rather than `upgrade_pr`, AND the
# upgrade-docs worker's post-LLM validator independently refuses any
# bump that isn't patch- or minor-level (returns 403).
#
# Two valid outcomes (both are good teaching moments — note both in
# the runbook):
#   1. LLM follows the prompt: calls notify_tool with channel=alert,
#      severity=high, escalation body. Does NOT call upgrade_propose_pr.
#   2. LLM nevertheless tries upgrade_propose_pr with a major bump:
#      worker validator returns 403 and the agent surfaces the
#      refusal. This proves the post-LLM validator is the real safety
#      gate, not the prompt.
upgrade_c() {
  banner "Upgrade-C — major-bump escalation (layered safety)"
  echo "Expectation: action=escalation via notify_tool (alert channel)."
  echo "             OR worker 403 if the LLM attempts upgrade_propose_pr"
  echo "             with a major bump (validator refuses; either"
  echo "             outcome demonstrates layered safety)."
  reset_baseline_upgrade
  call_coordinator /chat \
    '{"prompt":"Hypothetical: a critical advisory affects lodash 4.x and the fix is only in lodash 5.x (a major version bump). The validator refuses major bumps. Please escalate via notify_tool (channel=alert, severity=high) instead of attempting an upgrade PR — explain the major-version constraint in the message body.","workload":"upgrade"}'
}

# Cleanup — restore the baseline declared in demo/ops-contract.yaml.
# Idempotent: unset_env swallows the "not currently set" error so
# running cleanup twice in a row is fine. Useful as a pre-flight step
# before the live demo.
#
# Note: cleanup is drift-only. The upgrade workload's baseline is the
# repo state of demo/upgrade-target/package.json on `main` — restoring
# that is a git operation, not a Cloud Run env reset, so it is out of
# scope for this script. See docs/demo-script.md for the manual
# cleanup steps after upgrade-b.
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

Drift beats (mutate ${TARGET_SERVICE} Cloud Run env):
  beat-a    baseline check (expect no_op)
  beat-b    flip PAYMENT_MODE=live (expect drift_issue)
  beat-c    flip NEW_THING=test (expect docs_pr or escalate)
  beat-d    flip FEATURE_NEW_CHECKOUT=true (expect docs_pr)
  beat-e    combo drift + /chat rollback (expect rollback w/ approval URL)

Upgrade beats (read GitHub state; require coordinator USE_ADK=true
and UPGRADE_READER_URL + UPGRADE_DOCS_URL set — 17.E deploy infra):
  upgrade-a discover dependencies in demo/upgrade-target (read-only)
  upgrade-b propose upgrade PR (lodash 4.17.20 -> 4.17.21).
            !! WILL OPEN A REAL PR ON ${GITHUB_REPO} !!
  upgrade-c major-bump escalation (layered safety; no PR opened)

Lifecycle:
  cleanup   restore baseline env on ${TARGET_SERVICE} (drift only)
  all       run beat-a..beat-e then cleanup, sequentially
  all-upgrade   run upgrade-a..upgrade-c (separate from \`all\` so a
                drift-only run does not accidentally open an upgrade PR)

Environment:
  PROJECT          GCP project (required)
  REGION           Cloud Run region (default: asia-northeast1)
  TARGET_SERVICE   drift target (default: payment-demo)
  GITHUB_REPO      repo for the upgrade-b warning banner
                   (default: adi-prasetyo/driftscribe)

Currently resolved:
  coordinator: $COORD_URL
  target:      $TARGET_SERVICE (region=$REGION)
  github_repo: $GITHUB_REPO (upgrade beats display only)
EOF
}

case "${1:-}" in
  beat-a)      beat_a ;;
  beat-b)      beat_b ;;
  beat-c)      beat_c ;;
  beat-d)      beat_d ;;
  beat-e)      beat_e ;;
  upgrade-a)   upgrade_a ;;
  upgrade-b)   upgrade_b ;;
  upgrade-c)   upgrade_c ;;
  cleanup)     cleanup ;;
  all)
    # `all` is for dry-runs and recording the demo end-to-end. Each
    # beat is independent; if one fails the rest still run.
    #
    # Intentionally drift-only — upgrade-b opens a real GitHub PR and
    # must not be fired by accident from a plain `all` run. Use
    # `all-upgrade` to exercise the upgrade beats end-to-end.
    beat_a
    beat_b
    beat_c
    beat_d
    beat_e
    cleanup
    ;;
  all-upgrade)
    # Same shape as `all` but for the upgrade workload. Sequential so
    # the operator can read each beat's output before the next fires;
    # upgrade-b opens a real PR so any failure earlier is loud.
    upgrade_a
    upgrade_b
    upgrade_c
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
