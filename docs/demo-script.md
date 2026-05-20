# DriftScribe demo script (English)

> Japanese version: [`docs/demo-script.ja.md`](demo-script.ja.md).

Operator runbook for the live ~90s hackathon demo. Audience is a single
hackathon judge watching the screen recording. The driver of the demo
runs the commands; this file is the keyboard-side cheat-sheet.

The runner: `scripts/demo.sh` (Phase 16.2). All beats POST to the
deployed coordinator at `driftscribe-agent` and print the
`X-Trace-Id` from the response so you can chase the log trail in
Cloud Logging.

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
./scripts/demo.sh upgrade-b
```

**Warning: this opens a REAL pull request on `$GITHUB_REPO`** (default
`adi-prasetyo/driftscribe`). The script prints a prominent warning
banner before the curl; the operator should read it before hitting
Enter on a real demo recording.

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
./scripts/demo.sh upgrade-a   # discovery; sets the stage
./scripts/demo.sh upgrade-b   # the climax — real PR opens
./scripts/demo.sh upgrade-c   # the safety story — validator refuses major
```

Or fire them as a batch (no `cleanup` step — upgrade has no Cloud Run
env to reset):

```bash
./scripts/demo.sh all-upgrade
```

`all-upgrade` is intentionally separate from `all` so a drift-only
recording doesn't accidentally open an upgrade PR.

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
