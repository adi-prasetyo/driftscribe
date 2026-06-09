# DriftScribe
> [日本語版はこちら](README.ja.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)
[![E2E](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml/badge.svg?event=workflow_dispatch)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml)

A multi-agent coordinator/worker pattern for safe AI-driven DevOps on Cloud Run.
Four workloads ship today: live drift detection (`payment-demo` Cloud Run env vs
an ops contract), dependency upgrade reviews (npm `package.json` vs GitHub
Advisory DB), read-only project exploration, and agent-authored
infrastructure-as-code (OpenTofu PRs through a gated apply pipeline). The
reasoning loops are grounded by Google's Developer Knowledge MCP. Submission for
DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

**New here?** Start with [`docs/OVERVIEW.md`](docs/OVERVIEW.md) — a plain-English, ~10-minute tour of the whole system.

**Architecture diagram:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html) — self-contained, open in a browser.

## Pattern

DriftScribe is built around four invariants that hold across every workload:

- **Workload-aware coordinator.** One public service routes `POST /chat workload=<name>` to a workload-specific agent prompt + tool set. The LLM never sees a cross-workload tool — capability is bounded per workload, not just at the registry layer.
- **Narrow per-workload workers.** Each workload has its own execute-only worker pair (or trio). Workers hardcode payload-intent policy: the request body cannot redirect a worker at a different repo, file, or service. Worker code never imports `agent.*` — they are isolated processes.
- **Layer 0 / 1 / 2 safety.** Layer 0: capability-bounded tool registry, per workload. Layer 1: per-service IAM scoping — the coordinator's `run.invoker` on drift workers does NOT extend to upgrade workers. Layer 2: payload-intent policy at each worker, plus a post-LLM deterministic validator on the upgrade write path (semver shape, path regex, GHSA URL shape) and HITL on the drift rollback path.
- **MCP-grounded reasoning.** Google's Developer Knowledge MCP is attached at the coordinator. The drift workload cites authoritative Cloud Run env-variable guidance; the upgrade workload cites migration guides for the package being bumped. Workers do NOT have MCP access — only the coordinator's reasoning step.

The full topology and the IAM boundaries are documented in
[`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md).

## Workloads

### Workload 1: Drift

- Watches the `payment-demo` Cloud Run service env vs [`demo/ops-contract.yaml`](demo/ops-contract.yaml).
- Actions: `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`.
- Workers: `reader` (read-only Cloud Run state), `docs` (open docs PR), `rollback` (revision rollback), plus the shared `notifier`.
- HITL approval gate on `rollback`: HMAC-signed one-shot link, 15-minute TTL, single-use Firestore transaction. The coordinator never executes a rollback itself — it only mints the approval URL.

### Workload 2: Dependency Upgrades

- Watches [`demo/upgrade-target/package.json`](demo/upgrade-target/package.json) vs the GitHub Advisory DB.
- Actions: `no_op` / `docs_pr` / `upgrade_pr` / `escalation`.
- Workers: `upgrade-reader` (read-only lockfile + advisory query), `upgrade-docs` (open upgrade PR), plus the shared `notifier`.
- Post-LLM deterministic validator on the write path: lockfile path regex, `package_name` must exist in the current lockfile, `target_version` must be greater than current (no downgrades), version jump ∈ {patch, minor}, `advisory_url` must match `https://github.com/advisories/GHSA-...`. Major bumps are refused at the validator — the LLM is instructed to route those to `escalation`; if it doesn't, the validator fails closed.
- Also carries PR-lifecycle tools (`upgrade-close-pr`, `upgrade-merge-pr`) so the agent can close or CI-gated-merge an upgrade PR it opened; the `upgrade-docs` worker re-validates eligibility (driftscribe label + `upgrade/` branch + `main` base, green required check) before acting.

### Workloads 3 & 4: Infrastructure (read + author)

Two **chat-only** workloads cover infrastructure-as-code (the infra-IaC initiative):

- **`explore`** (read-only) — whole-project resource inspection via Cloud Asset Inventory (`infra-reader` worker), plus live Cloud Run env, the ops contract, the dependency lockfile, and developer docs. Lists **zero mutation tools** — it can read everything and change nothing (the read-only guarantee is pinned by a test that asserts its tools are disjoint from the mutation set).
- **`provision`** (infra edits) — authors OpenTofu changes from a chat request and opens **one `iac/`-only PR** via the `tofu-editor` worker (which re-validates every file: `iac/` prefix, foundation ban, secret ban, AGENT-mode static gate). It never touches live infra. The actual `tofu apply` runs **downstream** in the `tofu-apply` worker — the sole live-infra mutator — behind a plan-bound, HMAC-signed operator approval, a path the chat agent cannot invoke directly.

Both are chat-only: `/recheck` refuses them (no autonomous observation source). The operator UI renders a live infra resource map (managed vs. drift) alongside the decisions timeline.

## Demo

```bash
# Workload 1: drift
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-a   # baseline → no_op
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-b   # drift → drift_issue
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-c   # ADK reasoning beat
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-d   # docs PR preview
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh beat-e   # rollback w/ HITL gate

# Workload 2: upgrade (upgrade-b opens a REAL PR; confirmation gate required)
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-a              # discover deps + advisories (read-only)
PROJECT=driftscribe-hack-2026 CONFIRM_UPGRADE_PR=1 \
  ./scripts/demo.sh upgrade-b                                          # propose bump (lodash 4.17.20 → 4.17.21)
PROJECT=driftscribe-hack-2026 ./scripts/demo.sh upgrade-c              # safety: validator refuses major bump

PROJECT=driftscribe-hack-2026 ./scripts/demo.sh cleanup                # restore drift baseline (drift only)
```

`upgrade-b` requires `CONFIRM_UPGRADE_PR=1` on every invocation because it opens
a real pull request on the configured `GITHUB_REPO`. The gate is single-use by
design — re-firing from shell history alone won't open another PR unless the
env var is still set in the shell.

Full operator runbook (screen layout, timing, expected outputs, cleanup):
[`docs/demo-script.md`](docs/demo-script.md).

## Cost & latency

Per `/chat` call: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (estimated; verify
with the 20-call benchmark below). The Developer Knowledge MCP call adds ~1
round-trip per `docs_pr` / `upgrade_pr` path; the coordinator caches MCP
results for 60s in-process, so repeated mentions within a session don't
multiply. p50 latency: TBD ms classifier-path, TBD ms ADK-path. p95: TBD ms.
Idle cost at `min-instances=0`: $0. Demo total spend over hackathon: TBD (pull
from GCP billing breakdown before submission).

To collect real numbers, run 20 back-to-back `/chat` calls against the deployed
coordinator and record the `X-Trace-Id` + wall-clock for each; compute p50/p95
from the resulting series. Procedure lives alongside the demo runner — see
[`scripts/demo.sh`](scripts/demo.sh) for the request shape and operator-token
resolution.

**Log retention:** Cloud Logging's `_Default` bucket is extended to 365 days
by `infra/scripts/setup_secrets.sh`. All DriftScribe logs (including the
agent's thought summaries, tool-call events, and per-call LLM-usage records)
are preserved and queryable via Logs Explorer for a year. Storage beyond day
30 is billed at $0.01/GiB-month; hackathon volume is well under the threshold
where this matters. See [`docs/runbooks/deploy.md`](docs/runbooks/deploy.md)
for the verification step and a sample query.

## How DriftScribe's drift workload compares to other drift tools

The table below scopes the comparison to Workload 1 (drift). The upgrade
workload sits in a different category (Dependabot- / Renovate-shaped) and is
not compared here.

| | DriftScribe (Workload 1) | Drift (CloudPosse) | Steampipe | Cloud Custodian | AWS Config Rules |
| --- | --- | --- | --- | --- | --- |
| AI-driven decisions | ✓ | ✗ | ✗ | ✗ | ✗ |
| HITL approval gates | ✓ | ✗ | ✗ | ✗ | ✗ |
| Layered safety (OS + policy) | ✓ | ✗ | ✗ | partial | partial |
| Multi-cloud | ✗ (GCP only) | ✓ (Terraform-aware, multi) | ✓ | ✓ (AWS-primary) | ✗ (AWS) |
| Open source | ✓ | ✓ | ✓ | ✓ | ✗ |
| Deployment surface | Cloud Run (10 DriftScribe services + 3 demo services) | Terraform | Plugin host | Lambda | Managed service |
| Target user | DevOps + SRE on GCP | IaC platform teams | SQL-fluent ops | AWS ops | AWS compliance teams |

DriftScribe trades multi-cloud breadth for layered safety on a single platform;
it's hackathon-stage, the others are production-mature. The wager is that
AI + HITL is the missing axis — existing tools detect drift well but either
stop at the report (Drift, Steampipe) or — when remediation is enabled — apply
changes without HITL as the default product centerline (Custodian, Config
Rules can be composed with approval workflows; it just isn't the default).
DriftScribe sits in the middle: the agent proposes, the operator disposes, and
the worker boundary makes "propose" safe to expose.

## Repository layout

- [`agent/`](agent/) — coordinator service (ADK agent, classifier, approvals, auth, MCP attach, IaC authoring)
- [`workloads/`](workloads/) — per-workload manifests (`drift`, `upgrade`, `explore`, `provision`): system prompts, contracts, tool/worker/action lists
- [`workers/`](workers/) — execute-only worker services: drift `reader` / `docs` / `rollback`, upgrade `upgrade-reader` / `upgrade-docs`, infra `infra-reader` / `tofu-editor` / `tofu-apply`, plus the shared `notifier`
- [`driftscribe_lib/`](driftscribe_lib/) — shared library (structured logging + trace IDs, GitHub helpers, HCL parser, plan-approval schema)
- [`iac/`](iac/) — the OpenTofu the agent reads and authors (the demo's own infrastructure)
- [`frontend/`](frontend/) — operator UI (Svelte + Vite SPA, served at `/`)
- [`demo/`](demo/) — `payment-demo` drift target + ops contract, `upgrade-target` pinned npm lockfile
- [`docs/`](docs/) — [`OVERVIEW.md`](docs/OVERVIEW.md) (start here), `architecture/`, `runbooks/`, `plans/`
- [`scripts/`](scripts/) — demo runner
- [`infra/`](infra/) — Cloud Build + smoke tests
- [`tests/`](tests/) — unit + integration suite

## Status

Built out past the hackathon MVP. Two initiatives landed on top of the Phase 17
multi-agent framework:

- **Infra-IaC agent** — a whole-project inventory reader (`infra-reader`, Cloud
  Asset Inventory), agent-authored OpenTofu via the `tofu-editor` worker, and a
  gated `tofu-apply` worker (sole live-infra mutator) behind a plan-bound,
  HMAC-signed approval. The `explore` and `provision` workloads expose the read
  and author sides. DriftScribe drove this very pipeline (author → approve →
  apply) to provision its own checkout demo (`storefront` + `orders-worker`).
- **Operator UI** — rebuilt as a Svelte + Vite SPA, now served at the site root
  `/` (operator token required), with a live infra resource-map panel
  (managed vs. drift) and a per-decision trace + env-diff view.

This sits on Phase 20 (assertive E2E suite — drift via `/recheck`, upgrade via
GitHub branch observation, HITL form-POST flow with explicit revision capture,
Playwright UI on stable `data-testid` selectors, in a dedicated `driftscribe-e2e`
GCP project under WIF + Required-reviewer gate), Phase 19.B (transparency UI),
Phase 18.A (365-day logging), and Phase 17 (multi-agent framework). Hackathon
submission deadline 2026-07-10.

Implementation plans live in [`docs/plans/`](docs/plans/) (dated, newest last).
E2E runbooks: [`docs/runbooks/e2e-environment.md`](docs/runbooks/e2e-environment.md)
(project + secrets + cloudbuild) and [`docs/runbooks/e2e-ci.md`](docs/runbooks/e2e-ci.md)
(WIF + GitHub Environment).

Operator UI: `/` (the coordinator root; operator token required). See [`docs/demo-script.md`](docs/demo-script.md#transparency-ui-walkthrough) for the walkthrough.
