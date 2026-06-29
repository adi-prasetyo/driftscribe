# DriftScribe demo script (English)

> Japanese version: [`docs/demo-script.ja.md`](demo-script.ja.md).

Operator runbook for the live ~90s hackathon demo. Audience is a single
hackathon judge watching the screen recording. The driver of the demo
runs the commands; this file is the keyboard-side cheat-sheet.

The runner: `scripts/demo.sh` (Phase 16.2). All beats POST to the
deployed coordinator at `driftscribe-agent` and print the
`X-Trace-Id` from the response so you can chase the log trail in
Cloud Logging.

**The story to tell.** DriftScribe is one small crew running a stewardship
loop around a cloud estate: Provision stands infrastructure up, Anchor guards
what's live on its own, Patch keeps it current, and Explore explains it. The
scripted beats below walk two of those crews (Anchor's drift detection, Patch's
upgrades); Provision and Explore are shown interactively from chat. The
through-line for the judge: you provision once, then Anchor keeps watch for drift.

## Pre-flight (run 5 minutes before recording)

```bash
export PROJECT=driftscribe-hack-2026
export REGION=asia-northeast1

# 1. Confirm coordinator is up and reachable.
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'

# 2. Confirm USE_ADK=true on the current revision (beats c, e need this).
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -o 'USE_ADK=[a-z]*'

# 3. Open the architecture diagram in a browser tab.
#    File: docs/architecture/architecture.html
#    Tip: open it locally (file://) so you can scroll/zoom without
#    network jitter on the recording.

# 4. Restore baseline env on payment-demo. Idempotent.
./scripts/demo.sh cleanup

# 5. Sanity check — beat-a should print action=no_op.
./scripts/demo.sh beat-a

# 6. Operator token works. (call_coordinator inside the script
#    already exercised it on step 5; no separate check needed.)

# 7. Pick a target revision for beat-e and export it.
#    Beat-e fails fast without this; see "HITL flow during beat-e".
gcloud run revisions list --service=payment-demo \
  --project="$PROJECT" --region="$REGION" \
  --format='value(metadata.name)'
export BEAT_E_TARGET_REVISION=<pick-a-previous-revision-from-the-list>
```

If any of those steps fail, fix the underlying issue before recording.
The live demo is not the right time to debug coordinator deployment.

> **Before recording:** trigger the manual-dispatch E2E workflow and wait for green.
> `gh workflow run e2e.yml` then `gh run watch`. Requires reviewer approval on
> the `e2e` GitHub Environment. The E2E run is the fail-fast signal that the
> demo path is intact — running it before each recording catches IAM / MCP /
> worker-boundary regressions that would otherwise surface on camera.

## Screen layout

```
+-------------------------------+-------------------------------+
|                               |                               |
|  Terminal (~80x24)            |  Browser: architecture.html   |
|                               |                               |
|  $ ./scripts/demo.sh beat-a   |  [diagram 1: Reader path]    |
|  ...                          |                               |
|                               |                               |
+-------------------------------+-------------------------------+
```

- Left half: terminal at ~80×24, font scaled so the audience can read
  decision JSON without squinting (recommended: 16pt).
- Right half: `docs/architecture/architecture.html` in a browser. Pre-
  scroll to the top diagram so beat-A's narration matches what's
  on-screen.
- Optional third tab (out of frame, switch as needed): Cloud Logging
  filtered to `resource.labels.service_name="driftscribe-agent"` —
  for the trace-ID payoff at the end.

## Timing (target: 90s)

Approximately 12–15s per beat. Hit Enter at the top of each row, then
let the response render while narrating.

| t (s) | Terminal action                       | Browser action                    | Narration                                                                                          |
| ----- | ------------------------------------- | --------------------------------- | -------------------------------------------------------------------------------------------------- |
| 0:00  | `./scripts/demo.sh beat-a`            | scroll to "Read" path             | "DriftScribe watches a live Cloud Run service against an ops-contract. Baseline check returns no_op." |
| 0:12  | `./scripts/demo.sh beat-b`            | hover the Drift-Issue worker box  | "We flip PAYMENT_MODE to live. That variable is locked by contract — so the agent files a drift issue." |
| 0:25  | `./scripts/demo.sh beat-c`            | hover the ADK reasoning box       | "Now an unknown variable. The ADK reasoning agent decides whether to write docs or escalate."      |
| 0:42  | `./scripts/demo.sh beat-d`            | hover the Docs worker box         | "Operator-toggleable variable. Agent proposes a docs PR with the new value preview."               |
| 0:58  | `./scripts/demo.sh beat-e`            | hover the Rollback worker + HITL  | "Combo: real drift plus a natural-language rollback request. Agent returns an approval URL — HITL." |
| 1:15  | click the approval URL → Approve      | bring the approval page to front  | "Operator clicks Approve. Rollback worker executes the revision pin. Drift resolved."              |
| 1:25  | `./scripts/demo.sh cleanup`           | back to architecture diagram      | "Cleanup restores baseline. Every beat surfaced an X-Trace-Id for the audit trail."                |

If you go over time, drop beat-c — it's the most expendable. Beat-e
is the climax (HITL + rollback) and beat-b is the clearest example of
contract enforcement; keep both.

## Per-beat expected output

Each beat first resets baseline (`PAYMENT_MODE=mock`,
`FEATURE_NEW_CHECKOUT=false`, removes any stray `NEW_THING`), then
applies its own drift, so beats are independent and re-runnable in any
order. The `reset_baseline` helper in `scripts/demo.sh` is idempotent
and is called at the top of beat-b, beat-c, beat-d, beat-e. (Beat-a
doesn't mutate env and so doesn't need it; `cleanup` *is* the reset.)

The script does not assert these — eyeball them against the terminal.

**beat-a** (baseline):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "no_op",
  "trigger": "manual_recheck",
  ...
}
```

**beat-b** (PAYMENT_MODE drift, allow_manual_change=false):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "drift_issue",
  "target_var": "PAYMENT_MODE",
  "github": { "url": "https://github.com/.../issues/...", ... },
  ...
}
```

**beat-c** (unknown var):
- With USE_ADK=true: `action` is `docs_pr` or `escalate` (ADK call).
- With USE_ADK=false: `action` is `escalate` (classifier default).

**beat-d** (FEATURE_NEW_CHECKOUT drift, allow_manual_change=true):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "docs_pr",
  "target_var": "FEATURE_NEW_CHECKOUT",
  "target_docs_file": "demo/docs/runbook.md",
  "github": { "url": "https://github.com/.../pull/...", ... },
  ...
}
```

**beat-e** (rollback via /chat, USE_ADK=true):
```
<- HTTP 200  X-Trace-Id: <uuid>
{
  "action": "rollback",
  "approval_url": "https://driftscribe-agent-.../approval/<id>?t=...",
  ...
}
```
With USE_ADK=false beat-e returns:
```
<- HTTP 503  X-Trace-Id: <uuid>
{ "detail": "ADK not enabled (set USE_ADK=true to enable /chat)" }
```

## Upgrade workload beats

Phase 17.C.6 adds three beats that exercise the `upgrade` workload via
`/chat workload=upgrade`. Unlike drift beats, these do NOT mutate the
`payment-demo` Cloud Run env — the upgrade workers read
`demo/upgrade-target/package.json` from GitHub via the Contents API,
so the "baseline" is the repo state on `main`, not a Cloud Run env.

### Pre-flight (upgrade beats)

```bash
# 1. Coordinator must have USE_ADK=true (same requirement as beat-c/e).
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -oE 'USE_ADK=[a-z]+'

# 2. Coordinator must have UPGRADE_READER_URL + UPGRADE_DOCS_URL set
#    (17.E deploy infra wires these). Until 17.E ships, the upgrade
#    beats return HTTP 503 with body
#    `{"detail":"workload 'upgrade' is not deployed: ..."}` — which is
#    itself a reasonable demonstration of the Phase 17.A.3 workload
#    pre-resolve guard. The script surfaces that body rather than
#    swallowing it.
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -oE 'UPGRADE_(READER|DOCS)_URL=[^,]+'

# 3. demo/upgrade-target/package.json on `main` must be at the
#    demonstration baseline (lodash@4.17.20). The pin is intentional
#    — DO NOT bump it. See demo/upgrade-target/README.md.
git show main:demo/upgrade-target/package.json | grep '"lodash"'
# Expect: "lodash": "4.17.20"
```

### upgrade-a — discover dependencies (read-only)

```bash
./scripts/demo.sh upgrade-a
```

Expected agent tool sequence:
- `upgrade_read_dependencies` (no args; target_repo + lockfile_path
  derived server-side from `UPGRADE_TARGET_REGISTRY["phase17_demo"]`).

Expected response body: free-form text summarizing the dependencies
in `demo/upgrade-target/package.json`, naming `lodash@4.17.20` and the
matched advisory `GHSA-35jh-r3h4-6jhm` (CVE-2021-23337). The prompt
asks the agent NOT to propose action — so no PR is opened, no notify
is fired. Action label in the response should be `no_op` or a
descriptive summary; eyeball it.

Showcase point: the LLM never sees `target_repo` / `lockfile_path` —
those are pinned in `agent/workloads/registry.py::UPGRADE_TARGET_REGISTRY`
(authority lives in code, not YAML, not the prompt).

### upgrade-b — propose upgrade PR (LIVE)

```bash
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh upgrade-b
```

**Warning: this opens a REAL pull request on `$GITHUB_REPO`** (default
`adi-prasetyo/driftscribe`). The script REQUIRES `CONFIRM_UPGRADE_PR=1`
on every invocation — without it the beat refuses to fire and exits
with status 2 + a message explaining how to re-arm. Pasting the
command from shell history alone cannot re-fire the beat unless the
env var is still in the operator's shell, by design.

Expected agent tool sequence:
1. `upgrade_read_dependencies` — confirm the advisory.
2. `search_developer_docs` — per the chat prompt's citation rule
   (`workloads/upgrade/chat_system_prompt.md`).
3. `upgrade_propose_pr` with:
   - `package_name="lodash"`,
   - `target_version="4.17.21"`,
   - `advisory_url="https://github.com/advisories/GHSA-35jh-r3h4-6jhm"`,
   - `body=<prose citing the advisory + the developer-docs result>`.
   The upgrade-docs worker's post-LLM validator (Phase 17.C.3a) checks
   the bump is patch- or minor-level and that the lockfile path is the
   pinned `demo/upgrade-target/package.json`; both pass.
4. `notify` on the alert channel.

Expected response body (free-form text): mentions the PR URL on
`$GITHUB_REPO` and confirms the notify call.

Showcase point: `upgrade_propose_pr` is authority-clean — the LLM
chose ONLY the package name, target version, advisory URL, and body
prose. The repo / lockfile path / branch / base / PR title are
derived server-side. A prompt-injection that tries to redirect the PR
at a different repo cannot succeed because those fields never leave
the registry.

#### Cleanup after upgrade-b

The opened PR is a real GitHub PR. After recording, clean up:

```bash
# 1. Close the PR (replace <N> with the PR number from the agent's response).
gh pr close <N> --delete-branch --repo "$GITHUB_REPO"
```

If `gh` isn't authenticated locally, close + delete the branch via the
GitHub web UI on the PR page. The branch name follows the pattern
`upgrade/<package>-<version-with-dots-as-dashes>` — for upgrade-b
specifically that's `upgrade/lodash-4-17-21` (see
`agent/adk_tools.py::upgrade_propose_pr_tool` for the derivation rule).
Safe to delete without affecting `main`.

Note: re-running `upgrade-b` before closing the PR will collide on
the same branch name (`upgrade/lodash-4-17-21` is deterministic from
`package_name` + `target_version`). The worker surfaces the
PyGithub error in the agent's response — that's a legitimate failure
mode to demonstrate during Q&A if needed, but in normal demo flow
close the prior PR first.

### upgrade-c — major-bump escalation (layered safety)

```bash
./scripts/demo.sh upgrade-c
```

Two valid outcomes — both are good teaching moments:

1. **LLM follows the prompt** (the common path): calls `notify` with
   `channel=alert`, `severity=high`, and an escalation body
   explaining that a major-version bump is required and the validator
   refuses major bumps. Does NOT call `upgrade_propose_pr`. Action
   label `escalation`.
2. **LLM tries `upgrade_propose_pr` anyway**: the upgrade-docs
   worker's post-LLM validator returns 403 with reason
   `"major version bump refused at validator ... agent should have
   routed this to the 'escalation' action"`. (If the LLM passes a
   non-triple like `"5.x"` instead of `5.0.0`, the validator returns
   422 on the unparseable semver before the major-bump rule fires;
   that's still a refusal, just via the schema gate rather than the
   policy gate.) The agent surfaces the refusal in its response.

Both demonstrate the **layered safety property**: even if the prompt
(or a prompt-injection) convinces the LLM to attempt a major bump,
the worker-side validator is the real safety gate. The chat prompt
documents the policy; the worker enforces it.

Showcase point: the validator is a code-side allowlist (patch/minor
only). It cannot be overridden by YAML, by the system prompt, or by
prompt-injection.

### Recording-time variant

If you're recording the demo end-to-end, run upgrade beats in this
order after the drift beats finish:

```bash
./scripts/demo.sh upgrade-a                              # discovery; sets the stage
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh upgrade-b         # the climax — real PR opens
./scripts/demo.sh upgrade-c                              # the safety story — validator refuses major
```

Or fire them as a batch (no `cleanup` step — upgrade has no Cloud Run
env to reset):

```bash
CONFIRM_UPGRADE_PR=1 ./scripts/demo.sh all-upgrade
```

`CONFIRM_UPGRADE_PR=1` is required for `upgrade-b` AND for `all-upgrade`
because `all-upgrade` runs `upgrade-b`. Without it, `upgrade-b` refuses
to fire (exits 2) and the batch halts before opening any PR.

`all-upgrade` is intentionally separate from `all` so a drift-only
recording doesn't accidentally open an upgrade PR.

## Transparency UI walkthrough

Phase 19.B adds an operator-facing reasoning timeline at the
coordinator root `/`. The page surfaces every
`/chat` call's final response immediately, then fills in the three
reasoning groups (Coordinator / Tools & workers / MCP) as Cloud
Logging ingests the events (~15s lag). It also surfaces past
decisions in the right rail and lets you click straight through to
the approval page for any pending rollback.

This section is a standalone walkthrough — it does NOT replace the
beat sequence above. Run it after the recording, or as a separate
Q&A demo for the judge.

### Pre-flight (one-time)

```bash
# 1. Coordinator must be deployed with USE_ADK=true (same requirement
#    as beat-c/e). Plus the runtime SA needs roles/logging.viewer to
#    fetch traces (wired by infra/scripts/setup_secrets.sh).
gcloud run services describe driftscribe-agent \
  --project="$PROJECT" --region="$REGION" \
  --format='value(status.url)'

# 2. You'll need the operator token (same one beats use). Have it on
#    the clipboard before opening the UI.
gcloud secrets versions access latest \
  --secret=driftscribe-operator-token \
  --project="$PROJECT"
```

For local verification without Cloud Run, boot a coordinator with the
stub trace fetcher:

```bash
USE_ADK=false DRIFTSCRIBE_TOKEN=test GCP_PROJECT=test-proj \
  uvicorn agent.main:app --port 8080
# Then open http://localhost:8080/
```

The stub fetcher (`agent/trace_fetcher.py`) returns a synthetic
timeline so you can exercise the rendering paths without GCP creds.
The `Cache-Control: no-store` header is set on every operator
surface (`/`, `/trace`, `/decisions`) — confirm with
`curl -i http://localhost:8080/ | grep -i cache-control`.

### Walkthrough beats

| Step | Action                                                                                      | Expected (≤ timing)                                                                                                       |
| ---- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 1    | Open `https://<coordinator-url>/` in a browser.                                             | Page loads. Token-prompt modal appears (no token in `sessionStorage` yet).                                                |
| 2    | Paste the operator token; click **Save**.                                                   | Modal closes. Header shows `token: set` pill (green).                                                                     |
| 3    | Type `what is the current drift?` in the chat input. Workload dropdown stays on **drift**. Click **Send**. | Within ≤2s: top "Final response" card populates with the agent's reply. Trace-ID pill in the header shows `trace abcd1234…`. |
| 4    | Wait ~15s (Cloud Logging ingestion lag).                                                    | Within ≤30s: the three reasoning groups fill in. Click each `<details>` to expand.                                        |
| 5    | Observe the three groups render with distinct visual treatment.                             | Coordinator reasoning (`llm_thought`, `llm_usage`) — green swatch. Tools & workers (`tool_call` / `tool_result`) — amber swatch. MCP (Developer Knowledge) — purple swatch. The legend at the page bottom labels the swatches. |
| 6    | In the right rail, find a past rollback decision (`action=rollback`). Click **open trace →**. | Page enters historical mode: badge `viewing historical trace <id>` shows at the top, chat form is dimmed, polling stops. Three groups re-render from the historical `/trace` response. |
| 7    | Click the inline **Approve →** button on a rollback row whose `expires_at` is still future. | Browser navigates to `/approvals/{id}?t=…`. This is the existing HITL approval page (unchanged).                          |
| 8    | Click **← new chat** in the historical badge.                                               | Returns to live mode. Chat form re-enables; polling for new traces resumes when you next click Send.                      |

The friendly worker labels (Reader (drift), Notifier, Developer
Knowledge MCP — answer, etc.) are mapped client-side from raw tool
function names — see `_WORKER_LABELS` in `transparency.html`. Add new
entries there when wiring a new tool so the timeline doesn't surface
raw function names like `read_live_env_tool` to the judge.

### Expected timings (acceptance)

- **Final-response card** populates within **≤2s** of clicking Send
  (bounded by the LLM call + the round-trip; the UI does NOT wait on
  Cloud Logging for this).
- **Reasoning groups** fill within **≤30s** of the response landing
  (bounded by Cloud Logging ingestion, typically 10–15s in practice).
- **Past decisions rail** loads within **≤2s** of page load.

If the reasoning groups stay empty past 30s, check that
`roles/logging.viewer` is bound to the coordinator runtime SA (the
sanity-check checklist in the Phase 19 plan calls this out) and that
the trace_id from the response actually appears in Logs Explorer
with `jsonPayload.trace_id="<id>"`.

### Screenshot

To be added by the maintainer once the UI is deployed against a
non-stub coordinator — the subagent that wrote this walkthrough had
no headless-browser tool available. The screenshot should capture:
the final-response card populated, the three reasoning groups
expanded, the trace-ID pill green, and at least one decision row in
the right rail. Save as `docs/submission/transparency-ui.png` and
reference from this section.

## Trace ID lookup

Every response carries an `X-Trace-Id` header (Phase 15.2 middleware).
The script prints it on a dedicated line before the body so you can
copy it into Cloud Logging during Q&A:

```bash
gcloud logging read 'jsonPayload.trace_id="<id>"' \
  --project=$PROJECT --limit=20 --format=json
```

That returns every log line emitted while handling the request — the
coordinator's tool calls, worker requests, the worker's own logs (all
the workers propagate the same trace ID via `X-Trace-Id` in
`agent/worker_client.py`). This is the "audit trail" payoff: one ID,
the whole story.

If you're recording, leave a Cloud Logging tab open in advance and
paste the beat-e trace ID into the filter at the end — it's the
strongest visual hit for "I can audit what the AI did."

## HITL flow during beat-e

beat-e requires the operator to nominate a concrete previous Cloud Run
revision to roll back to. `propose_rollback_tool` takes a
`target_revision` string, and no ADK-callable tool enumerates revisions
— the Reader Worker only returns the *active* one. So the runner
fails-fast if `BEAT_E_TARGET_REVISION` is not set, with a message
pointing here.

Pre-flight (do this once before recording):

```bash
gcloud run revisions list --service=payment-demo \
  --project="$PROJECT" --region="$REGION" \
  --format='value(metadata.name)'
```

Pick a revision name from the list — typically the most-recent one
that pre-dates the current active revision (the goal is "roll back to
the version before the drift was introduced"). Then export it:

```bash
export BEAT_E_TARGET_REVISION=<revision-name-from-the-list>
```

beat-e returns an `approval_url`. The runner CANNOT click it
headlessly — that's the human-in-the-loop point of the design.
During the demo:

1. Run `./scripts/demo.sh beat-e`.
2. In the terminal output, the `approval_url` field is the link.
3. Copy it; paste into the browser (or click it in a terminal that
   recognizes URLs).
4. You land on `agent/templates/approval.html` — a single-page form
   showing "Rollback payment-demo to revision X?" with **Approve** /
   **Reject** buttons.
5. Click **Approve**. The page POSTs back to the coordinator, which
   calls the rollback worker's `/execute` endpoint, which pins the
   target revision.
6. Page re-renders showing the executed result.

The approval token (`?t=...` in the URL) expires after 15 minutes
(see `_cached_rollback_is_expired` in `agent/main.py`). Don't pause
the demo between beat-e and the click.

## Recovery

If a beat hangs or returns 5xx that is NOT one of the expected
intentional failures (i.e. beat-e on USE_ADK=false → 503):

1. **Ctrl-C** the running command.
2. `./scripts/demo.sh cleanup` to restore baseline env on
   payment-demo. (This is safe to run mid-demo; it doesn't touch the
   coordinator.)
3. Retry the failed beat.

If beat-b/c/d return a 502 from the coordinator, the worker behind it
is unhealthy. Check Cloud Run service health for `driftscribe-reader`,
`driftscribe-docs`, `driftscribe-rollback`. The `X-Trace-Id` from the
502 still works for log lookup — that's how you triage.

If the script itself fails to start with `could not resolve
coordinator URL`, gcloud auth has lapsed:

```bash
gcloud auth login
gcloud config set project $PROJECT
```

Then re-run pre-flight.
