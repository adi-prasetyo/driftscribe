# Demo Health Monitor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A scheduled synthetic probe of the anonymous judge path through `driftscribe.adp-app.com` that fails loudly (GitHub failure email) whenever the public demo window is silently closed, half-open, or serving broken APIs — during the 7/13–7/24 judging window nobody is watching.

**Architecture:** Three small pieces. (1) `demo-window.sh` gains a credential-free `probe` subcommand reusing its existing `edge_state()` classifier. (2) A new `infra/cloudflare/demo-health-probe.sh` wraps that edge probe plus two deep GET probes of judge-critical APIs (`/infra/graph`, and `/decisions` → `/trace/{id}` — the 2026-07-06 outage class where every `/trace` 503'd while `/` stayed 200), with retries to ride out cold starts. (3) A new `.github/workflows/demo-health.yml` runs the probe script every 30 minutes with **zero credentials** (no WIF, no secrets, no PAT — pure anonymous curl) and fails the run on any unhealthy state; GitHub's failed-scheduled-run email to the owner is the alert, the same notification contract `demo-reset.yml` already establishes.

**Explicit non-goal — never auto-repair:** window flips are deliberate operator actions (`demo-window.sh on|off`), and a monitor that re-opens the edge would fight the documented mid-window `off` flip used for IaC approvals. The monitor only observes and alerts.

**Tech Stack:** bash + curl + jq, GitHub Actions cron. No Python, no worker/frontend/coordinator changes, no deploy needed — the workflow schedules itself from `main` after merge.

---

## Context an implementer needs (read this first)

- **Why now:** on 2026-07-08 the window sat fully CLOSED (Access login wall for anonymous visitors) for hours after an operator flip, and nothing noticed. Judging is 7/13–7/24; first-round judges are non-expert Findy staff. A silently broken demo = a judge who scores what they see.
- **The judge path:** anonymous browser → Cloudflare Access (needs the `driftscribe-demo-bypass` Everyone-bypass policy) → `driftscribe-proxy` Worker (needs `DEMO_MODE="1"`, injects the operator token for routes on `DEMO_ALLOWLIST` in `infra/cloudflare/worker/src/proxy.js:46-59`) → coordinator origin. Three distinct failure layers, hence the closed / half-open / open classification that already exists in `infra/cloudflare/demo-window.sh:146-164` (`edge_state()`).
- **Existing self-heal:** `.github/workflows/demo-reset.yml` heals demo *data* (payment-demo, lodash fixture, stale adoption PRs, autonomy/pause dial) every 2h/daily — but all its checks go through the **run.app origin URL with the operator token**, so they stay green even when the public hostname is walled off. That is the gap this plan closes.
- **Probe constraints:** GET-only. Never probe `POST /chat` (each run holds long Gemini calls = real money, and the Worker rate-limits it 5/60s/IP). Cache-bust every probe (`?nocache=` + `Cache-Control: no-cache`) — established practice in `demo-window.sh:121-125`.
- **Live JSON shapes (verified 2026-07-08):** `GET /decisions` returns `{"decisions": [...]}`; each row has a `trace_id` field. `GET /trace/{trace_id}` and `GET /infra/graph` are both on `DEMO_ALLOWLIST`.
- **Coordination caveat:** another session is currently working PR #216 (orders-sub) and may run `demo-window.sh` — but should not *edit* it. Rebase before opening the PR if the file changed upstream.
- **Branch from origin/main**, not local main (local is behind): `git fetch origin && git checkout -b feat/demo-health-monitor origin/main`.

---

### Task 1: `demo-window.sh probe` subcommand (credential-free)

**Files:**
- Modify: `infra/cloudflare/demo-window.sh`

The script currently hard-fails at the top (`demo-window.sh:32-34`) when `CLOUDFLARE_DRIFTSCRIBE_API_TOKEN` is unset, but the edge probes (`probe()`, `edge_state()`, `describe_edge()`) never touch the CF API. Move the guard so only CF-API paths need the token, and add `probe`.

**Step 1: Reproduce the failing behavior (this is the "failing test")**

```bash
env -u CLOUDFLARE_DRIFTSCRIBE_API_TOKEN bash infra/cloudflare/demo-window.sh probe
```

Expected: exits 2 with `usage: ... <on|off|status>` (subcommand doesn't exist yet). After the change it must classify the edge with no token.

**Step 2: Update the header comment (subcommand list + env-var note)**

In the header block (`demo-window.sh:4-7`), add a `probe` line to the subcommand list:

```
#   demo-window.sh probe   anonymous edge probe only, exit 0 iff fully OPEN
#                          (no CF API token needed — used by demo-health.yml)
```

and amend the "Required env var" note (line ~21) to say the token is required **for on/off/status only**; `probe` runs credential-free.

**Step 3: Move the token guard into `load_policies`**

Replace lines 32–34:

```bash
: "${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN:?load it first: set -a; source .env; set +a}"

AUTH=(-H "Authorization: Bearer ${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN}")
JSON=(-H "Content-Type: application/json")
```

with:

```bash
JSON=(-H "Content-Type: application/json")

# The CF API token is only needed by subcommands that read/mutate Access
# policies (on/off/status). `probe` is anonymous-curl only, so it must run
# with no credentials at all (that is what lets demo-health.yml call it
# from a zero-secret scheduled job).
require_cf_token() {
  : "${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN:?load it first: set -a; source .env; set +a}"
  AUTH=(-H "Authorization: Bearer ${CLOUDFLARE_DRIFTSCRIBE_API_TOKEN}")
}
```

Then add `require_cf_token` as the first line of `load_policies()` (before `APP_ID=$(find_app_id)`), so `on`/`off`/`status` keep exactly their current guard behavior. (Bash arrays assigned inside a function without `local` are global — `cf_call`'s `"${AUTH[@]}"` keeps working.)

**Step 4: Add the `probe` subcommand**

After `cmd_status()` (line 274), add:

```bash
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
```

Update the dispatch case and usage:

```bash
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
```

**Step 5: Verify all four paths**

```bash
bash -n infra/cloudflare/demo-window.sh   # syntax gate, expected: silent

# probe needs NO token; exit code mirrors live state:
env -u CLOUDFLARE_DRIFTSCRIBE_API_TOKEN bash infra/cloudflare/demo-window.sh probe; echo "exit=$?"
# window OPEN  -> "OPEN — bypass live AND Worker demo mode injecting..." exit=0
# window CLOSED-> "CLOSED — anonymous / → Access login redirect..."      exit=1

# status must STILL demand the token:
env -u CLOUDFLARE_DRIFTSCRIBE_API_TOKEN bash infra/cloudflare/demo-window.sh status
# expected: "CLOUDFLARE_DRIFTSCRIBE_API_TOKEN: load it first..." nonzero exit

# status with the token must behave exactly as before:
set -a; source .env; set +a
bash infra/cloudflare/demo-window.sh status
# expected: "Bypass policy: ..." + policy list + edge state line
```

**Step 6: Commit**

```bash
git add infra/cloudflare/demo-window.sh
git commit -m "feat(demo): credential-free 'probe' subcommand on demo-window.sh"
```

---

### Task 2: `demo-health-probe.sh` — edge + deep API probes with retries

**Files:**
- Create: `infra/cloudflare/demo-health-probe.sh`

**Step 1: Write the script**

```bash
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
```

**Step 2: Verify locally against the LIVE edge (both outcomes)**

```bash
bash -n infra/cloudflare/demo-health-probe.sh   # expected: silent
chmod +x infra/cloudflare/demo-health-probe.sh

RETRIES=1 bash infra/cloudflare/demo-health-probe.sh; echo "exit=$?"
```

Expected while the window is CLOSED (state at plan time): edge step prints `CLOSED — ...`, script prints `FAIL: edge is not fully OPEN` then `UNHEALTHY after 1 attempts`, `exit=1`.
Expected once the window is OPEN: all three probe blocks pass, `HEALTHY`, `exit=0`. **Run it both ways if the window state changes during implementation — never flip the window just to test; the closed-state run can only be done while it happens to be closed.**

**Step 3: Commit**

```bash
git add infra/cloudflare/demo-health-probe.sh
git commit -m "feat(demo): judge-path health probe script (edge + deep API, retried)"
```

---

### Task 3: `.github/workflows/demo-health.yml` — zero-credential 30-min monitor

**Files:**
- Create: `.github/workflows/demo-health.yml`

A separate workflow file, NOT a new job in `demo-reset.yml`: that workflow's jobs run on **every** trigger (no per-schedule `if:` on `service-reset`/`adopt-pr-sweep`), so adding a `*/30` cron there would run the GCP-credentialed reset jobs 48×/day. A separate file also keeps this workflow at literally zero credentials.

**Step 1: Write the workflow**

```yaml
name: demo-health

# Synthetic monitor for the public judging window (opened 2026-07-07 —
# docs/plans/2026-07-08-demo-health-monitor.md). demo-reset.yml heals demo
# DATA through the run.app origin with the operator token, so it stays
# green even when the public hostname is walled off (which happened on
# 2026-07-08: the window sat CLOSED for hours after an operator flip and
# nothing noticed). This workflow probes the path an anonymous judge
# actually takes — CF Access edge -> demo-proxy Worker -> coordinator —
# and FAILS the run on any unhealthy state. GitHub's failed-scheduled-run
# email to the owner is the alert: the same notification contract
# demo-reset.yml documents. Repeated failure emails while broken are the
# intended urgency during judging week.
#
# NEVER auto-repairs. Window flips are deliberate operator actions
# (infra/cloudflare/demo-window.sh), and the runbook's mid-window `off`
# flip (IaC approvals) must not be fought by a monitor. A deliberate
# short flip that crosses a 30-min tick costs one false-positive email —
# accepted, it doubles as the "you forgot to flip back on" reminder.
#
# ZERO credentials by design: no WIF, no secrets, no PAT — anonymous curl
# is the whole point (we probe what a stranger sees). Nothing here can
# mutate anything, so `schedule`-runs-from-main is the only trust anchor
# needed. When the window closes for good (~7/30), disable this workflow:
# `gh workflow disable demo-health.yml` (see demo-window.sh close checklist).
on:
  schedule:
    # :17/:47, NOT */30 — GitHub's docs single out the top of the hour as
    # the most contended (delayed or even dropped) cron slot, and */30
    # lands half its firings there. demo-reset.yml already rides :00; this
    # monitor deliberately doesn't.
    - cron: '17,47 * * * *'
  workflow_dispatch:

permissions:
  contents: read   # checkout only

concurrency:
  group: demo-health
  cancel-in-progress: true   # probes are stateless; the newest answer wins

jobs:
  judge-path-probe:
    runs-on: ubuntu-latest
    timeout-minutes: 15   # probe worst case ~8 min (3 attempts x ~2 min of curl max-times + 2x40s sleeps + checkout); padded so the script's graceful exit always beats the job ceiling — a killed job would skip the summary step
    steps:
      - name: Checkout
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
        with:
          persist-credentials: false

      - name: Probe the anonymous judge path
        id: probe
        run: |
          set -o pipefail
          if bash infra/cloudflare/demo-health-probe.sh 2>&1 | tee probe.log; then
            echo "healthy=true" >> "$GITHUB_OUTPUT"
          else
            echo "healthy=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Summary + verdict
        env:
          # Step output via env:, never ${{ }} in the run: body (repo
          # convention — see demo-reset.yml's actions-injection comments).
          HEALTHY: ${{ steps.probe.outputs.healthy }}
        run: |
          {
            echo "## demo-health / judge-path-probe — $(date -u +%FT%TZ)"
            echo '````'
            cat probe.log
            echo '````'
            if [ "$HEALTHY" != "true" ]; then
              echo "### :rotating_light: judge path UNHEALTHY"
              echo "Anonymous visitors cannot fully use driftscribe.adp-app.com right now."
              echo ""
              echo "Triage (repo root, \`set -a; source .env; set +a\` first):"
              echo '- `infra/cloudflare/demo-window.sh status` — edge + policy state'
              echo '- CLOSED -> `demo-window.sh on` (it prints the full ordering checklist: dial, DEMO_MODE, then edge)'
              echo '- HALF-OPEN -> redeploy the Worker with DEMO_MODE="1"; the deploy output MUST list CHAT_RATE_LIMIT'
              echo '- edge OPEN but an API probe failed -> coordinator/infra-reader problem: check Cloud Run logs'
              echo ""
              echo "Window deliberately closed for good? Disable this monitor:"
              echo '`gh workflow disable demo-health.yml`'
            fi
          } >> "$GITHUB_STEP_SUMMARY"
          [ "$HEALTHY" = "true" ]
```

**Step 2: Lint-verify the workflow**

```bash
# actionlint if available, else at minimum a YAML parse:
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/demo-health.yml'))" && echo YAML-OK
```

Expected: `YAML-OK`.

**Step 3: Commit**

```bash
git add .github/workflows/demo-health.yml
git commit -m "feat(demo): demo-health.yml — 30-min zero-credential judge-path monitor"
```

---

### Task 4: Close-runbook updates (the monitor must die with the window)

**Files:**
- Modify: `infra/cloudflare/demo-window.sh` (the `close_checklist()` heredoc, currently lines 185–192)
- Modify: `docs/plans/2026-07-07-demo-daily-reset-and-notice.md` (wherever it lists `gh workflow disable demo-reset.yml` in the close steps — grep for `demo-reset.yml`)
- Modify: `docs/plans/2026-06-12-hackathon-judge-readiness-design.md` (~line 256 — the older CONSOLIDATED flip runbook also lists close steps and would go stale; Codex review catch)

**Step 1: Extend `close_checklist()`**

```bash
close_checklist() {
  cat <<EOF
Window-CLOSE ordering (reverse of open):
  1. this script: demo-window.sh off                     <- edge gate, FIRST
  2. worker/wrangler.toml DEMO_MODE="0" + wrangler deploy
  3. operator: restore the autonomy dial if desired
  4. judging-window automation: gh workflow disable demo-health.yml
     (else it emails a failure every 30 min forever) + demo-reset.yml
EOF
}
```

**Step 2: Mirror the same line in BOTH plan docs' close runbooks**

Find the close-window step lists in `docs/plans/2026-07-07-demo-daily-reset-and-notice.md` AND `docs/plans/2026-06-12-hackathon-judge-readiness-design.md` (grep each for `demo-reset.yml` / the close checklist) and add `gh workflow disable demo-health.yml` alongside the existing disable step. Keep the wording consistent with the script.

**Step 3: Verify + commit**

```bash
bash -n infra/cloudflare/demo-window.sh
grep -n "demo-health.yml" infra/cloudflare/demo-window.sh \
  docs/plans/2026-07-07-demo-daily-reset-and-notice.md \
  docs/plans/2026-06-12-hackathon-judge-readiness-design.md
# expected: at least one hit in each of the three files
git add infra/cloudflare/demo-window.sh docs/plans/2026-07-07-demo-daily-reset-and-notice.md docs/plans/2026-06-12-hackathon-judge-readiness-design.md
git commit -m "docs(demo): close runbook — disable demo-health.yml with the window"
```

---

### Task 5: PR, merge, live verification

**Step 0: Alert-channel preflight (Codex review finding — do not skip)**

The "failed scheduled run emails the owner" contract has a real precondition: GitHub notifies the user **associated with the cron** (whoever created/last modified the workflow file's schedule, or re-enabled it), and only if that account has Actions notifications enabled. Preflight:

1. The PR must be authored and merged by the operator's own account (`adi-prasetyo`) — true for every PR in this repo, but state it: a bot-merged workflow would email nobody.
2. Operator confirms github.com → Settings → Notifications → Actions is set to email (failed-only is fine).
3. Treat the contract as PROVEN only after a real `schedule`-triggered failure has produced an email — demo-reset.yml's adopt-fixture net has already done this once on this repo, which is why the contract is trusted; if in doubt, the Step 3 red-path dispatch below is `workflow_dispatch`, which does NOT exercise the schedule-notification path — wait for the first scheduled tick instead.

**Step 1: Push and open the PR**

```bash
git push -u origin feat/demo-health-monitor
gh pr create --title "feat(demo): judge-path health monitor (demo-health.yml)" --body "..."
```

Body should state: zero-credential 30-min synthetic probe of the anonymous judge path; alert = failed-run email (demo-reset.yml's existing contract); never auto-repairs; motivated by the 2026-07-08 silent-closure incident.

**Step 2: CI green, then merge.** No deploy: `schedule` triggers always run the workflow file committed to `main`, so merging IS the deployment.

**Step 3: Live verification (both verdicts)**

```bash
gh workflow run demo-health.yml
sleep 90 && gh run list --workflow=demo-health.yml --limit 1
```

- If the window is still CLOSED at merge time: expect `failure` — that is the alert path proven live end-to-end (check the run's step summary shows the triage checklist, and that the failure email arrives). Then, after the window is reopened (post-#216 work), dispatch again and expect `success`.
- If the window is already OPEN: expect `success`; the red path was already proven in Task 2's local closed-state run. **Do not flip the window off just to see the workflow fail.**

**Step 4: Confirm the cron self-runs**

Within ~30–40 min of merge: `gh run list --workflow=demo-health.yml --limit 3` shows a `schedule`-triggered run. Done.
