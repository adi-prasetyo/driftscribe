# DriftScribe IAM matrix

> **Status:** Phase 11.8 — multi-agent deploy + bootstrap automation. The coordinator runs under a dedicated `driftscribe-agent@…` SA (NOT the default compute SA) with its own per-secret bindings on `coordinator-shared-token`, `github-pat`, and `gemini-api-key`. The bootstrap script (`infra/scripts/setup_secrets.sh`) creates all 5 SAs, applies per-SA IAM (including the resource-scoped rollback grant on `payment-demo` only), and grants `run.invoker` to the coordinator on each worker after the first build. The operator runbook is [`docs/runbooks/deploy.md`](../runbooks/deploy.md).

This document is the source of truth for **what each service account can do, and what it explicitly cannot do**. The "negative-space" column is load-bearing — it's not enough to enumerate the grants; reviewers and judges should be able to run `gcloud projects get-iam-policy driftscribe-hack-2026 --format=json` and verify nothing beyond the listed bindings is present.

## Matrix

| Service Account | Cloud Run service it backs | IAM bindings (positive) | Negative-space (explicit non-grants) | Phase |
| --- | --- | --- | --- | --- |
| `driftscribe-agent@…` (coordinator) | `driftscribe-agent` | `roles/run.invoker` on each worker service (per-service binding, not project-wide); `roles/secretmanager.secretAccessor` on the *specific named secrets* the coordinator needs (`coordinator-shared-token`, `github-pat`, `gemini-api-key`); `roles/datastore.user` (project-wide — accepted constraint; Firestore doesn't offer collection-scope IAM, and the coordinator owns `approvals/` and `sessions/` writes — specifically the `pending → denied` flip on the approvals collection, see Phase 11.7 design notes). **Note:** the coordinator does NOT hold `approval-hmac-key` — only the rollback worker does, which is what makes the approve/deny authority split meaningful (a compromised coordinator can refuse executions but cannot mint them). | **NOT** `roles/run.developer` (cannot deploy/modify Cloud Run — Phase 11.7 delegated this entirely to the rollback worker, resource-scoped to `payment-demo`); **NOT** `roles/run.viewer` at project scope (Phase 11.7 delegated to the reader worker); **NOT** `roles/secretmanager.secretAccessor` at project scope (only the named secrets above); **NOT** `roles/iam.serviceAccountTokenCreator` (cannot impersonate any other SA); **NOT** GitHub admin scope (the coordinator's read-only PAT is scoped to PR list/read on the demo repo; the docs worker holds a *separate* fine-grained PAT for `Contents: write` + `Pull requests: write`); **NOT** the rollback HMAC key | 8 (initial); IAM trimmed in 11.7 |
| `reader-agent-sa@…` | `driftscribe-reader` | `roles/run.viewer` on the project (lets it call Cloud Run admin to read service env + revision lists) | **NOT** `roles/run.developer` (cannot deploy or modify any service); **NOT** `roles/iam.serviceAccountTokenCreator`; **NOT** any Secret Manager access; **NOT** any GitHub credentials | 11.3 |
| `docs-agent-sa@…` | `driftscribe-docs` | `roles/secretmanager.secretAccessor` on **one** secret only: `docs-agent-github-pat` (per-secret binding); `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy) | **NOT** any project-level GCP role; **NOT** able to read or write Cloud Run state; **NOT** Firestore; **NOT** any other secret; **NOT** any GCP-level GitHub credential. The GitHub PAT injected as `GITHUB_TOKEN` is fine-grained: `Contents: Read & write` + `Pull requests: Read & write` on `adi-prasetyo/driftscribe` *only* — no org admin, no other repos, no account-level scopes. The worker's Layer 2 policy additionally hardcodes the target repo via env (caller cannot override), enforces a `^demo/docs/[^/]+\.md$` path allowlist with `normpath`-based traversal defense, refuses hidden files, requires the branch to start with `driftscribe/`, and refuses any base other than `main` | 11.4 |
| `rollback-agent-sa@…` | `driftscribe-rollback` | `roles/run.developer` **on `payment-demo` service only** via a resource-scoped binding (`gcloud run services add-iam-policy-binding payment-demo --member=… --role=roles/run.developer`); `roles/datastore.user` (project-wide — same [acknowledged constraint](#acknowledged-constraints) as coordinator: Firestore lacks collection-scope IAM); `roles/secretmanager.secretAccessor` on `approval-hmac-key` only; `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy). The worker's Layer 2 policy hardcodes `TARGET_SERVICE=payment-demo` via env, refuses any caller-supplied service field (`extra="forbid"` schema), refuses rollback targets that aren't in the service's revision list, and refuses rollback targets equal to the currently-active revision. Layer 4 enforces a single-use HMAC-bound approval token with a 15-min TTL and transactional `pending→used` flip — see [`driftscribe_lib/approvals.py`](../../driftscribe_lib/approvals.py) | **NOT** project-wide `roles/run.developer` (cannot touch the coordinator, the reader, the docs worker, or itself); **NOT** any other Cloud Run service (resource-scoped binding only on `payment-demo`); **NOT** any other Secret Manager secret; **NOT** GitHub access of any kind; **NOT** `roles/iam.serviceAccountTokenCreator` | 11.5 |
| `notifier-agent-sa@…` | `driftscribe-notifier` | `roles/secretmanager.secretAccessor` on **one** secret only: `driftscribe-webhook-url` (per-secret binding); `roles/run.invoker` granted *to the coordinator on this service* (per-service binding, set after first deploy). The worker's Layer 2 policy hardcodes the outbound URL via Secret-Manager-injected env (`NOTIFY_WEBHOOK_URL`), refuses any caller-supplied `url` field via `extra="forbid"`, constrains `channel` to `info\|alert\|approval` and `severity` to `low\|medium\|high\|critical` via `Literal`, and caps `body` at 10000 chars. Fail-closed: empty / missing webhook URL at boot raises `RuntimeError` and the revision fails to come up | **NOT** any project-level GCP role grants whatsoever. **NOT** `roles/run.viewer` (cannot read Cloud Run state); **NOT** `roles/run.developer` (cannot deploy or modify any service); **NOT** `roles/datastore.user` (cannot read or write Firestore); **NOT** any other Secret Manager secret (per-secret binding scoped to `driftscribe-webhook-url` only); **NOT** any GitHub credentials; **NOT** `roles/iam.serviceAccountTokenCreator`. The notifier's "capability" *is* the webhook URL — knowing the URL is the entire authorization model | 11.6 |

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
- **Eventarc-to-Cloud-Run auth uses a Google-signed ID token verified by Cloud Run's own IAM check**, not by application code. That path bypasses Layer A (the operator token) and Layer B (audience-bound caller email allowlist) because Cloud Run validates Eventarc's identity before our `/eventarc` handler runs. This is by design — Eventarc is a trusted internal source.

## Phase 11.7 IAM trim

The coordinator rewrite (Phase 11.7) is the point at which the legacy direct-GCP and direct-GitHub mutation surfaces on `driftscribe-agent@…` are *operationally* removed. The plan only adds the new per-worker `roles/run.invoker` bindings; the removals of older project-wide grants are done by hand via the commands below, because a `gcloud builds submit` cannot itself remove bindings it didn't add.

**Phase 11.8 automation:** The per-worker `roles/run.invoker` grants and the rollback worker's resource-scoped `roles/run.developer` on `payment-demo` are now applied by `infra/scripts/setup_secrets.sh` automatically — gated on each service's existence, so the script is safe to run before the first build (the grants are no-ops) and re-run after the first build (the grants apply). The `roles/run.viewer` *removal* on the coordinator is deferred — see the inline comment in `setup_secrets.sh`.

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
#    NOTE: only run these AFTER the multi-agent path is verified
#    end-to-end via Phase 11.8's smoke test. The Phase 11.7 commit only
#    adds the worker-routed code paths; the legacy /recheck flow still
#    has a fallback (USE_ADK=false) that calls read_live_env directly,
#    and may need roles/run.viewer temporarily.
gcloud projects remove-iam-policy-binding ${PROJECT} \
  --member=serviceAccount:driftscribe-agent@${PROJECT}.iam.gserviceaccount.com \
  --role=roles/run.viewer || true

# (No other project-wide GCP-mutation roles to remove — the coordinator
#  never had roles/run.developer in the first place; that was always
#  delegated to the rollback worker.)
```

**Critical:** do NOT remove `roles/datastore.user` from the coordinator. The coordinator still owns the `pending → denied` flip on the approvals collection (Phase 11.7 design) and writes to the `sessions/` collection for the state store. Removing this grant would break both /recheck idempotency and the approval reject path.

## Cross-references

- Multi-agent topology and auth layers: [`multi-agent-design.md`](./multi-agent-design.md)
- Implementation plan: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`
- Cloud Run inter-service auth proof: `spikes/cloud_run_auth/README.md`
