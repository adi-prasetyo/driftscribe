# DriftScribe deploy runbook (Phase 11.8)

End-to-end operator runbook for bringing the multi-agent fleet up on a fresh
GCP project. Every step is idempotent — if you re-run a step nothing breaks.

If you're standing up an existing project, jump to step 5; the earlier steps
are bootstrap-only.

## Prerequisites

- `gcloud` authenticated as a Project Owner of the target GCP project.
- `python3` on PATH (the bootstrap script uses `secrets.token_urlsafe` to
  mint the operator token + HMAC key).
- A clean `git` working tree at the commit you want to deploy.

> **Local-dev ADC (Phase 14.5):** running the coordinator locally with
> `USE_ADK=true` needs Application Default Credentials. Run
> `gcloud auth application-default login` once on your workstation; the
> `google-genai` SDK picks the credentials up via
> `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` +
> `GOOGLE_CLOUD_LOCATION`. In production, Cloud Run injects ADC from the
> `driftscribe-agent` service account automatically — no extra step.

## Step 1 — bootstrap APIs, SAs, IAM, secrets (without optional secrets)

```bash
PROJECT=driftscribe-hack-2026
GH_PAT=github_pat_xxx    # FINE-GRAINED PAT (see below)

./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT"
```

> **IMPORTANT (Phase 11.9 / Codex review of 11.7):** `GH_PAT` MUST be a
> **fine-grained** PAT, NOT a classic PAT. The coordinator's
> `search_recent_prs_tool` only reads PR metadata, so the scope is small
> and a leaked classic PAT (`repo` scope = write everywhere) would
> meaningfully weaken the Layer 1 IAM claim in
> [`../architecture/iam-matrix.md`](../architecture/iam-matrix.md). Create
> it at https://github.com/settings/personal-access-tokens/new with:
>
> - **Repository access:** Only select repositories →
>   `adi-prasetyo/driftscribe`
> - **Repository permissions:**
>   - `Pull requests: Read-only` (read-only — coordinator NEVER writes
>     PRs; the docs worker holds a separate fine-grained PAT for that)
>   - Nothing else
>
> If you have already deployed with a classic PAT, rotate to a
> fine-grained one before doing any demo or making the deployment
> public — the IAM matrix's coordinator-row negative-space claim is
> only literally true once this is done.

The script will:
- Enable required APIs, including `developerknowledge.googleapis.com`
  (Phase 17.B — backs the Developer Knowledge MCP server) and run
  `gcloud beta services mcp enable developerknowledge.googleapis.com`
  explicitly. The latter is auto-enabled by Google after 2025-03-17 when
  the underlying API is enabled, but the explicit call keeps fresh-project
  bootstrap deterministic.
- Create the Artifact Registry repo.
- Create 5 service accounts: `driftscribe-agent` (coordinator) +
  `reader-agent-sa` + `docs-agent-sa` + `rollback-agent-sa` + `notifier-agent-sa`.
- Apply per-SA IAM (Firestore + Run viewer for the coordinator; Run viewer
  for the reader; project Datastore for rollback; no project-level grants
  for docs or notifier).
- Auto-generate two secrets on first run only: `coordinator-shared-token`
  (the `X-DriftScribe-Token` header value) and `approval-hmac-key`.
- Bind per-secret accessors so each worker reads only its own secret
  (Phase 17.B: the coordinator also gets `secretAccessor` on
  `developer-knowledge-api-key`, scoped to that single secret — no
  project-wide grant).
- Skip the Docs Agent PAT secret, the notifier webhook secret, and the
  Developer Knowledge API key secret (you'll supply those in step 4).
- Skip the per-worker `run.invoker` grants because workers don't exist
  yet — the script logs "service not deployed yet" and continues.

**Save the operator token** printed in this step. You'll need it to call
`/chat` and `/recheck` later.

## Step 1b — verify the `_Default` log-bucket retention extension

`setup_secrets.sh` extends Cloud Logging's `_Default` bucket retention
from 30 days to 365 days. This holds every DriftScribe log line —
including the thought-summary, tool-call, and LLM-usage events from
Phase 18.B — for a full year. Storage beyond the first 30 days is
billed at $0.01/GiB-month; hackathon volume is well under 1 GiB/month.

Verify:

```bash
gcloud logging buckets describe _Default \
  --project=$PROJECT \
  --location=global \
  --format='value(retentionDays)'
```

Expected: `365`. If the value is still `30`, re-run `setup_secrets.sh`.

Querying example for thought-summary + tool-call replay (after Phase 18.B
is also deployed):

```text
resource.type="cloud_run_revision"
resource.labels.service_name="driftscribe-agent"
jsonPayload.event=("llm_thought" OR "tool_call" OR "llm_usage")
jsonPayload.trace_id="<the trace id you want to replay>"
```

Paste into Logs Explorer; sort ascending by timestamp.

## Step 2 — create the fine-grained Docs Agent PAT

The Docs Agent uses a separate, more-restricted PAT than the coordinator:

1. Open https://github.com/settings/personal-access-tokens/new
2. **Token name:** `driftscribe-docs-agent`
3. **Expiration:** 90 days (or per your security policy)
4. **Repository access:** Only select repositories → `adi-prasetyo/driftscribe`
5. **Permissions:**
   - Contents: Read and write
   - Pull requests: Read and write
   - (Nothing else — no org access, no other repos)
6. Generate and copy the token.

The Docs Agent's `TARGET_REPO` env is hardcoded to `adi-prasetyo/driftscribe`
in `cloudbuild.yaml`, and its path allowlist enforces `^demo/docs/[^/]+\.md$`.
Even if this PAT leaked, the blast radius is bounded to that single repo's
contents + PR write.

## Step 3 — create the demo webhook URL

The Notifier Agent posts approval / alert messages to a webhook. For the
demo, use https://webhook.site:

1. Open https://webhook.site
2. Copy "Your unique URL" (looks like `https://webhook.site/<uuid>`).
3. Keep the tab open during the demo so you can show inbound posts live.

For a real deployment, replace this with a Slack incoming webhook or any
HTTPS endpoint you control. The notifier worker's `extra="forbid"` schema
refuses any caller-supplied URL — the only outbound destination is the one
in this secret.

## Step 3b — create the Developer Knowledge API key (Phase 17.B)

The coordinator's ADK agent grounds its reasoning in authoritative Google
docs by calling the Developer Knowledge API via its remote MCP endpoint
(`https://developerknowledge.googleapis.com/mcp`). Auth is a single
`X-Goog-Api-Key` header sourced from Secret Manager.

The API key MUST be restricted to **only** the Developer Knowledge API.
Create it in the Console — the UI enforces the API-restriction selection
inline, while the `gcloud services api-keys create` equivalent is fragile
across gcloud versions:

1. Open
   `https://console.cloud.google.com/apis/credentials?project=<PROJECT>`
2. **+ Create credentials → API key.** Copy the generated key string
   immediately; you cannot view it later.
3. Click **Edit API key** on the new key.
4. **Name:** `driftscribe-developer-knowledge` (or similar).
5. **API restrictions:** select **Restrict key**, then under "Select APIs"
   pick **only** `Developer Knowledge API`. Do NOT leave it "Don't
   restrict key" — an unrestricted key is far worse than the alternative
   if it ever leaks.
6. **Application restrictions:** leave at `None`. Cloud Run egress IPs are
   not stable, and the coordinator does not run in a browser context, so
   neither IP nor HTTP-referrer restrictions apply cleanly.
7. **Save.**

The Console will show a brief "Key updated" toast; the API restriction
takes effect immediately. If a request slips out with `aiplatform` or any
other API name in the URL while bound to this key, GCP will return
`403 API_KEY_API_RESTRICTED` — defense in depth above the per-secret IAM
binding.

## Step 4 — re-run the bootstrap with the optional args

```bash
DOCS_PAT=github_pat_xxx                       # from step 2
WEBHOOK_URL=https://webhook.site/<uuid>       # from step 3
DK_API_KEY=AIza...                            # from step 3b
# Phase 17.E.2: two additional fine-grained PATs for the upgrade workload
# (same repo as DOCS_PAT, different scopes — see setup_secrets.sh prompt
# for the exact permissions to select):
UPGRADE_READER_PAT=github_pat_xxx             # READ-ONLY (Contents:read + Pull requests:read)
UPGRADE_DOCS_PAT=github_pat_xxx               # READ+WRITE (Contents:read+write + Pull requests:read+write)

./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$DOCS_PAT" "$WEBHOOK_URL" "$DK_API_KEY" "$UPGRADE_READER_PAT" "$UPGRADE_DOCS_PAT"
```

This creates `docs-agent-github-pat`, `driftscribe-webhook-url`,
`developer-knowledge-api-key`, `upgrade-reader-github-pat`, and
`upgrade-docs-github-pat`, binds the per-secret accessors to the Docs,
Notifier, coordinator, upgrade-reader, and upgrade-docs SAs respectively,
and leaves everything else untouched. `coordinator-shared-token` and
`approval-hmac-key` are detected as already-existing and NOT regenerated
(regenerating them would invalidate every running revision).

> **Two-phase note:** if you ran step 1 without the optional args, the
> coordinator SA (`driftscribe-agent@…`) already exists by this point, so
> the per-secret IAM binding on `developer-knowledge-api-key` lands
> immediately. If a SA were somehow missing the script logs a clear hint
> — re-run after the missing SA is created (typically after the first
> `gcloud builds submit`).

## Step 5 — first Cloud Build

```bash
gcloud builds submit \
  --project="$PROJECT" \
  --config=infra/cloudbuild.yaml \
  --substitutions=_TAG="$(git rev-parse --short HEAD)" \
  .
```

This builds + pushes all 8 images, deploys all 8 services (`payment-demo`,
`driftscribe-agent`, the 4 drift workers, and the 2 upgrade workers), and
runs the URL-sync post-deploy steps that wire each worker's `OWN_URL` and
the coordinator's six `*_URL` env vars (READER, DOCS, ROLLBACK, NOTIFIER,
UPGRADE_READER, UPGRADE_DOCS) to the actual Cloud Run-assigned URLs.

The coordinator deploys with `USE_ADK=false` by default — `/chat` returns
"ADK disabled" until you flip it in step 8.

## Step 6 — apply per-worker `run.invoker` grants

The Cloud Build can't grant `roles/run.invoker` to the coordinator on the
workers because the workers don't exist until that build's deploy steps
finish. Re-run the bootstrap so it picks up the now-existing services and
applies the per-worker bindings:

```bash
./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$DOCS_PAT" "$WEBHOOK_URL" "$DK_API_KEY" "$UPGRADE_READER_PAT" "$UPGRADE_DOCS_PAT"
```

Look for these lines in the output:

```
driftscribe-agent: granted run.invoker on driftscribe-reader
driftscribe-agent: granted run.invoker on driftscribe-docs
driftscribe-agent: granted run.invoker on driftscribe-rollback
driftscribe-agent: granted run.invoker on driftscribe-notifier
driftscribe-agent: granted run.invoker on driftscribe-upgrade-reader
driftscribe-agent: granted run.invoker on driftscribe-upgrade-docs
rollback-agent-sa: granted run.developer on payment-demo (resource-scoped)
```

If any worker still says "not deployed yet", that worker's deploy step in
step 5 failed — check the Cloud Build logs before continuing.

## Step 7 — confirm Eventarc trigger fires

The Step 6 re-run of `setup_secrets.sh` also:

- Created `eventarc-trigger-sa@…`.
- Granted it `roles/run.invoker` on `driftscribe-agent` plus
  `roles/eventarc.eventReceiver` project-wide.
- Created the `driftscribe-cloudrun-changes` trigger filtering on
  `payment-demo` mutations (resourceName exact match,
  `methodName=google.cloud.run.v2.Services.UpdateService`).

The Step 5 build also stamps `EVENTARC_AUDIENCE` on the coordinator (the
coordinator's own assigned URL) in the same `gcloud run services update`
call as the four worker URLs — see the final post-deploy step in
`infra/cloudbuild.yaml`. Without that env, the `/eventarc` handler
fail-closes with 503.

Manually mutate `payment-demo` once. Then check both halves of the path:
the audit log emitted the expected method name (trigger-side), AND the
coordinator's Cloud Run logs show `/eventarc` ran `_do_recheck` to
completion (handler-side). The audit-log shape alone only proves the
trigger *would* match — it doesn't prove Eventarc delivered or that the
handler completed.

> **State-store note (DRY_RUN=true demo deploy):** the default
> `cloudbuild.yaml` deploys the coordinator with `DRY_RUN=true`, which
> swaps `FirestoreStateStore` for `InMemoryStateStore`. That means
> decisions produced by `/eventarc` are NOT persisted to Firestore on
> the default demo deploy — verifying via Firestore only works after
> flipping `DRY_RUN=false`. For the default deploy, verify via Cloud
> Run logs as shown below.

```bash
gcloud run services update payment-demo --update-env-vars=DEMO_PROBE=1 --project "$PROJECT" --region asia-northeast1
sleep 10

# 1) Trigger-side: audit log methodName check.
gcloud logging read \
  'resource.type=cloud_run_revision AND protoPayload.methodName=~"Services\."' \
  --limit 1 --format='value(protoPayload.methodName)' \
  --project "$PROJECT"

# 2) Handler-side: /eventarc invocation in coordinator logs (~30s later).
gcloud logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name="driftscribe-agent" AND httpRequest.requestUrl=~"/eventarc"' \
  --limit 5 --format='value(httpRequest.status,httpRequest.requestUrl)' \
  --project "$PROJECT"
```

If the audit log output is `google.cloud.run.v2.Services.UpdateService`
AND the coordinator logs show a `200` on `/eventarc`, you're done.

If the audit log emits `google.cloud.run.v1.Services.ReplaceService`
instead, the v1 path uses a **different resourceName format** as well —
not just the methodName. v1 uses `namespaces/{project}/services/{name}`,
not `projects/{project}/locations/{region}/services/{name}`. Edit BOTH
filter lines in section 10 of `infra/scripts/setup_secrets.sh`:

```diff
- --event-filters="methodName=google.cloud.run.v2.Services.UpdateService"
- --event-filters="resourceName=projects/${PROJECT}/locations/${REGION}/services/payment-demo"
+ --event-filters="methodName=google.cloud.run.v1.Services.ReplaceService"
+ --event-filters="resourceName=namespaces/${PROJECT}/services/payment-demo"
```

Then delete the existing trigger and re-run the script to recreate it
with the new filters:

```bash
gcloud eventarc triggers delete driftscribe-cloudrun-changes \
  --location=asia-northeast1 --project "$PROJECT"
./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$DOCS_PAT" "$WEBHOOK_URL" "$DK_API_KEY" "$UPGRADE_READER_PAT" "$UPGRADE_DOCS_PAT"
```

Re-mutate `payment-demo` and re-check the coordinator logs.

## Step 8 — enable the ADK delegation path

Before flipping `USE_ADK=true`, confirm your project's Vertex AI quota for
`generate-content` on `gemini-2.5-flash` in `asia-northeast1` is healthy
(GCP Console → Vertex AI → Quotas). Phase 14.5 moved Gemini auth from the
AI Studio API key to Vertex AI ADC, so quota is now per-project /
per-region / per-model — separate from any AI Studio credit balance and
shared with all other Vertex AI usage in this project. The coordinator
falls back to a deterministic classifier when `USE_ADK=false`; the ADK
path is what demonstrates the multi-agent delegation.

```bash
gcloud run services update driftscribe-agent \
  --project="$PROJECT" --region=asia-northeast1 \
  --update-env-vars=USE_ADK=true
```

## Step 9 — run the E2E smoke test

```bash
PROJECT="$PROJECT" ./infra/scripts/e2e_smoke.sh
```

The default mode runs the three negative tests:
1. `/recheck` without the token → 401
2. `/read` on the reader without an ID token → 401
3. `/read` on the reader with a user ID token (wrong audience) → 401/403

To also exercise the positive `/chat` path AND the prompt-injection probe
(which consume Vertex AI Gemini quota), set `RUN_POSITIVE=1`:

```bash
RUN_POSITIVE=1 PROJECT="$PROJECT" ./infra/scripts/e2e_smoke.sh
```

A clean run prints `PASS: <n>    FAIL: 0` and exits 0.

## Step 10 — record the demo

With the smoke test green, hit `$COORD_URL/recheck?force=true` and
`/chat` with the token, screen-record the approval flow, and you have
the artifact for the submission.

## Re-deploying after a code change

Steps 5 only — re-run `gcloud builds submit`. IAM and secrets don't need
re-bootstrapping unless you've added a new SA, secret, or service.

## Troubleshooting

- **Revision fails with `INVALID_ARGUMENT: Secret not found`**: a
  `--set-secrets` reference points at a secret that doesn't exist yet.
  Re-run `setup_secrets.sh` with all seven args (PROJECT, GH_PAT,
  DOCS_PAT, WEBHOOK_URL, DK_API_KEY, UPGRADE_READER_PAT,
  UPGRADE_DOCS_PAT).
- **Coordinator logs `MissingDeveloperKnowledgeApiKeyError`**:
  the `--set-secrets=DEVELOPER_KNOWLEDGE_API_KEY=...` reference was
  stripped from the coordinator deploy step, or the secret has no
  versions. Restore the reference in `infra/cloudbuild.yaml`, confirm
  `gcloud secrets versions list developer-knowledge-api-key` shows at
  least one ENABLED version, then redeploy. The exception class is
  defined in `agent/mcp/developer_knowledge.py` and surfaces as 503
  on both `/chat` and `/recheck`.
- **MCP requests return `403 API_KEY_API_RESTRICTED`**: the API key
  restriction was set to a different API (or no API). Re-edit the key
  in the Console and restrict it to **only** the Developer Knowledge API
  (Step 3b). No redeploy needed — Secret Manager value is unchanged.
- **Worker returns 401 on every call from the coordinator**: the
  worker's `OWN_URL` env doesn't match what the coordinator uses as the
  audience. Look at the post-deploy `gcloud run services update` step
  log in the build; if it logged "Could not resolve … URL" the step
  failed and the placeholder is still in place.
- **Worker returns 403 on every call from the coordinator**: the
  coordinator's SA email is missing from the worker's `ALLOWED_CALLERS`
  env. Confirm `cloudbuild.yaml` lists `driftscribe-agent@…` (the
  dedicated coordinator SA, NOT the default compute SA).
- **`/chat` returns 503 "ADK disabled"**: you forgot step 8.
- **`/chat` returns 503 "auth not configured: DRIFTSCRIBE_TOKEN unset"**:
  the `coordinator-shared-token` secret reference was removed from the
  coordinator's `--set-secrets` line — fail-closed by design. Restore the
  reference and redeploy.

## Cross-references

- IAM matrix (per-SA grants + negative space): [`../architecture/iam-matrix.md`](../architecture/iam-matrix.md)
- Multi-agent topology: [`../architecture/multi-agent-design.md`](../architecture/multi-agent-design.md)
- Phase 11 plan: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`
