# DriftScribe demo script (English)

> Japanese version: `docs/demo-script.ja.md` (Task 16.3 — TBD).

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
