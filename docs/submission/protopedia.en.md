# DriftScribe — ProtoPedia submission (English)

> [日本語版はこちら](protopedia.ja.md)
>
> Submitted to: ProtoPedia (https://protopedia.net) — DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy). Each section is sized to drop straight into a ProtoPedia form field.

## Title

DriftScribe — a multi-agent coordinator/worker pattern for safe AI-driven DevOps on Cloud Run

## Summary

DriftScribe is a multi-agent framework for safe AI-driven DevOps on Cloud Run. A single workload-aware coordinator (Google ADK + Gemini 2.5 Flash on Vertex AI) routes operator requests to per-workload agents that see only the tools they're allowed to use. Two demo workloads ship today: **Anchor** (the `drift` workload) watches a live Cloud Run service (`payment-demo`) against an ops contract and decides between `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`; **Patch** (the `upgrade` workload) watches an npm `package.json` against GitHub Advisory DB and decides between `no_op` / `docs_pr` / `upgrade_pr` / `escalation`. Reasoning is grounded by Google's Developer Knowledge MCP (attached at the coordinator only). Destructive paths are gated — rollback uses HMAC-signed single-use HITL approvals; upgrade-PR opens reuse a post-LLM deterministic validator (semver, GHSA URL shape, path regex) that fails closed. The agent proposes, the operator (or the validator) disposes, and the worker boundary keeps "propose" safe to expose.

## Highlights

- **Workload-aware coordinator + narrow per-workload workers**: One public coordinator service routes `POST /chat workload=<name>` to a per-workload agent built with that workload's prompt and a per-workload tool subset. The LLM literally never sees a cross-workload tool. Workers live in their own Cloud Run services, hardcode payload-intent policy (request body cannot redirect them to a different repo/file/service), and are isolated from coordinator code (no `agent.*` imports — enforced by a subprocess-based test).
- **MCP-grounded reasoning**: Google's Developer Knowledge MCP is attached at the coordinator only (Streamable HTTP to `developerknowledge.googleapis.com/mcp`, 10-second timeout, 60-second response cache, fail-closed error envelope). The drift workload cites authoritative Cloud Run env-variable guidance in docs PRs; the upgrade workload cites migration guides for the package being bumped. Workers have no MCP access — minimal auth/network/observability surface.
- **Layered safety, scoped per workload**: Layer 0 = capability-bounded tool registry per workload (10 callables total, ~6-8 per workload, pinned by a three-way YAML ⇄ code-constant ⇄ runtime-resolution test). Layer 1 = per-service IAM, workload-scoped (coordinator's `run.invoker` on drift workers does NOT extend to upgrade workers, and vice versa). Layer 2 = worker payload-intent policy. Plus a post-LLM deterministic validator on the upgrade write path (lockfile-path regex, package_name must exist, target_version > current, version_jump ∈ {patch, minor}, GHSA-URL shape) and HMAC-signed HITL approvals on the drift rollback path.
- **Two non-overlapping auth boundaries**: Operator → coordinator uses `X-DriftScribe-Token` with constant-time comparison. Coordinator → workers uses audience-bound Google ID tokens that workers verify by both audience and caller email. Compromising one boundary does not unlock the other.
- **Cost-conscious operations**: `min-instances=0` makes idle cost $0. Per-`/chat` call ≈ $0.0003 (GCP + Gemini, estimated). MCP adds ~1 round-trip per docs-PR / upgrade-PR path (cached 60s in-coordinator). `X-Trace-Id` propagates from coordinator through every worker hop for single-request tracing in Cloud Logging.

## Stack

- Language / runtime: Python 3.12
- Web framework: FastAPI + uvicorn
- Agent framework: Google ADK (Agent Development Kit) with workload-aware factory
- LLM: Gemini 2.5 Flash (Vertex AI, asia-northeast1)
- MCP: Google Developer Knowledge MCP (Streamable HTTP)
- Runtime: Cloud Run × 7 services after Phase 17 (coordinator + 4 drift workers + 2 upgrade workers); the `notifier` is shared across workloads
- Data: Firestore (decisions, approvals)
- Events: Eventarc (Cloud Run audit-log trigger)
- Auth: Audience-bound Google ID tokens, Secret Manager, HMAC, single-use approval tokens
- Notifications: External webhook (webhook.site for the demo)
- Build / quality: uv, ruff, pytest (≥720 tests), Cloud Build
- CI: GitHub Actions (ruff + pytest on PRs and pushes to main)

## Demo

The walkthrough is structured as eight beats across two workloads, driven by `scripts/demo.sh`.

**Workload 1 (Anchor / `drift`, 5 beats):** beat-a establishes a clean baseline that resolves to `no_op`; beat-b introduces a deliberate drift and turns it into a `drift_issue`; beat-c is the ADK reasoning beat where the agent explains the cause; beat-d previews a docs PR from the docs worker; beat-e exercises the rollback worker through the HITL approval gate.

**Workload 2 (Patch / `upgrade`, 3 beats):** `upgrade-a` is a discovery beat — the agent reads `demo/upgrade-target/package.json` and reports the lodash 4.17.20 / GHSA-35jh-r3h4-6jhm advisory; `upgrade-b` is the climax beat — the agent calls `search_developer_docs` for migration guidance, then proposes a REAL upgrade PR bumping lodash to 4.17.21 (requires `CONFIRM_UPGRADE_PR=1` env override; opens a real GitHub PR); `upgrade-c` is the safety beat — the agent attempts a major-version bump and the post-LLM validator refuses with 403, routing to `escalation` via the notifier.

`X-Trace-Id` propagates from coordinator to every worker hop, so a single request can be followed end-to-end in Cloud Logging across both workloads.

- 90-second demo video: [TBD: 90-second demo video]
- Architecture diagram: [`docs/architecture/architecture.html`](../architecture/architecture.html) (self-contained HTML, two stacked diagrams — trigger fan-in + layered safety; open in a browser)
- Demo runbook: [`docs/demo-script.md`](../demo-script.md) (operator pre-flight + per-beat expectations + cleanup-after-upgrade-b instructions)

## Repository

https://github.com/adi-prasetyo/driftscribe

## Deployed URLs

- Coordinator (`driftscribe-agent`, public): [TBD after deploy: https://driftscribe-agent-xxxxx-an.a.run.app]
- Drift Reader (`driftscribe-reader`, private): [TBD after deploy]
- Drift Docs (`driftscribe-docs`, private): [TBD after deploy]
- Drift Rollback (`driftscribe-rollback`, private): [TBD after deploy]
- Notifier (`driftscribe-notifier`, private, shared across workloads): [TBD after deploy]
- Upgrade Reader (`driftscribe-upgrade-reader`, private): [TBD after deploy]
- Upgrade Docs (`driftscribe-upgrade-docs`, private): [TBD after deploy]
- Watched service (`payment-demo`, drift target): [TBD after deploy]
- Demo upgrade target: `demo/upgrade-target/package.json` (pinned to lodash@4.17.20 for the upgrade beats)

> The six private workers are deployed with `--no-allow-unauthenticated`; they are reachable only via audience-bound ID tokens minted by the coordinator's service account. The coordinator's `run.invoker` grant on drift workers does NOT extend to upgrade workers (and vice versa) — workload-scoped IAM is the Layer 1 of the framework.
