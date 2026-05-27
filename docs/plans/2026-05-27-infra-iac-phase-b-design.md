# Phase B — Whole-Project Infra Reader (Design)

**Status:** design, pre-implementation
**Date:** 2026-05-27
**Initiative:** Infra-as-code agent (see `docs/plans/2026-05-27-infra-iac-agent-design.md`)
**Predecessor:** Phase A (OpenTofu layer + static HCL gate), merged to `main` as `f08932f` (PR #5)

---

## 1. Goal

Let the read-only `explore` chat workload answer "explain my whole infra" for the
`driftscribe-hack-2026` GCP project: enumerate the project's **Cloud Asset
Inventory–searchable resources** (CAI is the source; see §4.2 for what that does
and does not cover) and label each one as **declared in IaC** (present in the
committed `iac/` OpenTofu config) or **not in IaC** (not matched to a supported IaC
identity — typically created out-of-band, but see §4.3 on matching limits). This
widens DriftScribe's read aperture from one hard-coded Cloud Run service to the
whole project, without adding any ability to mutate infrastructure.

> **Accuracy caveat (carried into the output payload):** the inventory reflects
> what CAI has indexed, not necessarily ground truth. CAI does not cover every
> resource type and is eventually consistent (it can lag or miss recent changes).
> The reader labels its output `inventory_source: cloud_asset_inventory` with a
> freshness caveat rather than claiming completeness.

## 2. Scope and non-goals

**In scope**
- A new read-only worker `infra-reader` with a single `POST /describe` endpoint.
- Whole-project live enumeration via **Cloud Asset Inventory** (`searchAllResources`).
- An **IaC-declared identity set** derived from the committed `iac/*.tf`
  (resource blocks + `imports.tf` import IDs), used to label each live resource.
- A bounded, structured inventory summary surfaced to chat through a new
  read-only coordinator tool wired into the `explore` workload.
- A shared HCL declared-identity parser, lifted out of `tools/iac_static_gate.py`.
- Operator runbook + Cloud Build wiring (written, **not executed** by the agent).

**Non-goals (deferred to Phase C or later)**
- Reading OpenTofu **state** (`tofu show -json`). State is encrypted, requires a
  KMS-decrypt credential, can contain plaintext secrets, and requires the backend
  to be bootstrapped. True state-based drift reconciliation belongs in Phase C,
  alongside the gated-apply trust machinery.
- IAM-policy enumeration (`searchAllIamPolicies`) — resources only in v1.
- An asset-type drill-down filter argument on the tool (zero-arg summary in v1).
- Any mutation, plan, or apply capability.

## 3. Two deliberate deviations from the A–D design doc

The one-line Phase B sketch in the A–D design doc said:

> `infra-reader` worker (`/describe`) + `tofu_read_project` tool + add to
> `explore`. Merge `tofu show -json` (managed) with GCP describe (unmanaged).

Mapping the real code surfaced that the literal sketch is in mild tension with
its own "Risk: none" label, because `tofu show -json` means handing a
chat-facing read worker a **KMS-decrypt credential** over state that can contain
secrets. Two refinements, both operator-approved during brainstorming:

1. **Managed-set source: HCL, not state.** We derive "declared in IaC" by parsing
   the committed `iac/*.tf`, not by reading state. No `tofu` binary, no state
   bucket read, no KMS decrypt — the worker holds **zero sensitive credential**
   and works before the operator bootstraps the backend. The honest label is
   *declared in IaC* vs *not in IaC* (declared ≠ applied until Phase C reads
   state).
2. **Tool rename: `tofu_read_project` → `read_project_inventory`.** Nothing tofu
   runs, so the old name would mislead. The symbolic name reflects what it does.

## 4. Architecture

```
explore chat ──▶ coordinator (ADK)
                   │  tool: read_project_inventory  (read-only, zero-arg)
                   ▼
            worker_client.call("infra_reader", {})        # audience-bound ID token
                   │  POST /describe   (Bearer ID token, verify_caller allowlist)
                   ▼
          ┌─────────────────────────────────────────────┐
          │ infra-reader worker (Cloud Run, read-only)    │
          │                                               │
          │  1. Cloud Asset Inventory searchAllResources  │  cloudasset.viewer +
          │     (paginated, minimal read_mask)            │  serviceUsageConsumer
          │     → live resource names+types               │  (project, read-only)
          │                                               │
          │  2. parse baked-in iac/*.tf  → declared IDs   │  driftscribe_lib HCL parser
          │     (resource blocks + imports.tf import IDs) │  (no state, no KMS)
          │                                               │
          │  3. label each live resource declared / not   │
          │     → bounded structured summary              │
          └─────────────────────────────────────────────┘
```

### 4.1 The `infra-reader` worker

- New Cloud Run service under `workers/infra_reader/`, mirroring `workers/reader/`:
  `main.py` (FastAPI), `__init__.py`, `tests/`.
- Endpoints: `GET /healthz`, `POST /describe`.
- Auth: same pattern as every other worker — verifies `Authorization: Bearer
  <ID token>` via `driftscribe_lib.auth.verify_caller()` (audience + email
  allowlist).
- Request schema: `extra="forbid"`, **zero arguments** — the LLM cannot override
  the target project (same lock-down philosophy as `workers/reader`'s
  `ReadRequest`).
- The project ID and the `iac/` directory path are read from environment /
  baked-in defaults, never from the request body.

### 4.2 Live enumeration — Cloud Asset Inventory

- Library: `google-cloud-asset` (new dependency).
- Call `AssetServiceClient.search_all_resources(scope="projects/<project>")`,
  iterating **all pages** (CAI caps `page_size` at 500; the client iterator
  handles paging — we still bound our own aggregation, see §4.5). We do not pass
  an `asset_types` filter in v1 (the full CAI-searchable project inventory); we
  aggregate client-side.
- **Explicit `read_mask` — do not accept CAI defaults.** `search_all_resources`
  returns a rich object by default (including `labels`, `tags`, `kms_key`,
  `additional_attributes`, `description`), any of which can carry sensitive data.
  We pass a **minimal** read mask: `read_mask="name,assetType,location"`
  (`displayName` only if it proves necessary and is reviewed). We never return,
  log, or surface raw CAI objects — only the masked fields, re-projected into our
  own summary shape. This is the primary control behind the "no secrets" property
  (see §9 — it is a mitigation, not an absolute guarantee).
- **Coverage is partial by design.** CAI explicitly does not make all resource
  types searchable and is eventually consistent. The reader treats CAI as
  "best-available index," not ground truth, and says so in its output. Resource
  types CAI cannot search simply do not appear; we do not attempt to backfill them
  with per-service describe calls in v1.
- Each masked result yields: `name` (the full `//service/...` resource name),
  `asset_type` (e.g. `run.googleapis.com/Service`), `location`.
- IAM: the worker SA gets **`roles/cloudasset.viewer`** **and**
  **`roles/serviceusage.serviceUsageConsumer`** at project scope — all CAI calls
  require `serviceusage.services.use` in addition to
  `cloudasset.assets.searchAllResources`. (A custom role carrying exactly those
  two permissions is an acceptable tighter alternative.) These are read-only, but
  project-wide — so they are recorded as a **documented, narrow exception** to the
  project's "no blanket project-wide grant" invariant, justified by the
  whole-project read being the entire point of Phase B. The Cloud Asset API must
  be enabled on the project (operator step).

### 4.3 IaC-declared identity set — from HCL, not state

- Source of truth for "declared": the committed `iac/*.tf`, **baked into the
  worker image at build time** (the worker reads its own local `iac/` directory;
  no git or GitHub access needed). The view is as-of-deploy; redeploying on merge
  refreshes it. This is acceptable for a reader.
- Two contributors to the declared identity set, with **explicit confidence
  tiers** (matching is approximate; the output never presents a low-confidence
  guess as fact):
  1. **`import` blocks** (`imports.tf`) — **high confidence.** Each
     `import { id = "..." }` is a concrete GCP resource identifier (e.g.
     `projects/driftscribe-hack-2026/locations/asia-northeast1/services/payment-demo`).
     These are the cleanest identity anchors.
  2. **`resource` blocks** — **derived / lower confidence.** For a **small,
     explicitly-supported set of resource types** (v1: `google_cloud_run_v2_service`),
     a typed, resource-specific resolver builds the identity from the block's
     `project` / `location` / `name` attributes **only when those are statically
     evaluable** (literals, or `var`/`local` defaults the static parser can
     resolve). For unsupported types, or attributes that depend on runtime values,
     the resource is recorded as **"declared, identity unresolved"** — it counts as
     declared-in-IaC at the address level but is not used for live↔declared
     matching.
- **The two contributors are complementary, which de-risks finding (4) below.**
  A resource declared via both an `import` block and a `resource` block matches at
  high confidence; if the operator later removes the import block (Phase A's
  `imports.tf` notes imports are removable after first apply), the resource is
  still declared via its `resource` block at derived confidence. **Recommended
  operator guidance (documented in the runbook): retain import blocks until Phase
  C introduces state-read**, because they give the highest-confidence matching.
- **Matching** live↔declared: normalize both sides to a comparable canonical
  form, then compare. CAI names look like
  `//run.googleapis.com/projects/<p>/locations/<l>/services/<svc>`; import IDs
  look like `projects/<p>/locations/<l>/services/<svc>`. The v1 normalizer strips
  the `//<service>/` scheme prefix and compares the path suffix — **this is known
  to be reliable only for the location/path-style IDs we support (Cloud Run v2)**.
  Provider import IDs come in many shapes (short names, self-links, project-number
  forms, global-vs-regional paths, bucket URLs, IAM pseudo-resources), so the
  matcher is **type-aware**, not a blind string strip: each supported type carries
  its own normalization rule, and unsupported shapes are not force-matched. A live
  resource is **declared** iff it matches a declared identity under that type's
  rule; otherwise **not in IaC**.
- **`declared_not_found`** (declared identities with no live match) is reported
  **with reason metadata**, never as bare "drift": each entry carries
  `source: import_id | derived_resource`, `confidence`, and `possible_causes`
  (e.g. "CAI lag / eventual consistency", "asset type not CAI-searchable",
  "import not yet applied", "name/region format mismatch", "parser could not
  resolve identity"). When the declared target is a **sensitive** type, the
  `identity` field is omitted and `identity_redacted: true` is set (same control
  as the counts-only sample treatment in §4.5) — otherwise a sensitive resource
  name could leak here even though it never appeared in a sample. The system
  prompt instructs chat to present these as *things to check*, not confirmed
  drift.

### 4.4 Shared HCL parser refactor

**This refactor sits under the merged Phase A gate, so it is regression-sensitive
and must be done conservatively.**

- **Move only the generic, policy-free parsing primitives** out of
  `tools/iac_static_gate.py` into a shared module
  (proposed: `driftscribe_lib/iac_hcl.py`): the dunder-metadata handling
  (`_is_meta_key`), quote/value unwrapping, and "load+parse a directory of `*.tf`
  files." **Gate *policy* semantics stay in `tools/iac_static_gate.py`** — do not
  pull any allow/deny rule logic into the shared (more permissive, runtime)
  module, and the gate must not become more lenient as a side effect.
- Add the declared-identity extraction (import IDs + typed resource-block
  identities, per §4.3) to the shared module.
- **Golden parity tests first:** before refactoring, capture the gate's current
  parse outputs on the committed `iac/*.tf` (and existing fixtures) as golden
  values; after the refactor, assert byte-for-byte parity. The full existing gate
  suite (`tests/unit/test_iac_static_gate*.py`, incl. the dunder-metadata cases)
  must stay green unchanged.
- `python-hcl2` graduates from a **dev-only** dependency to a **runtime**
  dependency. **This is not sufficient on its own:** worker Dockerfiles install
  runtime deps explicitly, so the new `workers/infra_reader/Dockerfile` must
  install **both** `python-hcl2` **and** `google-cloud-asset` (and whatever else
  the worker imports). The plan's worker task includes a Dockerfile + a container
  import-smoke check so a missing dep fails in CI, not at deploy.

### 4.5 Output shape (bounded, token-safe)

`/describe` returns, and the tool surfaces, a structured summary — never a full
dump:

```jsonc
{
  "project": "driftscribe-hack-2026",
  "generated_at": "2026-05-27T...Z",
  "inventory_source": "cloud_asset_inventory",
  "freshness_caveat": "CAI is eventually consistent and does not cover all resource types; this is a best-available index, not ground truth.",
  "iac_snapshot_sha": "f08932f",      // git SHA of the iac/ baked into this image
  "total_resources": 42,
  "declared_in_iac": 1,
  "not_in_iac": 41,
  "by_type": {
    "run.googleapis.com/Service": {
      "count": 3,
      "declared_in_iac": 1,
      "not_in_iac": 2,
      "sensitive": false,             // if true, sample is omitted (counts only)
      "sample": [
        {"name": "payment-demo", "location": "asia-northeast1",
         "iac": true,  "match_confidence": "high"},
        {"name": "coordinator",  "location": "asia-northeast1",
         "iac": false, "match_confidence": null}
      ]
    }
    // ... one entry per asset_type
  },
  "declared_not_found": [
    // {"identity": "...",            // omitted + identity_redacted:true if the
    //                                //   declared target type is sensitive
    //  "asset_type": "run.googleapis.com/Service",
    //  "source": "import_id"|"derived_resource",
    //  "confidence": "high"|"derived",
    //  "possible_causes": ["cai_lag", "not_yet_applied", "format_mismatch", ...]}
  ],
  "truncated": {"per_type_sample": 10}  // sample lists capped at 10/type
}
```

- Per-type `sample` lists are capped (default 10) so the payload stays bounded
  regardless of project size; `count` always reflects the true total.
- **Sensitive asset types are counts-only.** For types whose *names* tend to carry
  sensitive data (e.g. `secretmanager.googleapis.com/Secret*`), the per-type entry
  reports counts and the declared/not-in-iac split but **omits the `sample` list**
  (`sensitive: true`). The matcher still uses the names internally; they just are
  not surfaced to chat.
- `match_confidence` (`high` for import-ID matches, `derived` for resource-block
  matches, `null` for not-in-iac) is carried through so chat never overstates a
  derived guess.
- `iac_snapshot_sha` tells the user which committed revision of `iac/` the
  declared set came from (the image is built at a known SHA), so a stale image is
  detectable.

## 5. Coordinator wiring

All read-only; the `explore` workload's strict-read-only invariant must remain
provably intact.

- **Tool:** add symbolic name `read_project_inventory` to `_TOOL_REGISTRY`
  (`agent/workloads/registry.py`), mapped to a new wrapper
  `read_project_inventory_tool()` in `agent/adk_tools.py` that calls
  `worker_client.call("infra_reader", {})` and returns the summary dict.
- **Workload:** add `read_project_inventory` to `EXPLORE_WORKLOAD_TOOL_NAMES`
  (`agent/adk_agent.py`) and to `workloads/explore/workload.yaml`'s
  `enabled_tool_names`; add `infra_reader` to that workload's wired workers.
- **Worker registry:** add `"infra_reader": WorkerSpec(url_env="INFRA_READER_URL")`
  to `_WORKER_REGISTRY`.
- **worker_client:** add `INFRA_READER_URL` to `_WORKER_URL_ENV` and the
  `/describe` canonical endpoint to `WORKER_ENDPOINTS`
  (`agent/worker_client.py`).
- **Read-only pins:** `read_project_inventory` is disjoint from
  `_MUTATION_TOOL_NAMES`, so `test_explore_workload_is_strictly_read_only()` stays
  green; `infra_reader` is a read worker, so
  `test_explore_workload_wires_no_mutation_worker()` stays green. Update the
  expected tool-inventory pins in `tests/unit/test_coordinator_tool_inventory.py`.
- **System prompt:** extend `workloads/explore/system_prompt.md` to mention the
  new whole-project inventory capability (still read-only framing).

## 6. Error handling and degradation

This is a **read** path, not a fail-closed mutation gate, so failures degrade to
informative messages rather than hard blocks:

- **Cloud Asset API disabled / permission denied** → worker returns a structured
  error payload (`{"error": "cloud_asset_unavailable", "detail": "..."}`); the
  tool surfaces a clear "couldn't read project inventory: <reason>" to chat.
- **`iac/` directory missing or empty** → declared set is empty; every live
  resource is labeled "not in IaC" (correct).
- **HCL parse error in a baked-in `iac/*.tf`** → fail closed *for the declared
  set only* (treat declared set as unknown and say so), but still return the live
  inventory. The CI static gate already prevents un-parseable HCL from landing in
  `iac/`, so this is a defense-in-depth path.
- **Works before operator bootstrap:** because the reader never touches the state
  bucket, KMS, or the backend, it functions whether or not the operator has run
  `setup_iac_backend.sh` or applied anything.

## 7. Testing strategy (TDD)

**Worker (`workers/infra_reader/tests/`):** mock the `google-cloud-asset` client.
- pagination: multi-page `search_all_resources` aggregated to correct totals.
- **read mask:** assert the request object carries the intended minimal mask
  (`name,assetType,location` — protobuf `FieldMask(paths=["name","asset_type",
  "location"])`; the test pins whichever form the installed client requires) and
  that no label/tag/`additional_attributes` field is ever read.
- summary aggregation: counts by type, declared/not-in-iac splits, sample capping.
- declared/undeclared labeling + **confidence:** a live resource matching an
  import ID is `iac: true, match_confidence: "high"`; one matched only via a
  resource-block derivation is `match_confidence: "derived"`; a non-matching one
  is `iac: false, match_confidence: null`.
- `declared_not_found` **reason metadata** (not just presence): each entry carries
  `source`, `confidence`, and a non-empty `possible_causes` enum list.
- **sensitive-type redaction:** a `secretmanager.googleapis.com/Secret*` type
  yields `sensitive: true` with the `sample` omitted; a sensitive declared target
  in `declared_not_found` has its `identity` omitted with `identity_redacted: true`.
- normalization: CAI `//run.googleapis.com/.../services/<svc>` name vs import-ID
  path match correctly; an unsupported-type import ID is **not** force-matched.
- `iac_snapshot_sha` / `inventory_source` / `freshness_caveat` present in output.
- auth: missing/invalid Bearer → 401/403 via `verify_caller`; request body with
  extra fields → 422 (`extra="forbid"`).
- degradation: CAI permission-denied → structured error, not a 500 crash.
- **container import smoke:** the worker module imports cleanly with only the
  Dockerfile-declared deps (catches a missing `python-hcl2`/`google-cloud-asset`).

**Shared HCL parser (`tests/unit/`):** declared-identity extraction from a fixture
`iac/` (import IDs + resource addresses); behavior parity for the static gate
after the refactor (existing gate tests stay green).

**Coordinator (`tests/unit/`):** tool wrapper returns the worker summary; registry
resolves `read_project_inventory` + `infra_reader`; updated inventory pins;
`test_explore_workload_is_strictly_read_only()` and
`..._wires_no_mutation_worker()` still pass.

## 8. Operator-side steps (written by agent, NOT executed)

Documented in a runbook (`docs/runbooks/infra-reader.md`), mirroring Phase A's
discipline of leaving live-GCP actions to the operator:

1. Enable the Cloud Asset API (`cloudasset.googleapis.com`) on
   `driftscribe-hack-2026`.
2. Create the `infra-reader` service account; grant **both**
   `roles/cloudasset.viewer` **and** `roles/serviceusage.serviceUsageConsumer`
   (project) — the documented invariant exception (or a custom role with exactly
   `cloudasset.assets.searchAllResources` + `serviceusage.services.use`).
3. Deploy the worker via `infra/cloudbuild.yaml` (new service entry).
4. Set `INFRA_READER_URL` on the coordinator; add the worker SA to the relevant
   allowlists / audience config.
5. (Recommended, documented) retain `iac/imports.tf` import blocks until Phase C
   adds state-read — they give the highest-confidence declared↔live matching.

The agent produces all code + Cloud Build/IaC config; it does not deploy or grant
IAM.

## 9. Trust-boundary check

- The worker is **read-only**: no mutation tools, no write IAM, no KMS, no state.
- It is wired only into `explore` (the read-only workload); the existing
  disjointness pins enforce this in code.
- Its project-wide grants (`cloudasset.viewer` + `serviceUsageConsumer`) are
  read-only metadata and are explicitly documented as a scoped exception. It
  cannot read resource *contents* that CAI does not expose, and it never decrypts
  state.
- Surfaced output is a bounded summary of resource **names/types/locations** — it
  does not fetch container env at all (unlike the single-service reader).
  **"No secrets" is a controlled property, not an absolute guarantee:** resource
  names, labels, tags, and descriptions *can* embed sensitive strings. The
  controls are (a) a minimal CAI `read_mask` so we never even retrieve
  labels/tags/`additional_attributes`/`kms_key`; (b) counts-only treatment of
  sensitive asset types (e.g. Secret Manager) so their names are not surfaced;
  (c) never logging or returning raw CAI objects. Residual risk: a sensitive
  string embedded in a *resource name* of a non-sensitive type could still appear
  in a sample — acceptable for a read-only operator-facing chat, and noted here so
  it is a known, reviewed limit rather than an overclaim.

## 10. Decisions log

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Managed-set from committed HCL, not `tofu show -json` state | No KMS/state credential on a chat-facing read worker; truly risk-none; works pre-bootstrap. State-based drift deferred to Phase C. |
| 2 | Cloud Asset Inventory for enumeration | One API for the CAI-searchable project inventory (not literally every resource — CAI omits some types + lags); minimal code vs per-service describe. |
| 3 | `cloudasset.viewer` accepted as a documented invariant exception | Read-only, metadata-only; whole-project read is Phase B's purpose. |
| 4 | Tool renamed `read_project_inventory` | Nothing tofu runs; name must reflect behavior. |
| 5 | Zero-arg tool, `extra="forbid"` | LLM cannot override project/scope; matches `workers/reader` lock-down. |
| 6 | Bounded summary output (counts + capped samples) | Token-safe regardless of project size. |
| 7 | `iac/` baked into worker image; no git access | Reader needs no live repo; redeploy-on-merge refreshes the declared set. Response carries `iac_snapshot_sha` so staleness is detectable. |
| 8 | `python-hcl2` dev→runtime dependency; refactor moves only policy-free parsing | Worker imports the shared parser at runtime. Gate *policy* stays in `tools/iac_static_gate.py`; golden parity tests guard the merged Phase A gate. Worker Dockerfile installs `python-hcl2` + `google-cloud-asset` explicitly. |
| 9 | CAI is "best-available index," not "every resource" | CAI omits some types + is eventually consistent; output carries `inventory_source` + `freshness_caveat` instead of claiming completeness. (Codex review) |
| 10 | Minimal CAI `read_mask` (`name,assetType,location`); counts-only for sensitive types | CAI defaults return labels/tags/kms/additional_attributes; we never retrieve them. "No secrets" is a stated mitigation, not an absolute. (Codex review) |
| 11 | Grant `serviceUsageConsumer` alongside `cloudasset.viewer` | All CAI calls require `serviceusage.services.use`; viewer alone is insufficient. (Codex review) |
| 12 | Confidence-tiered, type-aware matching; `declared_not_found` carries reason codes | Suffix-strip is only sound for path-style IDs; import-ID matches = high confidence, resource-block derivation = lower; never present a derived guess (or a not-found) as confirmed drift. (Codex review) |
| 13 | Resource-block derivation complements import IDs | If operator removes import blocks (Phase A says they're removable), resources stay declared via resource blocks at derived confidence; runbook recommends keeping imports until Phase C. (Codex review) |

## 11. Out of scope / future (Phase C+)

- `tofu show -json` state read + true drift reconciliation (declared vs *applied*).
- IAM-policy enumeration (`searchAllIamPolicies`).
- Asset-type drill-down filter argument.
- Caching / rate-limit handling if CAI calls become frequent.
