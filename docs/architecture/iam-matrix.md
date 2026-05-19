# DriftScribe IAM matrix

> **Status:** Phase 14.5 — multi-agent deploy + bootstrap automation, with Vertex AI ADC for the coordinator's LLM auth. The coordinator runs under a dedicated `driftscribe-agent@…` SA (NOT the default compute SA) with its own per-secret bindings on `coordinator-shared-token` and `github-pat`. The bootstrap script (`infra/scripts/setup_secrets.sh`) creates all 5 SAs, applies per-SA IAM (including the resource-scoped rollback grant on `payment-demo` only and the project-wide `roles/aiplatform.user` on the coordinator for Vertex AI), and grants `run.invoker` to the coordinator on each worker after the first build. The operator runbook is [`docs/runbooks/deploy.md`](../runbooks/deploy.md).

This document is the source of truth for **what each service account can do, and what it explicitly cannot do**. The "negative-space" column is load-bearing — it's not enough to enumerate the grants; reviewers and judges should be able to run `gcloud projects get-iam-policy driftscribe-hack-2026 --format=json` and verify nothing beyond the listed bindings is present.

## Matrix

| Service Account | Cloud Run service it backs | Workload scope | IAM bindings (positive) | Negative-space (explicit non-grants) | Phase |
| --- | --- | --- | --- | --- | --- |
| `driftscribe-agent@…` (coordinator) | `driftscribe-agent` | all (dispatcher; currently only drift workers exist, upgrade workers land in Phase 17.C) | `roles/run.invoker` on each worker service (per-service binding, not project-wide); `roles/secretmanager.secretAccessor` on the *specific named secrets* the coordinator needs (`coordinator-shared-token`, `github-pat`); `roles/datastore.user` (project-wide — accepted constraint; Firestore doesn't offer collection-scope IAM, the coordinator writes to `sessions/` for the state store and READS `approvals/` to render the operator-facing approval page — the `pending → denied` flip itself moved to the rollback worker in Phase 11.9, see [the carry-over note](#phase-119-carry-overs-from-codex-11-7-review)); `roles/aiplatform.user` (project-wide — Phase 14.5; required so the coordinator's runtime ADC can call Vertex AI's `generate-content` endpoint for `gemini-2.5-flash`, the model the ADK path drives). **Note:** the coordinator does NOT hold `approval-hmac-key` — only the rollback worker does, which is what makes the approve/deny authority split meaningful (a compromised coordinator can refuse executions but cannot mint OR silently deny them after Phase 11.9). After Phase 14.5 the coordinator additionally holds **NO external LLM API key** of any kind — Gemini auth flows entirely through Vertex AI ADC bound to this SA, so leaked-key revocation is a no-op (there is no key to leak). | **NOT** `roles/run.developer` (cannot deploy/modify Cloud Run — Phase 11.7 delegated this entirely to the rollback worker, resource-scoped to `payment-demo`); **NOT** `roles/run.viewer` (Phase 13 removed this — both `/recheck` paths now route live-state reads through the Reader Worker, so the coordinator cannot read any service's env directly); **NOT** `roles/secretmanager.secretAccessor` at project scope (only the named secrets above); **NOT** `roles/iam.serviceAccountTokenCreator` (cannot impersonate any other SA); **NOT** GitHub admin scope. The coordinator's `github-pat` MUST be a **read-only fine-grained PAT** scoped to `adi-prasetyo/driftscribe` with only `Pull requests: Read` — feeding `search_recent_prs_tool` (read-only PR metadata). The docs worker holds a *separate* fine-grained PAT for `Contents: write` + `Pull requests: write`. If the operator deployed prior to the Phase 11.9 runbook update (`docs/runbooks/deploy.md`) with a classic PAT, the negative-space claim "no GitHub write capability" is weakened until rotation — Codex review of 11.7 surfaced this gap. **NOT** the rollback HMAC key. **NOT** an AI Studio Gemini API key (Phase 14.5 — removed from `--set-secrets` and from `Settings`; the `gemini-api-key` Secret Manager resource is orphaned post-14.5 and operator-deleted manually). **NOT** `roles/run.invoker` on any service outside the four current drift workers (reader, docs, rollback, notifier). The coordinator's `roles/run.invoker` grants are per-service, not project-wide, and do NOT extend across workloads — when Phase 17.C lands `upgrade-reader-sa` and `upgrade-docs-sa` backing `driftscribe-upgrade-reader` and `driftscribe-upgrade-docs`, the coordinator must be granted `roles/run.invoker` on those services separately (handled by Phase 17.D's `setup_secrets.sh` extension and tracked in Phase 17.D.5's matrix update). Until that grant exists, any call from the coordinator into an upgrade worker will 403 at the Cloud Run admission layer regardless of whether the application-layer workload routing (`agent/workloads/registry.py::load_workload('upgrade')`) is wired up; the boot-time validation in that registry enforces the corresponding env-var requirement at the application layer, so a missing IAM grant manifests as a missing env-var failure long before the 403 path is reachable. | 8 (initial); IAM trimmed in 11.7; further doc-corrections in 11.9; 13 (run.viewer removed); 14.5 (Vertex AI ADC replaces AI Studio API key) |
| `reader-agent-sa@…` | `driftscribe-reader` | drift | `roles/run.viewer` on the project (lets it call Cloud Run admin to read service env + revision lists) | **NOT** `roles/run.developer` (cannot deploy or modify any service); **NOT** `roles/iam.serviceAccountTokenCreator`; **NOT** any Secret Manager access; **NOT** any GitHub credentials | 11.3 |
| `docs-agent-sa@…` | `driftscribe-docs` | drift | `roles/secretmanager.secretAccessor` on **one** secret only: `docs-agent-github-pat` (per-secret binding); `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy) | **NOT** any project-level GCP role; **NOT** able to read or write Cloud Run state; **NOT** Firestore; **NOT** any other secret; **NOT** any GCP-level GitHub credential. The GitHub PAT injected as `GITHUB_TOKEN` is fine-grained: `Contents: Read & write` + `Pull requests: Read & write` on `adi-prasetyo/driftscribe` *only* — no org admin, no other repos, no account-level scopes. The worker's Layer 2 policy additionally hardcodes the target repo via env (caller cannot override), enforces a `^demo/docs/[^/]+\.md$` path allowlist with `normpath`-based traversal defense, refuses hidden files, requires the branch to start with `driftscribe/`, and refuses any base other than `main` | 11.4 |
| `rollback-agent-sa@…` | `driftscribe-rollback` | drift | `roles/run.developer` **on `payment-demo` service only** via a resource-scoped binding (`gcloud run services add-iam-policy-binding payment-demo --member=… --role=roles/run.developer`); `roles/datastore.user` (project-wide — same [acknowledged constraint](#acknowledged-constraints) as coordinator: Firestore lacks collection-scope IAM); `roles/secretmanager.secretAccessor` on `approval-hmac-key` only; `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy). The worker's Layer 2 policy hardcodes `TARGET_SERVICE=payment-demo` via env, refuses any caller-supplied service field (`extra="forbid"` schema), refuses rollback targets that aren't in the service's revision list, and refuses rollback targets equal to the currently-active revision. Layer 4 enforces a single-use HMAC-bound approval token with a 15-min TTL and transactional `pending→used` flip — see [`driftscribe_lib/approvals.py`](../../driftscribe_lib/approvals.py) | **NOT** project-wide `roles/run.developer` (cannot touch the coordinator, the reader, the docs worker, or itself); **NOT** any other Cloud Run service (resource-scoped binding only on `payment-demo`); **NOT** any other Secret Manager secret; **NOT** GitHub access of any kind; **NOT** `roles/iam.serviceAccountTokenCreator` | 11.5 |
| `notifier-agent-sa@…` | `driftscribe-notifier` | drift | `roles/secretmanager.secretAccessor` on **one** secret only: `driftscribe-webhook-url` (per-secret binding); `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy). The worker's Layer 2 policy hardcodes the outbound URL via Secret-Manager-injected env (`NOTIFY_WEBHOOK_URL`), refuses any caller-supplied `url` field via `extra="forbid"`, constrains `channel` to `info\|alert\|approval` and `severity` to `low\|medium\|high\|critical` via `Literal`, and caps `body` at 10000 chars. Fail-closed: empty / missing webhook URL at boot raises `RuntimeError` and the revision fails to come up | **NOT** any project-level GCP role grants whatsoever. **NOT** `roles/run.viewer` (cannot read Cloud Run state); **NOT** `roles/run.developer` (cannot deploy or modify any service); **NOT** `roles/datastore.user` (cannot read or write Firestore); **NOT** any other Secret Manager secret (per-secret binding scoped to `driftscribe-webhook-url` only); **NOT** any GitHub credentials; **NOT** `roles/iam.serviceAccountTokenCreator`. The notifier's "capability" *is* the webhook URL — knowing the URL is the entire authorization model | 11.6 |
| `eventarc-trigger-sa@…` | *(not backing a Cloud Run service — Eventarc-internal identity)* | drift (trigger filter is hardcoded to `payment-demo`) | `roles/run.invoker` on the `driftscribe-agent` service (per-service binding) — required for Eventarc to validate the trigger destination at create time. The application path doesn't depend on the IAM check (the coordinator is `--allow-unauthenticated` at the Cloud Run admission layer); enforcement happens in app code, which verifies the bearer ID token's `email` claim equals this SA's address per `docs/architecture/eventarc-payload.md` §"Filtering at the handler". `roles/eventarc.eventReceiver` project-wide — required by Eventarc to deliver events to a destination using this SA's identity. The trigger filter narrows to `serviceName=run.googleapis.com` + `methodName=google.cloud.run.v2.Services.UpdateService` (or v1 `ReplaceService` if the env emits that — operator confirms in deploy runbook Step 7) + `resourceName=projects/<P>/locations/asia-northeast1/services/payment-demo` (exact match). The handler additionally guards on `service_name == "payment-demo"` and `location == "asia-northeast1"` from the audit-log body, so even a broadened trigger filter cannot bypass the in-app service whitelist | **NOT** `roles/run.developer` or any other Cloud Run admin role (cannot read, deploy, or mutate any service — it can only deliver events to `driftscribe-agent`); **NOT** any other Cloud Run service in `run.invoker` (per-service binding is on `driftscribe-agent` only); **NOT** any Secret Manager access; **NOT** `roles/datastore.user` (cannot read or write Firestore directly — the `/eventarc` handler's identity is the *coordinator* SA, not this one); **NOT** `roles/iam.serviceAccountTokenCreator`; **NOT** any GitHub credentials. The SA's effective capability is precisely "receive Eventarc events and deliver them to `/eventarc` on the coordinator"; everything Layer-2 downstream of that runs as the coordinator's SA | 14.3 |

## Why negative space matters

A traditional IAM doc says "here's what each SA can do" and stops. That's how you end up with a service account that ostensibly only needs to read one secret but also happens to have `roles/owner` because someone bound it during a debugging session and forgot to undo it. The negative-space column makes the audit binary: either the policy matches the matrix, or it doesn't.

For the demo, the judge-facing artifact is `infra/scripts/audit_iam.sh` (Phase 11.8, TODO), which:

1. Lists every binding for every DriftScribe SA via `gcloud projects get-iam-policy` and `gcloud run services get-iam-policy`.
2. Diffs against the matrix above.
3. Exits non-zero on any unexpected binding (both extra bindings on the listed SAs and bindings to any non-listed principal that touches our resources).

That script is the architectural enforcement; this document is its specification.

## Per-secret IAM examples

The matrix above invokes "per-secret binding" repeatedly. The exact gcloud invocation is:

```bash
gcloud secrets add-iam-policy-binding coordinator-shared-token \
  --project=driftscribe-hack-2026 \
  --member=serviceAccount:driftscribe-agent@driftscribe-hack-2026.iam.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor
```

> **Phase ordering note:** the `coordinator-shared-token` secret resource AND this binding to the coordinator SA must both exist *before* the Phase 11.1 deploy can succeed — Cloud Run validates `--set-secrets=DRIFTSCRIBE_TOKEN=coordinator-shared-token:latest` at deploy time and rejects the revision with `INVALID_ARGUMENT` if the resource is missing or unreadable by the runtime SA. The other per-worker bindings in this matrix (reader/docs/rollback/notifier) get added incrementally during Phase 11.3–11.6 and are not a prerequisite for the 11.1 deploy.

The crucial property: **no SA has `roles/secretmanager.secretAccessor` at the project level.** Each grant is scoped to a single secret resource, so a future "let me just read another secret real quick" attempt fails closed.

## Resource-scoped Cloud Run example

The rollback SA's most powerful grant is `roles/run.developer` on `payment-demo` only:

```bash
gcloud run services add-iam-policy-binding payment-demo \
  --project=driftscribe-hack-2026 \
  --region=asia-northeast1 \
  --member=serviceAccount:rollback-agent-sa@driftscribe-hack-2026.iam.gserviceaccount.com \
  --role=roles/run.developer
```

The same SA does **not** appear in the project-level IAM policy for `roles/run.developer`. There is no application code path through which the worker would attempt to roll back another service — `TARGET_SERVICE` is sourced from env at boot and the request schema (`ProposeRequest` / `ExecuteRequest` with `extra="forbid"`) rejects any caller-supplied service field, verified by `workers/rollback/tests/test_rollback.py::test_propose_rejects_extra_field`. The IAM scoping is the defense-in-depth backstop: even if a bug in the worker's policy check were exploited to swap the service name, Cloud Run's admin API would return 403 because the SA lacks `roles/run.developer` on any service other than `payment-demo`.

## Acknowledged constraints

- **Firestore lacks collection-scope IAM.** Both the coordinator and the rollback worker need `roles/datastore.user` at project scope. We accept this and rely on the application's transactional writes (which only touch `approvals/` and `sessions/`) plus the Layer 0 tool registry (the coordinator's ADK agent has no general-purpose Firestore tool) to bound the actual blast radius.
- **Eventarc-to-Cloud-Run auth is verified by the `/eventarc` application handler**, not by Cloud Run's IAM check. The coordinator runs `--allow-unauthenticated` at the Cloud Run admission layer (Phase 11.1, so /chat and /recheck can take the operator-token guard at Layer A), which means Cloud Run forwards the Eventarc-minted ID token without ever IAM-checking it. The handler at `agent/main.py` `/eventarc` verifies the Google-signed bearer via `google.oauth2.id_token.verify_oauth2_token` against `EVENTARC_AUDIENCE` (the coordinator's own URL, stamped by the post-deploy step in `infra/cloudbuild.yaml`) and rejects anything whose `email` claim is not `eventarc-trigger-sa@<project>.iam.gserviceaccount.com`. The `roles/run.invoker` grant on the trigger SA exists because Eventarc validates the SA at trigger-create time, not because it gates the request path. This intentionally bypasses Layer A (the operator token) and Layer B (audience-bound caller-email allowlist for worker-call auth) because the audit-log source IS the authorization — the SA email claim and Google's signature on the ID token together are what tie the event back to Eventarc.

## Phase 11.7 IAM trim

The coordinator rewrite (Phase 11.7) is the point at which the legacy direct-GCP and direct-GitHub mutation surfaces on `driftscribe-agent@…` are *operationally* removed. The plan only adds the new per-worker `roles/run.invoker` bindings; the removals of older project-wide grants are done by hand via the commands below, because a `gcloud builds submit` cannot itself remove bindings it didn't add.

**Phase 11.8 automation:** The per-worker `roles/run.invoker` grants and the rollback worker's resource-scoped `roles/run.developer` on `payment-demo` are now applied by `infra/scripts/setup_secrets.sh` automatically — gated on each service's existence, so the script is safe to run before the first build (the grants are no-ops) and re-run after the first build (the grants apply).

**Phase 13 automation:** The `roles/run.viewer` *removal* on the coordinator is also done by `setup_secrets.sh` automatically — the script no longer adds the grant on fresh deploys AND issues an idempotent `remove-iam-policy-binding ... || true` so any pre-Phase-13 deploy cleans up on re-run. The gcloud command in step 2 below is kept as a manual-fallback reference but is no longer the recommended path.

**Idempotency note:** these are `|| true`-suffixed in production runbooks so re-running them on an already-trimmed deployment is a no-op. The negative-space audit script in `infra/scripts/audit_iam.sh` (Phase 11.9+, TODO) is the canonical check that the trim has actually happened.

```bash
PROJECT=driftscribe-hack-2026
REGION=asia-northeast1

# 1. Grant the coordinator roles/run.invoker on each of the four worker
#    services (per-service binding, not project-wide). After 11.7 every
#    coordinator → worker call mints an audience-bound ID token; the
#    receiving worker's IAM check is what allows the call to land. The
#    workers also enforce in-app email allowlists (Layer 3), so this is
#    belt-and-suspenders.
for worker in driftscribe-reader driftscribe-docs driftscribe-rollback driftscribe-notifier; do
  gcloud run services add-iam-policy-binding ${worker} \
    --project=${PROJECT} \
    --region=${REGION} \
    --member=serviceAccount:driftscribe-agent@${PROJECT}.iam.gserviceaccount.com \
    --role=roles/run.invoker
done

# 2. Remove legacy project-wide grants the coordinator no longer needs.
#    All four delegations are now in place — these bindings would just
#    be unused privilege.
#
#    NOTE (Phase 13): this is now done by setup_secrets.sh automatically
#    — the script no longer adds run.viewer to the coordinator AND issues
#    a `|| true`-suffixed remove-iam-policy-binding so pre-Phase-13
#    deploys clean up on re-run. The command below is kept as a manual
#    fallback for operators who can't (or won't) re-run setup_secrets.sh.
gcloud projects remove-iam-policy-binding ${PROJECT} \
  --member=serviceAccount:driftscribe-agent@${PROJECT}.iam.gserviceaccount.com \
  --role=roles/run.viewer || true

# (No other project-wide GCP-mutation roles to remove — the coordinator
#  never had roles/run.developer in the first place; that was always
#  delegated to the rollback worker.)
```

**Critical:** do NOT remove `roles/datastore.user` from the coordinator. The coordinator still owns the `pending → denied` flip on the approvals collection (Phase 11.7 design) and writes to the `sessions/` collection for the state store. Removing this grant would break both /recheck idempotency and the approval reject path.

## Phase 11.9 carry-overs from Codex 11.7 review

Codex review of Phase 11 surfaced two Layer 1 weaknesses that the 11.9
follow-up commit chose to document rather than close immediately. The
first (`roles/run.viewer` on the coordinator) was closed in Phase 13;
the second remains open:

- **`github-pat` blast radius depends on operator hygiene.** The
  coordinator's `search_recent_prs_tool` only ever calls GitHub's PR
  list/read APIs, so a properly-scoped fine-grained PAT (`Pull
  requests: Read`, single repo) has the same effective capability as a
  no-write IAM grant would. But Secret Manager does not enforce the
  PAT's scope — if the operator stores a classic PAT in the
  `github-pat` secret, the coordinator effectively has GitHub write
  capability even though the application code never exercises it. The
  Phase 11.9 deploy runbook (`docs/runbooks/deploy.md`) now requires a
  fine-grained PAT; Phase 13 should follow up with an audit script or
  startup-time scope check.

The application-level Layer 0 (tool registry, see
[`multi-agent-design.md`](./multi-agent-design.md) §4) means the LLM
cannot exercise the remaining `github-pat` weakness through normal
control flow — the only tools that can read Cloud Run or call GitHub
at all are `read_live_env_tool` (delegates to Reader worker) and
`search_recent_prs_tool` (read-only PR list). The Layer 1 weakening
is what would manifest if the coordinator's own code were exploited
or if the SA were directly impersonated.

## Cross-references

- Multi-agent topology and auth layers: [`multi-agent-design.md`](./multi-agent-design.md)
- Implementation plan: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`
- Cloud Run inter-service auth proof: `spikes/cloud_run_auth/README.md`
