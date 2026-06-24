# Multi-tenant productization — scope & path

**Date:** 2026-06-24
**Status:** Findings + scope decision. **Productization is out of scope for the hackathon** (deadline 2026-07-10). This document exists to (a) record an honest map of how single-tenant DriftScribe is today, and (b) name the paths to a product so the scope decision is a considered one, not a gap.

## The question

> Can a different user point DriftScribe at *their own* GitHub account (and, by extension, their own infrastructure)?

Today, no. DriftScribe runs as a **single-tenant appliance** bound to one GitHub identity, one Google Cloud project, and one infrastructure estate. The headline finding: a "GitHub connector" is only ~10–15% of the work that question implies. DriftScribe's value is detecting drift between **GitHub IaC ↔ live Google Cloud infrastructure**, so "connect a different GitHub account" is only meaningful alongside "operate on a different cloud estate." Both sides are hardwired.

## Current state — single-tenant by construction

The coupling is not incidental; several pieces of it are deliberate security properties. Inventory across five layers:

### 1. GitHub identity
- **Five separate PATs**, one per service, each injected as the `GITHUB_TOKEN` env var: `github-pat` (coordinator), `docs-agent-github-pat` (docs), `upgrade-reader-github-pat`, `upgrade-docs-github-pat`, `tofu-editor-github-pat`. Loaded at worker boot from Secret Manager via Cloud Run `--set-secrets`.
- All GitHub calls go through `driftscribe_lib/github.py::get_repo(token, repo)` — initialized as `Github(token)`. **No GitHub App, no OAuth app, no installation tokens.** Purely PAT-based.
- Target repo is hardcoded `adi-prasetyo/driftscribe` (`infra/cloudbuild.yaml` `GITHUB_REPO` default; `agent/workloads/registry.py:675` `IAC_EDITOR_TARGET`; worker `TARGET_REPO`/`IAC_EDITOR_TARGET_REPO` env). Workers re-validate the request-body `target_repo` against their env-pinned constant and 403 on mismatch — a defense-in-depth check, not a per-request switch.
- **One bot identity for everyone.** Commits/PRs/comments are authored by the identity behind the configured token (the operator's account today). No per-user GitHub identity exists anywhere.

### 2. End-user / account identity
- **No user or tenant model exists.** Two credentials are accepted at the operator boundary (`agent/auth.py`): a shared static token `DRIFTSCRIBE_TOKEN` (one org-wide secret), and a Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`). The CF Access email is recorded as an audit `actor`/`approver` on `config/*` and `plan_approvals` documents — it is an audit subject, not an access-control subject.
- No signup, no sessions, no per-user token storage, no per-user rate limiting (the only rate limit is per-IP, in the Cloudflare Worker, on `POST /chat`).

### 3. Google Cloud project
- `GCP_PROJECT` is read as a **module-level constant at worker boot** (`workers/infra_reader/main.py:42`); Cloud Asset Inventory is scoped to `projects/{GCP_PROJECT}` (`:91`). There is no per-request project switching — the `DescribeRequest` schema is deliberately empty (`extra="forbid"`) so a caller cannot redirect scope.
- All service accounts live in the **same project** (`*@$PROJECT.iam.gserviceaccount.com`). Every worker's `ALLOWED_CALLERS` allowlist is the coordinator's intra-project SA email. **No cross-project SA impersonation or Workload Identity Federation for tenants exists anywhere.**
- All secrets live in the one project's Secret Manager. Vertex AI ADC, Firestore, and Cloud Logging are all derived from the single project.
- 11 Cloud Run services are a **single shared deployment** in `asia-northeast1` — no per-tenant isolation.
- The project id `driftscribe-hack-2026` is hardcoded as a literal in ~10 source locations, e.g. `workers/tofu_apply/gcs_fetch.py:33`, `driftscribe_lib/iac_plan_metadata.py:99`, `iac/versions.tf:12`, `iac/variables.tf:3`, `iac/cloudrun.tf:37`, `iac/imports.tf:7`, the `_KMS_KEY` default in `infra/cloudbuild.tofu-apply.yaml`, and the Cloud Build SA in several cloudbuild files. The Cloudflare Access AUD tag and team domain are likewise hardcoded literals identifying the single operator's CF Access application.

### 4. Data & IaC state
- Firestore collections are **flat and global with zero tenant key**: `events`, `decisions`, `config`, `approvals`, `plan_approvals`, `iac_pr_source`. Written/read across `agent/state_store.py`, `driftscribe_lib/approvals.py`, `agent/infra_graph_cache_store.py`, `agent/iac_pr_source_cache.py`.
- OpenTofu state bucket `driftscribe-hack-2026-tofu-state` prefix `prod` is a literal in `iac/versions.tf:14-16`. The artifact bucket name is hardcoded in **two** places (`workers/tofu_apply/gcs_fetch.py:33`, `driftscribe_lib/iac_plan_metadata.py:99`), and the apply worker **fail-closes** on any artifact URI whose bucket name isn't that literal.
- The `tofu-apply` worker **bakes the estate's `iac/` directory into its Docker image** and validates incoming plans against the hash of that baked tree. This image-bound IaC hash is an intentional security gate.

### 5. The estate model
- The managed estate is the triple `(gcp_project, target_service=payment-demo, target_region=asia-northeast1)` plus the drift contract `workloads/drift/contract.yaml` (which names the service + repo). These are deploy-time env vars and config defaults: **one coordinator deployment = one estate.**

## Paths to a product

Three routes, with rough order-of-magnitude effort. Estimates are coarse — they exist to compare paths, not to commit a schedule.

### Path A — Per-tenant isolated deployment (template + automation) — *recommended if productizing*
Keep the app single-tenant **internally**; make it *deployable per customer*. Parameterize the ~10 hardcodes, lift the literal project/bucket/repo/CF-AUD values into config, and automate the existing `infra/scripts/setup_*.sh` provisioning into a per-signup pipeline. Each customer gets their own GCP project, their own isolated stack, and their own GitHub App/PAT scoped to their repo.
- **Pro:** respects the current security architecture — intra-project SA trust, image-bound IaC hash, and the approval gates all keep working unchanged. Lowest *code* re-architecture.
- **Con:** a full stack per tenant is cost- and ops-heavy; it's "managed single-tenant," not an elastic SaaS.
- **Effort:** medium-high (weeks): de-hardcode + parameterize cloudbuild + onboarding automation + per-customer GitHub credential provisioning.

### Path B — Shared multi-tenant SaaS
One deployment; per-request tenant context threaded everywhere; cross-project GCP access into each tenant's project (via WIF / SA impersonation); namespaced Firestore (or per-tenant databases); per-tenant tofu state + artifacts; per-tenant GitHub App installation tokens; and a real user/account/session layer that does not exist today.
- **Pro:** the "real" product — elastic, one control plane.
- **Con:** breaks several *intentional* security properties — the image-bound IaC hash, the intra-project `ALLOWED_CALLERS` trust web, and the fail-closed single-bucket artifact validation each need redesign. Largest trust/security surface (holding powerful cross-tenant credentials).
- **Effort:** months. Highest risk.

### Path C — GitHub connector only (bring-your-own-repo demo)
A GitHub App / OAuth flow so a user connects their own GitHub and points DriftScribe at *their repo*, while the app still scans the demo Google Cloud project.
- **Pro:** smallest; a believable "plug in your own repo" demo moment.
- **Con:** semantically weak for DriftScribe — drift is GitHub-IaC-vs-live-GCP, so connecting a user's GitHub *without* their GCP detects nothing real. It's a demo affordance, not multi-tenancy.
- **Effort:** ~1–2 weeks — and most of that is the missing user/session layer, **not** the GitHub OAuth itself.

## Scope decision

- The literal "GitHub connector" is ~10–15% of real multi-tenancy. The hard, expensive part is **per-tenant GCP access + identity + data/state isolation**, plus reworking security gates that are single-tenant by design.
- DriftScribe is **deliberately single-tenant for the hackathon**: the choice buys a fully working, secure, end-to-end agent loop (detect → propose → human approves → apply) instead of a thin multi-tenant shell. Single-tenancy is what *lets* us enforce the human-approval gate, SA-to-SA trust, and image-hash-bound apply.
- **Productizing — whether via Path A or Path B — is out of scope for the hackathon.** Post-hackathon, **Path A** is the pragmatic route (respects the architecture); **Path B** is the true-SaaS endpoint but is a months-scale effort that reopens several security designs.
