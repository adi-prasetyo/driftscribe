# DriftScribe

**The agent proposes, you approve.**

> [日本語版はこちら](README.ja.md)

[![CI](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/ci.yml)
[![E2E](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml/badge.svg?event=workflow_dispatch)](https://github.com/adi-prasetyo/driftscribe/actions/workflows/e2e.yml)

**An AI DevOps agent that watches your Google Cloud estate and proposes fixes,
but never applies a risky change on its own.** Four crews ship today, each one a
workload with its own prompt and its own short list of tools. Each reads
everything in its lane, proposes rather than applies, and is held to the safety
boundary in the last column:

| Crew | Trigger | Scope | Safety boundary |
| --- | --- | --- | --- |
| **Anchor** | Autonomous (Eventarc) | Live Cloud Run config vs ops contract → docs PR, drift issue, or rollback | Rollback waits behind a single-use HITL approval |
| **Patch** | On demand (chat) | npm deps vs GitHub Advisory DB → upgrade PR | Major bumps refused by a deterministic validator |
| **Provision** | On demand (chat) | Authors `iac/`-only OpenTofu PRs | Never touches live infra; apply is a separate gated worker |
| **Explore** | On demand (chat) | Read-only inventory of the whole project; also explains how DriftScribe works | Zero mutation tools (pinned by a test) |

**How the crews fit together:** it runs as a loop. Provision stands new
infrastructure up (you ask, it opens the IaC PR). Anchor then guards what's
live, catching drift the moment it appears, on its own. Patch keeps
dependencies current, and Explore answers anything read-only. The handoff is
the point: you provision once, and Anchor keeps watch for drift.

The coordinator is Gemini on Google's Agent Development Kit, grounded by the
Developer Knowledge MCP; it holds no direct power to act. Narrow single-purpose
workers execute within hardcoded limits, rollbacks and live-infra applies always
wait behind single-use human approval gates, and every decision lands in the
operator UI with its reasoning trace. (Patch's autonomous trigger is future work;
today it runs from chat, and its `/recheck` path is unimplemented.)

Submission for the DevOps × AI Agent Hackathon 2026 (Google Cloud Japan / Findy).

**Live demo:** <https://driftscribe.adp-app.com>. The operator UI is open to
anonymous visitors during the hackathon judging window (behind Cloudflare
Access otherwise).

**New here?** Start with [`docs/OVERVIEW.md`](docs/OVERVIEW.md), a plain-English, ~10-minute tour of the whole system.

**Architecture diagram:** [`docs/architecture/architecture.html`](docs/architecture/architecture.html). Self-contained, open in a browser.

## Pattern

DriftScribe is built around four invariants that hold across every workload:

- **Workload-aware coordinator.** One public service routes `POST /chat workload=<name>` to a workload-specific agent prompt + tool set. The LLM never sees a cross-workload tool. Capability is bounded per workload, not just at the registry layer.
- **Narrow per-workload workers.** Each workload has its own execute-only worker pair (or trio). Workers hardcode payload-intent policy: the request body cannot redirect a worker at a different repo, file, or service. Worker code never imports `agent.*`; they are isolated processes.
- **Layer 0 / 1 / 2 safety.** Layer 0: capability-bounded tool registry, per workload. Layer 1: per-service IAM scoping. The coordinator's `run.invoker` on drift workers does NOT extend to upgrade workers. Layer 2: payload-intent policy at each worker, plus a post-LLM deterministic validator on the upgrade write path (semver shape, path regex, GHSA URL shape) and HITL on the drift rollback path.
- **MCP-grounded reasoning.** Google's Developer Knowledge MCP is attached at the coordinator. The drift workload cites authoritative Cloud Run env-variable guidance; the upgrade workload cites migration guides for the package being bumped. Workers do NOT have MCP access. Only the coordinator's reasoning step does.

The full topology and the IAM boundaries are documented in
[`docs/architecture/multi-agent-design.md`](docs/architecture/multi-agent-design.md).

## Workloads

### Anchor: Cloud Run config drift (`drift`)

Anchor runs autonomously: a live Eventarc trigger reacts to every Cloud Run
config change. It's event-driven, not a polling loop, so no chat invocation is
needed.

- Watches the `payment-demo` Cloud Run service env vs [`demo/ops-contract.yaml`](demo/ops-contract.yaml).
- Actions: `no_op` / `docs_pr` / `drift_issue` / `rollback` / `escalation`.
- Workers: `reader` (read-only Cloud Run state), `docs` (open docs PR), `rollback` (revision rollback), plus the shared `notifier`.
- HITL approval gate on `rollback`: HMAC-signed one-shot link, 15-minute TTL, single-use Firestore transaction. Anchor never executes a rollback itself; the `rollback` worker mints the one-time approval URL.

### Patch: dependency upgrades (`upgrade`)

Patch runs on demand from chat. Its autonomous trigger (a `/recheck` observation
loop analogous to Anchor's Eventarc trigger) is future work. Today `/recheck`
returns 503 (unimplemented) and Patch is chat-only.

- Checks [`demo/upgrade-target/package.json`](demo/upgrade-target/package.json) vs the GitHub Advisory DB.
- Actions: `no_op` / `docs_pr` / `upgrade_pr` / `escalation`.
- Workers: `upgrade-reader` (read-only lockfile + advisory query), `upgrade-docs` (open upgrade PR), plus the shared `notifier`.
- Post-LLM deterministic validator on the write path: lockfile path regex, `package_name` must exist in the current lockfile, `target_version` must be greater than current (no downgrades), version jump ∈ {patch, minor}, `advisory_url` must match `https://github.com/advisories/GHSA-...`. Major bumps are refused at the validator. The LLM is instructed to route those to `escalation`; if it doesn't, the validator fails closed.
- Also carries PR-lifecycle tools (`upgrade-close-pr`, `upgrade-merge-pr`) so the agent can close or CI-gated-merge an upgrade PR it opened; the `upgrade-docs` worker re-validates eligibility (driftscribe label + `upgrade/` branch + `main` base, green required check) before acting.

### Provision: infrastructure author (`provision`)

On demand, from chat only (`/recheck` refuses it: it has no autonomous
observation source).

- Authors OpenTofu changes from a chat request and opens one `iac/`-only PR via the `tofu-editor` worker (which re-validates every file: `iac/` prefix, foundation ban, secret ban, AGENT-mode static gate).
- Provision never touches live infrastructure. The actual `tofu apply` runs downstream in the `tofu-apply` worker (the only thing that runs `tofu apply` against live infra), behind a plan-bound, HMAC-signed operator approval, a path the chat agent cannot invoke directly.

### Explore: read-only investigation (`explore`)

On demand, from chat only (`/recheck` refuses it too).

- The broadest read scope of the four crews: it reads across every lane, the live Cloud Run env, the ops contract, the dependency lockfile, the whole-project resource inventory via Cloud Asset Inventory (`infra-reader` worker), pending IaC plan artifacts, the team decision log, past conversations, and authoritative developer docs.
- It is also the crew that explains DriftScribe itself: its prompt carries the whole-system overview, so a newcomer can get oriented in chat without reading the docs first. The other three crews redirect "how does DriftScribe work" questions here.
- Explore lists zero mutation tools. It can read everything and change nothing, a guarantee pinned by a test that asserts its tools are disjoint from the mutation set.

The operator UI renders a live infra resource map (managed vs. drift) alongside the decisions timeline.

**Read scope at a glance.** The crews differ as much in what they can *see* as
in what they can do. Anchor and Patch read only their own lane; Provision adds
the infra inventory it needs to author changes; Explore reads across every lane,
which is what makes it the orientation crew.

| Read source | Anchor | Patch | Provision | Explore |
| --- | :--: | :--: | :--: | :--: |
| Live Cloud Run env | ✓ | ✗ | ✓ | ✓ |
| Dependency lockfile | ✗ | ✓ | ✗ | ✓ |
| Ops contract | ✓ | ✗ | ✓ | ✓ |
| Whole-project inventory (Cloud Asset) | ✗ | ✗ | ✓ | ✓ |
| Pending IaC plan artifacts | ✗ | ✗ | ✗ | ✓ |
| Developer docs (MCP) | ✓ | ✓ | ✓ | ✓ |
| Recent GitHub PRs | ✓ | ✓ | ✗ | ✗ † |
| Team decision log | ✗ | ✗ | ✗ | ✓ |
| Past conversations | ✓ | ✓ | ✓ | ✓ |

† Explore deliberately omits the recent-PR search: that one tool rides a
write-capable GitHub token, which a strictly read-only crew must not hold. Every
other Explore tool is backed by a read-only credential.

## Demo

The demo is the live operator UI at <https://driftscribe.adp-app.com>. It shows
the infra resource map (managed vs. drift), the decisions timeline, and the
reasoning trace behind every decision, so you can watch a drift detection, a
docs PR, an upgrade proposal, and the rollback approval gate from the browser
without touching a terminal.

### Real, but restorable

A mutation stays open to anonymous visitors when its blast radius is bounded and
mechanically restorable, and is gated when it is not.

- **Open, self-healing:** asking Patch to fix the vulnerable dependency merges a
  real PR (one line of `demo/upgrade-target/package.json`); asking Anchor to roll
  back really moves `payment-demo` traffic to an earlier revision. A scheduled
  workflow ([`demo-reset.yml`](.github/workflows/demo-reset.yml)) restores all
  three baselines: the service every two hours, the upgrade fixture within a
  couple of hours of being fixed, and any adoption PR a visitor opens is closed
  unmerged after about two hours so the Adopt demo stays available. Nothing a
  visitor does is applied to real infrastructure.
- **Gated:** merging an infrastructure PR always requires the operator's
  identity, and free-form infrastructure authoring is operator-only during the
  public window (the one-click Adopt path, which only ever emits a bounded
  zero-change import, stays open). A merged infra PR cannot be unmerged, so it
  never happens anonymously.

What you see is neither a mockup nor an honor system: real changes land, and the
parts that cannot be safely reset are the parts you cannot reach.

`scripts/demo.sh` is the companion runner that drives activity behind that UI
(or for a keyboard-only walkthrough). The drift beats mutate the `payment-demo`
Cloud Run service to create the drift the UI then surfaces; the upgrade beats
exercise the dependency-upgrade path, where `upgrade-b` opens a real PR.

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
design: re-firing from shell history alone won't open another PR unless the
env var is still set in the shell.

Full operator runbook (UI walkthrough, screen layout, timing, expected outputs,
cleanup): [`docs/demo-script.md`](docs/demo-script.md).

## Cost & latency

Per `/chat` call: ~$0.0002 GCP + ~$0.0001 Gemini = ~$0.0003 (estimated). The
Developer Knowledge MCP call adds ~1 round-trip per `docs_pr` / `upgrade_pr`
path; the coordinator caches MCP results for 60s in-process, so repeated
mentions within a session don't multiply.

**Latency** (measured: 20 back-to-back warm `/chat` calls to the live
coordinator, Explore explainer path, fixed prompt, warm-ups discarded):
**p50 ≈ 3.2 s, p95 ≈ 5.5 s** (min 2.2 s, max 5.6 s). This covers the interactive
ADK chat path; autonomous drift detection runs on the `/recheck` path, which is
event-driven (Eventarc) and exercised separately by the E2E suite. With
`min-instances=0`, the first call after the service scales to zero adds a
container + model-client cold start on top of these warm figures.

**Spend.** Idle cost at `min-instances=0` is $0. No BigQuery billing export was
enabled for the demo project, so this README doesn't report a precise project
total. Billing exports aren't retroactive, and we won't publish a fabricated
figure. Demo volume is low: tens of `/chat` calls plus a handful of Cloud Build
deploys. For an exact number, the GCP Billing console → Reports, filtered to
project `driftscribe-hack-2026` over the hackathon window, is authoritative.

To reproduce the latency numbers, run 20 back-to-back `/chat` calls (after 3
discarded warm-ups, using the same Explore explainer prompt) against the deployed
coordinator and record the `X-Trace-Id` + wall-clock for each; compute p50/p95
from the resulting series. See [`scripts/demo.sh`](scripts/demo.sh) for the
request shape and operator-token resolution.

**Log retention:** Cloud Logging's `_Default` bucket is extended to 365 days
by `infra/scripts/setup_secrets.sh`. All DriftScribe logs (including the
agent's thought summaries, tool-call events, and per-call LLM-usage records)
are preserved and queryable via Logs Explorer for a year. Storage beyond day
30 is billed at $0.01/GiB-month; hackathon volume is well under the threshold
where this matters. See [`docs/runbooks/deploy.md`](docs/runbooks/deploy.md)
for the verification step and a sample query.

## Repository layout

- [`agent/`](agent/): coordinator service (ADK agent, classifier, approvals, auth, MCP attach, IaC authoring)
- [`workloads/`](workloads/): per-workload manifests (`drift`, `upgrade`, `explore`, `provision`): system prompts, contracts, tool/worker/action lists
- [`workers/`](workers/): execute-only worker services: drift `reader` / `docs` / `rollback`, upgrade `upgrade-reader` / `upgrade-docs`, infra `infra-reader` / `tofu-editor` / `tofu-apply`, plus the shared `notifier`
- [`driftscribe_lib/`](driftscribe_lib/): shared library (structured logging + trace IDs, GitHub helpers, HCL parser, plan-approval schema)
- [`iac/`](iac/): the OpenTofu the agent reads and authors (the demo's own infrastructure)
- [`frontend/`](frontend/): operator UI (Svelte + Vite SPA, served at `/`)
- [`demo/`](demo/): `payment-demo` drift target + ops contract, `upgrade-target` pinned npm lockfile
- [`docs/`](docs/): [`OVERVIEW.md`](docs/OVERVIEW.md) (start here), `architecture/`, `runbooks/`, `plans/`
- [`scripts/`](scripts/): demo runner
- [`infra/`](infra/): Cloud Build + smoke tests
- [`tests/`](tests/): unit + integration suite

## Scope & roadmap

**Current scope.** DriftScribe runs single-tenant, bound to one GitHub repo and
one Google Cloud project. This is a deliberate choice: we shipped a fully
working, secure, end-to-end agent loop (detect drift → propose IaC PR → human
approves → apply) rather than a thin multi-tenant shell. Single-tenancy is what
lets us enforce strong guarantees. Every infra change passes a human approval
gate, workers authorize each other by service-account identity, and the
`tofu-apply` worker only runs plans whose IaC matches a hash baked into its own
image.

**Path to product.** Letting other users run DriftScribe on their own GitHub and
their own cloud is a clear next step, reachable either as isolated per-customer
deployments or as a shared multi-tenant service. We scoped that productization
out of the hackathon *deliberately*: the multi-tenant identity and cross-project
access it requires is security-sensitive work we'd rather do right than rush,
so we could keep the core agent loop solid and fully working end-to-end. The full
single-tenant coupling map and the productization paths are written up in
[`docs/plans/2026-06-24-multi-tenant-productization-scope.md`](docs/plans/2026-06-24-multi-tenant-productization-scope.md).

## Status

Built out past the hackathon MVP. Three initiatives landed on top of the Phase 17
multi-agent framework:

- **Infra-IaC agent:** a whole-project inventory reader (`infra-reader`, Cloud
  Asset Inventory), agent-authored OpenTofu via the `tofu-editor` worker, and a
  gated `tofu-apply` worker (the only thing that runs `tofu apply`) behind a plan-bound,
  HMAC-signed approval. The `explore` and `provision` workloads expose the read
  and author sides. DriftScribe drove this very pipeline (author → approve →
  apply) to provision its own checkout demo (`storefront` + `orders-worker`).
- **Operator UI:** rebuilt as a Svelte + Vite SPA, now served at the site root
  `/` (operator token required), with a live infra resource-map panel
  (managed vs. drift) and a per-decision trace + env-diff view.
- **Multi-turn chat + team memory:** operator chats with each crew are persisted
  and resumable from a history rail in the operator UI. Crews can also read each
  other's recent conversations as shared, read-only "team memory" (turn text is
  secret-redacted and snippet-capped), so a question asked of one crew can help
  inform the others.

This sits on Phase 20 (assertive E2E suite: drift via `/recheck`, upgrade via
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
