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

## Step 1 — bootstrap APIs, SAs, IAM, secrets (without optional secrets)

```bash
PROJECT=driftscribe-hack-2026
GH_PAT=github_pat_xxx    # FINE-GRAINED PAT (see below)
GEMINI_KEY=AIza...       # from https://aistudio.google.com

./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$GEMINI_KEY"
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
- Enable required APIs.
- Create the Artifact Registry repo.
- Create 5 service accounts: `driftscribe-agent` (coordinator) +
  `reader-agent-sa` + `docs-agent-sa` + `rollback-agent-sa` + `notifier-agent-sa`.
- Apply per-SA IAM (Firestore + Run viewer for the coordinator; Run viewer
  for the reader; project Datastore for rollback; no project-level grants
  for docs or notifier).
- Auto-generate two secrets on first run only: `coordinator-shared-token`
  (the `X-DriftScribe-Token` header value) and `approval-hmac-key`.
- Bind per-secret accessors so each worker reads only its own secret.
- Skip the Docs Agent PAT secret + the notifier webhook secret (you'll
  supply those in step 4).
- Skip the per-worker `run.invoker` grants because workers don't exist
  yet — the script logs "service not deployed yet" and continues.

**Save the operator token** printed in this step. You'll need it to call
`/chat` and `/recheck` later.

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

## Step 4 — re-run the bootstrap with the optional args

```bash
DOCS_PAT=github_pat_xxx                       # from step 2
WEBHOOK_URL=https://webhook.site/<uuid>       # from step 3

./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$GEMINI_KEY" "$DOCS_PAT" "$WEBHOOK_URL"
```

This creates `docs-agent-github-pat` and `driftscribe-webhook-url`, binds
the per-secret accessors to the Docs and Notifier SAs respectively, and
leaves everything else untouched. `coordinator-shared-token` and
`approval-hmac-key` are detected as already-existing and NOT regenerated
(regenerating them would invalidate every running revision).

## Step 5 — first Cloud Build

```bash
gcloud builds submit \
  --project="$PROJECT" \
  --config=infra/cloudbuild.yaml \
  --substitutions=_TAG="$(git rev-parse --short HEAD)" \
  .
```

This builds + pushes all 6 images, deploys all 6 services (`payment-demo`,
`driftscribe-agent`, and the 4 workers), and runs the URL-sync post-deploy
steps that wire each worker's `OWN_URL` and the coordinator's four
`*_URL` env vars to the actual Cloud Run-assigned URLs.

The coordinator deploys with `USE_ADK=false` by default — `/chat` returns
"ADK disabled" until you flip it in step 7.

## Step 6 — apply per-worker `run.invoker` grants

The Cloud Build can't grant `roles/run.invoker` to the coordinator on the
workers because the workers don't exist until that build's deploy steps
finish. Re-run the bootstrap so it picks up the now-existing services and
applies the per-worker bindings:

```bash
./infra/scripts/setup_secrets.sh "$PROJECT" "$GH_PAT" "$GEMINI_KEY" "$DOCS_PAT" "$WEBHOOK_URL"
```

Look for these lines in the output:

```
driftscribe-agent: granted run.invoker on driftscribe-reader
driftscribe-agent: granted run.invoker on driftscribe-docs
driftscribe-agent: granted run.invoker on driftscribe-rollback
driftscribe-agent: granted run.invoker on driftscribe-notifier
rollback-agent-sa: granted run.developer on payment-demo (resource-scoped)
```

If any worker still says "not deployed yet", that worker's deploy step in
step 5 failed — check the Cloud Build logs before continuing.

## Step 7 — enable the ADK delegation path

Before flipping `USE_ADK=true`, confirm your Gemini API key has credit
remaining at https://aistudio.google.com. The coordinator falls back to a
deterministic classifier when `USE_ADK=false`; the ADK path is what
demonstrates the multi-agent delegation.

```bash
gcloud run services update driftscribe-agent \
  --project="$PROJECT" --region=asia-northeast1 \
  --update-env-vars=USE_ADK=true
```

## Step 8 — run the E2E smoke test

```bash
PROJECT="$PROJECT" ./infra/scripts/e2e_smoke.sh
```

The default mode runs the three negative tests:
1. `/recheck` without the token → 401
2. `/read` on the reader without an ID token → 401
3. `/read` on the reader with a user ID token (wrong audience) → 401/403

To also exercise the positive `/chat` path AND the prompt-injection probe
(which need Gemini credit), set `RUN_POSITIVE=1`:

```bash
RUN_POSITIVE=1 PROJECT="$PROJECT" ./infra/scripts/e2e_smoke.sh
```

A clean run prints `PASS: <n>    FAIL: 0` and exits 0.

## Step 9 — record the demo

With the smoke test green, hit `$COORD_URL/recheck?force=true` and
`/chat` with the token, screen-record the approval flow, and you have
the artifact for the submission.

## Re-deploying after a code change

Steps 5 only — re-run `gcloud builds submit`. IAM and secrets don't need
re-bootstrapping unless you've added a new SA, secret, or service.

## Troubleshooting

- **Revision fails with `INVALID_ARGUMENT: Secret not found`**: a
  `--set-secrets` reference points at a secret that doesn't exist yet.
  Re-run `setup_secrets.sh` with all five args.
- **Worker returns 401 on every call from the coordinator**: the
  worker's `OWN_URL` env doesn't match what the coordinator uses as the
  audience. Look at the post-deploy `gcloud run services update` step
  log in the build; if it logged "Could not resolve … URL" the step
  failed and the placeholder is still in place.
- **Worker returns 403 on every call from the coordinator**: the
  coordinator's SA email is missing from the worker's `ALLOWED_CALLERS`
  env. Confirm `cloudbuild.yaml` lists `driftscribe-agent@…` (the
  dedicated coordinator SA, NOT the default compute SA).
- **`/chat` returns 503 "ADK disabled"**: you forgot step 7.
- **`/chat` returns 503 "auth not configured: DRIFTSCRIBE_TOKEN unset"**:
  the `coordinator-shared-token` secret reference was removed from the
  coordinator's `--set-secrets` line — fail-closed by design. Restore the
  reference and redeploy.

## Cross-references

- IAM matrix (per-SA grants + negative space): [`../architecture/iam-matrix.md`](../architecture/iam-matrix.md)
- Multi-agent topology: [`../architecture/multi-agent-design.md`](../architecture/multi-agent-design.md)
- Phase 11 plan: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`
