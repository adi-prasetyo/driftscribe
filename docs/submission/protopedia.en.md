# DriftScribe: ProtoPedia submission (English)

> [日本語版はこちら](protopedia.ja.md)
>
> Submitted to: ProtoPedia (https://protopedia.net) for the DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy). Each section is sized to drop straight into a ProtoPedia form field.

## Title

DriftScribe: a multi-agent coordinator/worker pattern for safe AI-driven DevOps on Cloud Run

## Summary

DriftScribe is a multi-agent framework for safe AI-driven DevOps on Cloud Run. A single workload-aware coordinator (Google ADK + Gemini 2.5 Flash on Vertex AI) routes operator requests to per-workload agents that see only the tools they're allowed to use. Four crew ship today: **Anchor** watches a live Cloud Run service (`payment-demo`) against an ops contract and decides between `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`; **Patch** watches an npm `package.json` against the GitHub Advisory DB and decides between `no_op` / `docs_pr` / `upgrade_pr` / `escalation`; **Explore** is a read-only investigator across live infra and code, listing zero mutation tools; **Provision** authors OpenTofu changes and opens a single `iac/`-only PR that flows through a gated, HMAC-signed apply pipeline. Anchor runs autonomously (a live Eventarc trigger on Cloud Run config changes); the other three run on demand from chat. Reasoning is grounded by Google's Developer Knowledge MCP (attached at the coordinator only). Destructive paths are gated: rollback uses HMAC-signed single-use HITL approvals, and upgrade-PR opens reuse a post-LLM deterministic validator (semver, GHSA URL shape, path regex) that fails closed. The agent proposes, the operator (or the validator) disposes, and the worker boundary keeps "propose" safe to expose.

## Highlights

- **Workload-aware coordinator + narrow per-workload workers**: One public coordinator service routes `POST /chat workload=<name>` to a per-workload agent built with that workload's prompt and a per-workload tool subset. The LLM literally never sees a cross-workload tool. Workers live in their own Cloud Run services, hardcode payload-intent policy (request body cannot redirect them to a different repo/file/service), and are isolated from coordinator code (no `agent.*` imports, enforced by a subprocess-based test).
- **MCP-grounded reasoning**: Google's Developer Knowledge MCP is attached at the coordinator only (Streamable HTTP to `developerknowledge.googleapis.com/mcp`, 10-second timeout, 60-second response cache, fail-closed error envelope). The drift workload cites authoritative Cloud Run env-variable guidance in docs PRs; the upgrade workload cites migration guides for the package being bumped. Workers have no MCP access, which keeps their auth/network/observability surface minimal.
- **Layered safety, scoped per workload**: Layer 0 = a capability-bounded tool registry per workload, where each workload sees only its own tool subset (pinned by a three-way YAML ⇄ code-constant ⇄ runtime-resolution test). Layer 1 = per-service IAM, workload-scoped (coordinator's `run.invoker` on drift workers does NOT extend to upgrade workers, and vice versa). Layer 2 = worker payload-intent policy. Plus a post-LLM deterministic validator on the upgrade write path (lockfile-path regex, package_name must exist, target_version > current, version_jump ∈ {patch, minor}, GHSA-URL shape) and HMAC-signed HITL approvals on the drift rollback path.
- **Two non-overlapping auth boundaries**: Operator → coordinator uses `X-DriftScribe-Token` with constant-time comparison. Coordinator → workers uses audience-bound Google ID tokens that workers verify by both audience and caller email. Compromising one boundary does not unlock the other.
- **Cost-conscious operations**: `min-instances=0` makes idle cost $0. Per-`/chat` call ≈ $0.0003 (GCP + Gemini, estimated). MCP adds ~1 round-trip per docs-PR / upgrade-PR path (cached 60s in-coordinator). `X-Trace-Id` propagates from coordinator through every worker hop for single-request tracing in Cloud Logging.

## Stack

- Language / runtime: Python 3.12
- Web framework: FastAPI + uvicorn
- Agent framework: Google ADK (Agent Development Kit) with workload-aware factory
- LLM: Gemini 2.5 Flash on Vertex AI (`GOOGLE_CLOUD_LOCATION=global` so the coordinator receives reasoning summaries; Cloud Run itself runs in asia-northeast1)
- MCP: Google Developer Knowledge MCP (Streamable HTTP)
- Runtime: Cloud Run × 10 DriftScribe services (coordinator + drift `reader`/`docs`/`rollback` + upgrade `upgrade-reader`/`upgrade-docs` + infra `infra-reader`/`tofu-editor`/`tofu-apply` + shared `notifier`), plus 3 demo services (`payment-demo`, `storefront`, `orders-worker`)
- Data: Firestore (decisions, approvals, plan approvals, events, infra-graph cache)
- Events: Eventarc (Cloud Run audit-log trigger)
- Auth: Audience-bound Google ID tokens, Secret Manager, HMAC, single-use approval tokens
- Notifications: External webhook (webhook.site for the demo)
- Build / quality: uv, ruff, pytest (3,000+ tests), Cloud Build
- CI: GitHub Actions (ruff + pytest on PRs and pushes to main)

## Demo

DriftScribe's crews form a stewardship loop around one cloud estate: Provision stands infrastructure up, Anchor guards what's live with the only autonomous trigger, Patch keeps it current, and Explore explains it. The demo follows that arc.

The walkthrough is structured as eight beats across two workloads, driven by `scripts/demo.sh`.

**Workload 1 (Anchor / `drift`, 5 beats):** beat-a establishes a clean baseline that resolves to `no_op`; beat-b introduces a deliberate drift and turns it into a `drift_issue`; beat-c is the ADK reasoning beat where the agent explains the cause; beat-d previews a docs PR from the docs worker; beat-e exercises the rollback worker through the HITL approval gate.

**Workload 2 (Patch / `upgrade`, 3 beats):** `upgrade-a` is a discovery beat where the agent reads `demo/upgrade-target/package.json` and reports the lodash 4.17.20 / GHSA-35jh-r3h4-6jhm advisory; `upgrade-b` is the climax beat where the agent calls `search_developer_docs` for migration guidance, then proposes a REAL upgrade PR bumping lodash to 4.17.21 (requires `CONFIRM_UPGRADE_PR=1` env override; opens a real GitHub PR); `upgrade-c` is the safety beat where the agent attempts a major-version bump and the post-LLM validator refuses with 403, routing to `escalation` via the notifier.

**Explore and Provision** (the two infra crew) run interactively from chat rather than as scripted beats: Explore returns a read-only whole-project inventory via Cloud Asset Inventory and changes nothing, while Provision authors an `iac/`-only OpenTofu PR that a human approves before the `tofu-apply` worker (the sole live-infra mutator) applies it.

`X-Trace-Id` propagates from coordinator to every worker hop, so a single request can be followed end-to-end in Cloud Logging across every workload.

- 90-second demo video: [TBD: 90-second demo video]
- Architecture diagram: [`docs/architecture/architecture.html`](../architecture/architecture.html) (self-contained HTML, two stacked diagrams for trigger fan-in and layered safety; open in a browser)
- Demo runbook: [`docs/demo-script.md`](../demo-script.md) (operator pre-flight + per-beat expectations + cleanup-after-upgrade-b instructions)

## Scope & roadmap

**Current scope (single-tenant, by design).** DriftScribe runs bound to one GitHub repo and one Google Cloud project. This is deliberate: we prioritized a fully working, secure, end-to-end agent loop (detect drift → propose IaC PR → human approves → apply) over a thin multi-tenant shell. Single-tenancy is what *lets* us enforce the guarantees above: the human approval gate, service-account trust between workers, and an apply worker that only runs plans whose IaC matches a hash baked into its own image.

**Path to product.** Letting other users run DriftScribe on their own GitHub and their own cloud is the clear next step, reachable either as isolated per-customer deployments or as a shared multi-tenant service. We scoped that out of the hackathon *deliberately*: the multi-tenant identity and cross-project cloud access it requires is security-sensitive work we'd rather do right than rush. A "GitHub connector" alone is the small part (~10-15%); the real work is per-tenant cloud access, identity, and data isolation. The full single-tenant coupling map and the productization paths are documented at `docs/plans/2026-06-24-multi-tenant-productization-scope.md`.

## Repository

https://github.com/adi-prasetyo/driftscribe

## Deployed URLs

- Operator UI / coordinator (`driftscribe-agent`, public): <https://driftscribe.adp-app.com> (behind Cloudflare Access; open to anonymous visitors during the judging window)
- Drift workers (private): `driftscribe-reader`, `driftscribe-docs`, `driftscribe-rollback`
- Upgrade workers (private): `driftscribe-upgrade-reader`, `driftscribe-upgrade-docs`
- Infra workers (private): `driftscribe-infra-reader`, `driftscribe-tofu-editor`, `driftscribe-tofu-apply` (the sole live-infra mutator)
- Shared (private): `driftscribe-notifier`
- Demo services: `payment-demo` (drift target), plus `storefront` + `orders-worker` (the checkout demo DriftScribe provisioned through its own author → approve → apply loop)
- Demo upgrade target: `demo/upgrade-target/package.json` (pinned to lodash@4.17.20 for the upgrade beats)

> The nine private workers are deployed with `--no-allow-unauthenticated`; they are reachable only via audience-bound ID tokens minted by the coordinator's service account. The coordinator's `run.invoker` grant is workload-scoped: its grant on drift workers does NOT extend to upgrade workers (and vice versa), which is Layer 1 of the framework.
