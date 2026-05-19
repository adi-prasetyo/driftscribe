# DriftScribe IAM matrix

> **Status:** Phase 11.3 — coordinator SA (`driftscribe-agent`, Phase 8) exists; the reader worker (`reader-agent-sa`, this phase) is provisioned via the operator command block in the 11.3 commit message. The remaining three worker SAs (docs/rollback/notifier) are still TODO and listed here so the target state is reviewable now.

This document is the source of truth for **what each service account can do, and what it explicitly cannot do**. The "negative-space" column is load-bearing — it's not enough to enumerate the grants; reviewers and judges should be able to run `gcloud projects get-iam-policy driftscribe-hack-2026 --format=json` and verify nothing beyond the listed bindings is present.

## Matrix

| Service Account | Cloud Run service it backs | IAM bindings (positive) | Negative-space (explicit non-grants) | Phase |
| --- | --- | --- | --- | --- |
| `driftscribe-agent@…` (coordinator) | `driftscribe-agent` | `roles/run.invoker` on each worker service (per-service binding, not project-wide); `roles/secretmanager.secretAccessor` on the *specific named secrets* the coordinator needs (`coordinator-shared-token`, `github-pat`, `gemini-api-key`, future `approval-hmac-key`); `roles/datastore.user` (project-wide — accepted constraint; Firestore doesn't offer collection-scope IAM, and the coordinator owns `approvals/` and `sessions/`) | **NOT** `roles/run.developer` (cannot deploy/modify Cloud Run); **NOT** `roles/run.viewer` at project scope (cannot enumerate other services); **NOT** `roles/secretmanager.secretAccessor` at project scope (only the named secrets above); **NOT** `roles/iam.serviceAccountTokenCreator` (cannot impersonate any other SA); **NOT** GitHub admin scope (uses fine-grained PAT scoped to the demo repo) | 8 (already deployed); IAM trimmed in 11.7 |
| `reader-agent-sa@…` | `driftscribe-reader` | `roles/run.viewer` on the project (lets it call Cloud Run admin to read service env + revision lists) | **NOT** `roles/run.developer` (cannot deploy or modify any service); **NOT** `roles/iam.serviceAccountTokenCreator`; **NOT** any Secret Manager access; **NOT** any GitHub credentials | 11.3 |
| `docs-agent-sa@…` (TODO) | `driftscribe-docs` | `roles/secretmanager.secretAccessor` on **one** secret only: `docs-agent-github-pat` (per-secret binding) | **NOT** any other GCP role beyond the default; specifically **NOT** able to read or write Cloud Run state, Firestore, or any other secret. The GitHub PAT itself is fine-grained: `Contents: Read & write` + `Pull requests: Read & write` on `adi-prasetyo/driftscribe` only — no org admin, no other repos | 11.4 |
| `rollback-agent-sa@…` (TODO) | `driftscribe-rollback` | `roles/run.developer` **on `payment-demo` service only** via a resource-scoped binding (`gcloud run services add-iam-policy-binding payment-demo --member=… --role=roles/run.developer`); `roles/datastore.user` (project-wide — same constraint as coordinator); `roles/secretmanager.secretAccessor` on `approval-hmac-key` only | **NOT** project-wide `roles/run.developer` (cannot touch the coordinator, the reader, the docs worker, or itself); **NOT** any other Cloud Run service; **NOT** any other Secret Manager secret; **NOT** GitHub access of any kind | 11.5 |
| `notifier-agent-sa@…` (TODO) | `driftscribe-notifier` | `roles/secretmanager.secretAccessor` on **one** secret only: `driftscribe-webhook-url` (per-secret binding) | **NOT** any other GCP role; **NOT** able to read or write Cloud Run state, Firestore, GitHub, or any other secret. The notifier's "capability" *is* the webhook URL — knowing the URL is the entire authorization model | 11.6 |

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

The same SA does **not** appear in the project-level IAM policy for `roles/run.developer`. Attempting to roll back any other service (e.g., the coordinator itself) returns 403 from Cloud Run's admin API — verified by `workers/rollback/tests/test_rollback.py::test_cannot_target_other_services` (Phase 11.5, TODO).

## Acknowledged constraints

- **Firestore lacks collection-scope IAM.** Both the coordinator and the rollback worker need `roles/datastore.user` at project scope. We accept this and rely on the application's transactional writes (which only touch `approvals/` and `sessions/`) plus the Layer 0 tool registry (the coordinator's ADK agent has no general-purpose Firestore tool) to bound the actual blast radius.
- **Eventarc-to-Cloud-Run auth uses a Google-signed ID token verified by Cloud Run's own IAM check**, not by application code. That path bypasses Layer A (the operator token) and Layer B (audience-bound caller email allowlist) because Cloud Run validates Eventarc's identity before our `/eventarc` handler runs. This is by design — Eventarc is a trusted internal source.

## Cross-references

- Multi-agent topology and auth layers: [`multi-agent-design.md`](./multi-agent-design.md)
- Implementation plan: `docs/plans/2026-05-19-driftscribe-v3-multi-agent.md`
- Cloud Run inter-service auth proof: `spikes/cloud_run_auth/README.md`
