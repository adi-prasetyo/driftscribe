# DriftScribe — Checkout demo build-out + live infra graph

**Date:** 2026-06-03
**Status:** Phase 1 (resource-map graph) SHIPPED + LIVE 2026-06-04 (PR #61 squash
`27b64bd`). Phase 2 (resolver extension) SHIPPED + LIVE 2026-06-04 (PR #62 squash
`8d05d95` → infra-reader rebake; Codex `019e8e26` GO, 5-lens adversarial 0 must-fix).
**Phase 3 (checkout build-out) COMPLETE + LIVE-VERIFIED 2026-06-05:** PR1 `#66`→`4f8683a`
(assets bucket + order-events topic + orders-sub) + PR2 `#68`→`7854ff3` (storefront +
orders-worker Cloud Run) both shipped via DriftScribe's OWN author→approve→apply loop;
Track B (mock secret + 2 runtime SAs + pipeline IAM + resource-scoped demo wiring) done;
`/infra/graph` totals **managed=7** (services payment-demo+storefront+orders-worker, buckets
assets+c6e-probe, topic order-events, sub orders-sub; SAs/secret intentionally amber). Codex
completed-work review (thread `019e9134`) — no must-fix. Follow-up approval-page UX PR
`#69`→`90d13e2` (severity-aware approve callout — no red "error" box after a successful
decision — + a clickable `/iac-approvals/<N>` link in the decisions rail + accurate prose;
Codex-reviewed, no must-fix) SHIPPED. Full Phase-3 execution record (every command + HCL
block) in `docs/plans/2026-06-04-phase3-checkout-buildout-execution-plan.md`. Phases 4–5
(edges, go-bigger) + the deferred drift-demo finale remain future work. Original design
Codex-reviewed 2026-06-03; findings folded in.
**Author:** operator + Claude (brainstormed interactively 2026-06-03)

## 1. Goal & narrative

Grow DriftScribe's *managed* infrastructure from a single adopted Cloud Run
service (`payment-demo`) into a small but **real, multi-resource system**, and
add a live **infrastructure graph** to the operator UI that auto-refreshes as
infra changes are applied.

Two intertwined deliverables:

- **A — Infra graph feature** (code): a collapsible "Infrastructure" panel in
  the Svelte SPA that renders the project's current resources, colored
  managed-in-IaC vs. drift, refreshed when an apply lands.
- **B — Checkout demo build-out** (live infra, dogfooded): provision the new
  resources *through DriftScribe's own authoring → approve → apply loop*, so
  each addition is itself a live end-to-end test and visibly lands in the graph.

**Narrative — a serverless checkout platform.** `payment-demo` is a payments
service, so the coherent story is a small e-commerce checkout it sits inside:

| Resource | Role | Intended wiring (edges) |
|---|---|---|
| `storefront` (Cloud Run) | customer-facing web; takes orders | → invokes `payment-demo`; → publishes `order-events`; → reads `assets` |
| `payment-demo` (Cloud Run, **exists**) | charges the card | → reads `payment-api-key` secret |
| `orders-worker` (Cloud Run) | fulfils orders async | ← consumes `orders-sub` |
| `order-events` (Pub/Sub topic) + `orders-sub` (subscription) | order event bus | `storefront` → topic → sub → `orders-worker` |
| `assets` (GCS bucket) | product images / receipts | `storefront` reads |
| `payment-api-key` (Secret Manager) | payment-provider key | exercises counts-only redaction |
| 3× service accounts + IAM bindings | per-service identity | service → SA; `run.invoker`; `pubsub.publisher/subscriber` |

New services run a **stock image** (`gcr.io/cloudrun/hello`) so there is **no
container-build pipeline to stand up**. **NB (see §2):** the *resources*
(buckets, Pub/Sub, Cloud Run services) are agent-dogfoodable, but the **IAM
bindings, service accounts, and the secret are operator-bootstrap** — DriftScribe's
safety controls forbid the agent from authoring them. So the `run.invoker` /
`pubsub.*` edges and the SA/secret nodes come from out-of-band `gcloud`, not the
agent's apply pipeline (§5 Phase 3).

**Why this shape:** it maximizes (1) resource-type diversity (richer inventory),
(2) inter-resource edges (a topology, not confetti), and (3) out-of-band-driftable
knobs (env, scaling, IAM) for drift detection to catch — while staying
scale-to-zero / free-tier.

**"Go bigger later" (not in this build):** global HTTPS load balancer, a real
datastore (Firestore/Cloud SQL), Artifact Registry + a built image, a Vertex/GenAI
component (the ¥159,556 GenAI App Builder credit runs to May 2027). Captured in §7.

## 2. The honest data-source reality (what the code actually gives us)

This is the crux that shapes the whole plan. Verified by reading the code:

- **CAI inventory has NO edges.** `driftscribe_lib/infra_inventory.build_inventory`
  (infra_inventory.py:99-110) returns `by_type` buckets, each
  `{count, declared_in_iac, not_in_iac, sensitive, sample[]}` where `sample` is
  `{name, location, iac, match_confidence}`. The worker's read-mask is only
  `name, asset_type, location` (infra_reader/main.py:54) — deliberately, to avoid
  surfacing sensitive attributes. **There is no relationship/edge data at all.**
  → A graph straight from CAI is a **grouped node inventory**, not a wired DAG.

- **The declared-identity resolver only understands Cloud Run v2.**
  `iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES` (iac_hcl.py:103-104) maps exactly
  `google_cloud_run_v2_service → run.googleapis.com/Service`. Every other resource
  type resolves to `identity=None` and lands in `declared_not_found`
  (cause `identity_unresolved` / `asset_type_not_supported`).
  → When DriftScribe authors a **bucket / topic / subscription / secret / SA**
  into `iac/`, the reader **cannot match it to its live CAI resource**, so the
  graph would mislabel those managed resources as **drift (not-in-IaC)**.
  **The build-out's correctness is coupled to extending this resolver.**

- **Secrets are already safe.** `SENSITIVE_ASSET_TYPES` (Secret, SecretVersion)
  are surfaced as counts only — no `sample`, identity redacted
  (infra_inventory.py:19-22, 101-110, 124-135). The graph inherits this for free.

- **CAI is eventually consistent** (`_FRESHNESS`, infra_inventory.py:24-27) and
  soft-fails to `{"error":"cloud_asset_unavailable"}` at HTTP 200
  (infra_reader/main.py:129-137). The graph must handle lag + the degraded shape.

- **The safety controls forbid the agent from authoring IAM, service accounts,
  or secrets** — this reshapes the build-out (verified, Codex 2026-06-03):
  - **Apply denylist** (`iac_plan_denylist.py:51-52, 238-250`): `iam-change-forbidden-v1`
    denies any `google_*_iam_*` resource PLUS an explicit extras set that includes
    `google_service_account`/`_key`, custom roles, and WIF pools. Enforced on the
    *plan*, so it blocks these at propose/apply regardless of authoring mode →
    **IAM bindings and service accounts cannot go through the DriftScribe apply
    pipeline at all; they live entirely out-of-band (gcloud).**
  - **AGENT static gate** (`iac_static_gate.py:89-110`): bans
    `google_secret_manager_secret`(+version, +regional) and the `secret_data`
    attribute in AGENT mode → **the agent cannot author the secret; the operator
    bootstraps it** (the agent may still *reference* an existing secret by id in a
    Cloud Run `--set-secrets`/`value_source` binding — that's allowed).
  - **Buckets** are denied only on a *protected* (state/artifact) name
    (`iac_plan_denylist.py:427-481`); **Pub/Sub is unrestricted.**
  - These are deliberate, central safety features (no privilege escalation, no
    agent-minted secrets). **We do NOT relax them for the demo.** Instead the
    build-out splits into an agent-dogfoodable track and an operator-bootstrap
    track (§5 Phase 3), and the resulting managed-vs-drift coloring *is* the
    demo: agent-applied resources render green, out-of-band SAs/secrets amber.

- **The infra-reader's declared set is BAKED INTO ITS CONTAINER.** It parses
  `/app/iac` from the image (`infra_reader/main.py:47`, default `IAC_DIR=/app/iac`;
  baked via its Dockerfile). After an IaC PR merges + applies, `/describe` keeps
  comparing CAI against the *old* baked `iac/` until `driftscribe-infra-reader`
  is rebuilt + redeployed. → **Every IaC change that adds a managed resource needs
  an infra-reader rebake/redeploy before the graph will color that resource as
  managed** (otherwise it shows as drift). This is an operational step per change.

**Consequence:** the pretty edged preview shown during brainstorming is a **v2**
target, not v1. v1 is an honest grouped node graph; edges + correct labeling for
the new resource types require new code. The plan below phases this explicitly.

## 3. Decisions locked during brainstorming

- **Data source:** CAI inventory (`infra_reader /describe`) — whole-project,
  drift-aware, redaction-safe by construction. (Not tofu state: plaintext
  secrets + KMS + widens the sole-mutator. Not the single-service reader: raw
  inline env values.)
- **Placement:** collapsed "Infrastructure" panel with a glanceable drift badge;
  Mermaid (~500KB) lazy-`import()`-ed only on first expand → 0 KB if never opened.
- **Naming:** call it a **"resource map" / inventory graph**, NOT "topology",
  until Phase 4 edges exist (Codex nit — node-only ≠ topology).
- **Generation split:** server returns a **structured typed DTO** (nodes +
  managed/drift flags; edges empty until Phase 4); the Svelte component composes
  the Mermaid source client-side (`securityLevel:'strict'`, escaped labels).
- **Refresh:** the existing `/decisions` reload is NOT a sufficient change signal
  — the SPA only calls `loadDecisions()` on mount + after a chat turn
  (`App.svelte:93,213,293`), and approvals happen in a separate page/tab. So
  Phase 1 adds a real trigger: a **manual "refresh" affordance + a focus/visibility
  refresh + light polling while the panel is open**, and — because CAI lag means
  a just-applied resource often isn't indexed yet — **delayed re-fetches (now,
  +10s, +30s, +60s)** after an `applied` `iac_apply` decision, all under the
  visible "CAI is eventually consistent — may lag" note. No new push channel, no
  touching the tofu-apply worker.
- **Endpoint:** new `GET /infra/graph` on the coordinator (token-guarded,
  `no-store`), proxying `infra_reader` (the SPA can't reach the internal-ingress
  worker directly).
- **CSP:** no change. The SPA shell (`/ui/transparency`) has no CSP, so Mermaid's
  runtime `<style>`/SVG injection works. Mermaid stays OUT of the Jinja
  `/approvals` + `/iac-approvals` pages whose `style-src 'self'` would break it.
- **Build-out method:** dogfood via DriftScribe's authoring→approve→apply loop;
  bootstrap by hand only what the agent can't author yet, logging each gap.
- **Scope order:** checkout system on the **current app** first; go-bigger later.

## 4. Security constraints (non-negotiable)

Mirrors the `decision.ts` allowlist discipline (the decision doc is returned RAW
from `/trace`; the UI renders only a curated allowlist):

1. **Never feed the graph from raw tofu state or the single-service reader's
   `env`** — both carry plaintext secrets. CAI inventory only.
2. **Secrets stay counts-only** — never a secret name/value as a node label
   (inherited from `infra_inventory`; assert with a test).
3. **Escape every Mermaid label**; `securityLevel:'strict'`; never `'loose'`.
   Resource names flow into labels — treat as untrusted.
4. **Allowlist the response fields** server-side — the `/infra/graph` payload is
   a deliberately-shaped topology, never a passthrough of arbitrary inventory keys.
5. **No new write IAM, no touching the sole-mutator.** Pull-only via the existing
   read-only `infra_reader` (cloudasset.viewer + serviceUsageConsumer).
6. Add a Python test (mirroring `decision.test.ts`) asserting a planted
   secret/credential never appears in the `/infra/graph` payload.
7. **Resource names are sensitive-ish** (Codex): bucket / service / topic names
   leak project + customer intent. The graph already keeps secret *types* to
   counts-only; for everything else, names are shown (they're operator-only,
   token-gated, and CAI already returns them to the existing inventory tool), but
   the DTO is a deliberate allowlist — no raw inventory passthrough.

## 5. Implementation plan (phased)

Each phase ships independently, is Codex-reviewed, and follows the standard gates
(ruff, pytest, svelte-check, vitest, vite build, smoke). All frontend changes
follow the established component patterns (`DecisionSummary.svelte`, scoped
`<style>`, `$props/$derived`, `.ds-card`).

### Phase 1 — Resource-map v1 (grouped node graph, current infra)
*Code only. Visualizes whatever exists today; immediately useful. NODE-ONLY — no edges.*

- **Backend:** `GET /infra/graph` in `agent/main.py` — `Depends(verify_token)`,
  `Cache-Control: no-store`, calls `worker_client.call("infra_reader", {})`,
  transforms the inventory into a typed topology DTO (nodes grouped by
  `asset_type`, each `{id, label, asset_type, managed: bool, location}`; secret
  types → a single counts-only node), passes through `freshness_caveat` and the
  `cloud_asset_unavailable` degraded shape. **No edges yet.**
- **New pure module** `driftscribe_lib/infra_graph.py` — `build_graph(inventory)
  → {nodes, edges:[], groups, caveat, degraded}`. Pure + unit-tested (incl. the
  secret-never-leaks assertion, degraded-shape handling, empty inventory).
- **Frontend:** `frontend/src/lib/infra_graph.ts` (typed DTO + a pure
  `toMermaid(topology)` composer, vitest-tested for label escaping + grouping);
  `frontend/src/components/InfraDiagram.svelte` (collapsed `.ds-card` panel,
  drift badge from `not_in_iac` count, `await import('mermaid')` on first expand,
  `securityLevel:'strict'`); render in `App.svelte` `#chat-area`; add `mermaid`
  to `frontend/package.json`.
- **Refresh wiring (all five §3 triggers):** re-fetch on expand; a manual refresh
  button; a focus/`visibilitychange` refresh while open; **light polling
  (~45s) while open**; and delayed re-fetches (now/+10s/+30s/+60s) after an
  `applied` `iac_apply` decision is observed (or, if observed while collapsed,
  on the next expand), to ride out CAI lag — all behind the visible freshness
  note (see §3).
- **Tests:** pytest for the endpoint (auth, no-store, degraded soft-fail, secret
  redaction); vitest for `infra_graph.ts`; a smoke test (mock `/infra/graph`,
  expand panel → graph renders, drift badge shows).
- **Deploy:** standard two-step coordinator deploy (build → promote pinned
  traffic — see `coordinator_deploy_traffic_pinning`).

### Phase 2 — Extend the declared-identity resolver (correct labeling)
*Pure lib. Prereq for the build-out to color the agent-managed resources correctly.*

Extend `iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES` + per-type identity templates for
the agent-dogfoodable types. **The identity string must match the CAI name exactly
after `normalize_cai_name` — Codex flagged real traps here, so each is test-driven
against a real CAI-name fixture:**

- `google_storage_bucket` → `storage.googleapis.com/Bucket`. CAI name is
  `//storage.googleapis.com/<BUCKET>`, so `normalize_cai_name` → **bare `<BUCKET>`**
  (NOT `projects/_/buckets/<BUCKET>`). Identity template = the bucket name.
- `google_pubsub_topic` → `pubsub.googleapis.com/Topic`, identity
  `projects/<PROJECT_ID>/topics/<TOPIC>`.
- `google_pubsub_subscription` → `pubsub.googleapis.com/Subscription`, identity
  `projects/<PROJECT_ID>/subscriptions/<SUB>`.
- `google_service_account` → `iam.googleapis.com/ServiceAccount`. **Operator-bootstrap
  (not agent-applied), but worth matching so it colors green if the operator chooses
  to declare it in iac/ out-of-band.** CAI may use email OR numeric-id forms; derive
  `<account_id>@<PROJECT_ID>.iam.gserviceaccount.com` and test both.
- `google_secret_manager_secret` → **DEFERRED.** CAI secret names use
  **PROJECT_NUMBER**, not project id; the resolver only has `var.project_id`. Either
  thread the project number through (env on the reader) or **don't claim secret
  matching** (let the secret render as a counts-only node, uncolored). Recommend
  deferring — the secret is operator-bootstrap and the counts-only node is fine.

- **Safe for the static gate:** it imports only `parse_hcl/iter_blocks/unwrap/
  block_label/is_meta_key` (verified) — NOT the supported-types map — so extending
  the resolver does not change gate policy.
- De-dup remains keyed on `(asset_type, identity)` (infra_inventory.py:62); test
  the confidence-keeps-highest path per new type.
- **Redeploy `driftscribe-infra-reader` after this phase** (the resolver is baked
  into its image), else `/describe` keeps using the old resolver.

### Phase 3 — Build out the checkout system (two tracks)
*Corrected after Codex: the safety controls (§2) split this. We do NOT relax them.*

**Track A — agent-dogfoodable (full author → C2 plan → approve → C6 re-bake →
tofu-apply loop; each = one live e2e test):**
1. `assets` GCS bucket (non-protected name)
2. `order-events` Pub/Sub topic + `orders-sub` subscription
3. `storefront` Cloud Run service (stock image; env points at `payment-demo`'s
   URL; references the secret by id via `value_source` — referencing is allowed)
4. `orders-worker` Cloud Run service (stock image; pull-consumes `orders-sub`)

Per Track-A step: ask DriftScribe to author → C2 plan-builder → `/iac-approvals/<pr>`
approve → C6 re-bake (create-class) → tofu-apply → **rebake+redeploy
`driftscribe-infra-reader`** → confirm the node appears, colored green.

**Track B — operator-bootstrap (out-of-band `gcloud`; the agent is FORBIDDEN to
author these — that's the safety boundary, not a gap):**
- 3× service accounts (storefront-sa, orders-worker-sa, …) — blocked by the apply
  denylist (`iam-change-forbidden-v1` + `control-plane-sa`).
- IAM bindings: `run.invoker` (storefront→payment-demo), `pubsub.publisher`
  (storefront→topic), `pubsub.subscriber` (orders-worker→sub), bucket objectViewer,
  secret accessor — all blocked by the denylist.
- `payment-api-key` secret container + version — blocked by the AGENT static gate;
  operator creates it + sets the value out-of-band.

These appear in the graph as **not-in-IaC (amber)** nodes — which is the correct,
honest signal and a *feature demo* of managed-vs-drift. (Service accounts are CAI
assets; IAM bindings generally are not, so they surface as edges in Phase 4 only if
declared in iac/, which here they are not — so IAM edges stay absent. See Phase 4.)

**Drift demo (after the system stands):** bump `storefront`'s scaling/env out-of-band
→ drift detection flags it → DriftScribe authors a corrective PR → apply → graph
re-colors. Exercises the whole loop end-to-end.

### Phase 4 — Graph edges (partial topology, the "pretty" version)
- New edge-extraction over the IaC config (a `driftscribe_lib/infra_graph` edge
  pass over the parsed HCL). **Edge sources are constrained by §2 — IAM is
  out-of-band, so IAM edges are NOT derivable.** Derivable from the agent-authored
  config: `subscription → topic` (the subscription's `topic` field, clean),
  `service → secret` (Cloud Run `value_source.secret_key_ref`, clean), and
  `service → service` (a Cloud Run env value containing another service's URL —
  fuzzy string match, best-effort). Endpoints map via `DeclaredIdentity`
  (needs Phase 2).
- Covers declared-in-IaC resources only; unmanaged/out-of-band nodes (SAs, secret,
  IAM) stay edge-less — honest. `run.invoker` / `pubsub.*` edges are absent because
  that wiring lives in out-of-band IAM, not iac/.
- `toMermaid` upgraded to draw the derivable edges; tests for inference correctness.

### Phase 5 — Go bigger (future, separate plan)
Global LB, datastore, Artifact Registry + built image, Vertex/GenAI. Out of scope.

## 6. Cost

All scale-to-zero / free-tier: 3 Cloud Run services idle ≈ $0, Pub/Sub free tier,
GCS pennies, Secret Manager ~$0.06/secret/mo. **Well under $1/mo idle** — trivial
against the ~$300 marketing credit (expires **2026-07-20**, the natural deadline
for the headline demo) and the ¥159,556 GenAI credit (to May 2027).

## 7. Resolved by Codex review (2026-06-03, thread `019e8cce`)

- **Which resources are agent-dogfoodable vs operator-bootstrap** — resolved in
  §2 + Phase 3: buckets/Pub/Sub/Cloud Run = agent; SA/IAM/secret = operator
  (safety boundary, not relaxed).
- **Resolver identity traps** (bucket bare-name, pubsub paths, secret project-NUMBER,
  SA email/numeric) — resolved in Phase 2 (secret matching deferred).
- **Infra-reader staleness** (baked `iac/`) — resolved: rebake+redeploy step added
  to Phase 2 and every Track-A step.
- **Refresh trigger insufficiency + CAI lag** — resolved in §3 / Phase 1 (manual +
  focus + poll + delayed retries).
- **Naming** — "resource map", not "topology", until Phase 4.

## 8. Remaining operator decision

The only genuinely open call: **is the agent-dogfoodable subset (Track A) a
satisfying enough "live test", given that the SA/IAM/secret wiring (Track B) must
be operator-bootstrap by design?** Recommended: yes — Track A still grows the graph
from 1 box to ~6 nodes across 4 resource types via the real apply loop, and Track B
becomes a deliberate managed-vs-drift teaching moment rather than a workaround.
