# Unmatched IaC declarations in the Infrastructure panel - Implementation Plan

**Goal:** Make the Infrastructure panel show IaC declarations that were not
matched to a live Cloud Asset Inventory (CAI) resource, while continuing to show
new live resources as unmanaged. Give the operator a read-only investigation
path into Provision, but do not infer a rename, edit IaC, open a PR, migrate
state, or change live infrastructure.

**Architecture:** The infra-reader already computes `declared_not_found`, but
that raw diagnostic is available only to the crew tools and is deliberately
removed from the Infrastructure graph cache. Add a bounded, redaction-safe
projection of resolved, non-sensitive unmatched declarations to the inventory.
The projection survives L1/L2 caching and becomes an optional top-level
`unmatched_declarations` section in `GET /infra/graph`. It stays separate from
`groups[*].nodes`: graph nodes and resource-card rows continue to mean "this is
a live resource." The frontend renders the declarations in a distinct band and
can prefill a new Provision chat asking it to investigate visible same-type
unmanaged resources. The prefill explicitly forbids assuming a rename or making
changes before the operator confirms intent.

**Scope:** Infrastructure panel + Provision prefill only. Anchor is unchanged.
The normal Adopt action, `propose_adoption_tool`, tofu-editor file-write API,
C2 plan builder, denylist, approval flow, and tofu-apply worker are unchanged.

**Tech stack:** Python 3.12 / FastAPI / CAI inventory / Firestore graph cache /
Svelte 5 / TypeScript / Vitest / Playwright.

**Deploy surface:** `build_inventory` runs inside the **infra-reader worker**
(`workers/infra_reader/main.py:401`); the L2 cache and DTO shaping run in the
coordinator. This change spans BOTH Cloud Run services and the deploy order
matters — see Task 8.

---

## Context for the executor

Today the matching pipeline is:

1. `workers/infra_reader/main.py` parses every baked `iac/*.tf` file into
   `DeclaredIdentity` values and inventories the project through CAI.
2. `driftscribe_lib/infra_inventory.py::build_inventory` joins live and declared
   resources by exact `(asset_type, canonical_identity)`.
3. A live resource without an exact declaration becomes `not_in_iac` and is
   shown as drift. A declaration without a live match enters
   `declared_not_found`.
4. `driftscribe_lib/infra_graph.py::build_graph` ignores
   `declared_not_found`, and `agent/main.py::_persist_infra_inventory` removes it
   before writing L1/L2 cache data.

For an out-of-band `A -> B` replacement, the result is therefore visually
one-sided: `A` disappears and `B` appears as unmanaged. The backend knows that
`A` did not match, but the homepage does not show that fact.

### Important semantic boundary

"Not matched in the latest inventory" is not proof of deletion and is not proof
of a rename. CAI is eventually consistent; the IaC snapshot can be stale; a
declaration may not have been applied yet; and a canonical identity can fail to
match because of formatting. Preserve that uncertainty in field names, copy,
tests, and prompts.

The operator, not DriftScribe, decides whether live resource `B` is intended to
replace declaration `A`. This plan only makes the evidence visible and starts a
read-only investigation.

### Invariants that must remain true

- `groups[*].nodes` contains live resources only. Do not insert unmatched IaC
  declarations as nodes or include them in `count`, `managed`, `drift`, coverage,
  adoption rank, or "Start here" calculations.
- Existing graph fields are byte-compatible when there are no visible unmatched
  declarations. New fields are optional and emitted only when non-empty.
- Sensitive asset types remain counts-only. Never surface a secret identity,
  HCL address, or name through the new projection.
- The raw `declared_not_found` diagnostic remains available to
  `read_project_inventory_tool`; do not rename or remove it.
- Raw canonical paths in `declared_not_found` are still not persisted. Cache only
  the new bounded projection.
- Existing Adopt buttons and pending-adoption PR behavior are unchanged.
- Clicking Investigate prefills a new Provision conversation; it never sends the
  message automatically.
- No new mutation tool and no relaxation of import/delete/replace denylist rules.

---

## Data contract

### Infra-reader inventory: safe cached projection

Add an optional top-level inventory field named `unmatched_iac`:

```json
{
  "unmatched_iac": {
    "count": 1,
    "entries": [
      {
        "asset_type": "storage.googleapis.com/Bucket",
        "name": "bucket-a",
        "address": "google_storage_bucket.bucket_a"
      }
    ],
    "truncated": 0
  }
}
```

Rules:

- Derive it from `declared_not_found` entries with a string `asset_type` and a
  resolved string `identity` only.
- Exclude all types in `SENSITIVE_ASSET_TYPES`, even if a malformed caller marks
  them otherwise. Sensitive unmatched declarations are excluded ENTIRELY — no
  counts-only fallback, unlike `by_type`. This is deliberate: a count with no
  identity gives the operator nothing actionable in the panel, and crews still
  see the redacted entries in raw `declared_not_found`. State this in a code
  comment next to the exclusion so it reads as a decision, not an oversight.
- `name` is the last non-empty `/` segment of the canonical identity. It is
  untrusted text and must remain text-only in the browser.
- Include the HCL `address` only when it is a non-empty string.
- Do NOT include `confidence`. The operator UI has no rendering for it, and the
  crews already read the full-fidelity `declared_not_found` (which keeps
  `confidence` and `possible_causes`); a DTO field nothing consumes invites
  cargo-cult preservation later.
- Sort before truncating by `(asset_type, name, address)` so the cap is stable.
- Cap entries at 10 with a module constant. `count` is the total number of
  eligible non-sensitive entries before the cap; `truncated` is
  `max(0, count - len(entries))`.
- Emit `unmatched_iac` only when `count > 0`.
- Do not copy `identity`, project paths, `possible_causes`, or arbitrary raw
  fields into this cached projection.

The existing raw `declared_not_found` remains unchanged beside it in a live
infra-reader response. `agent/main.py::_persist_infra_inventory` continues to
strip the raw field, while retaining `unmatched_iac`.

### `GET /infra/graph`: operator DTO

Add an optional top-level field:

```json
{
  "unmatched_declarations": {
    "count": 1,
    "entries": [
      {
        "id": "u0",
        "asset_type": "storage.googleapis.com/Bucket",
        "type_label": "Storage bucket",
        "label": "bucket-a",
        "address": "google_storage_bucket.bucket_a"
      }
    ],
    "truncated": 0
  }
}
```

`build_graph` must validate the safe inventory projection defensively and never
raise on malformed data. Reapply the sensitive-type exclusion in this layer.
Use `_label_for(asset_type)` for `type_label`; assign deterministic render-only
IDs after validation. Omit the top-level field when no entries survive.

Do not alter `totals`, groups, nodes, edges, or degraded DTOs.

---

## Task 1: Build the safe unmatched-IaC projection

**Files:**

- Modify: `driftscribe_lib/infra_inventory.py`
- Test: `tests/unit/test_infra_inventory.py`

### Step 1.1: Add failing inventory tests

Cover these cases:

1. A resolved non-sensitive declaration with no live match remains in raw
   `declared_not_found` and also produces the safe `unmatched_iac` projection.
2. A matched declaration produces neither raw missing entry nor projection.
3. `identity=None` and `asset_type=None` diagnostics stay raw-only.
4. Secret and SecretVersion declarations never enter `unmatched_iac`; assert
   their identity and address do not occur in serialized projection output.
5. Canonical Cloud Run/Pub/Sub paths become the last-segment display name; a
   bare bucket identity remains unchanged.
6. More than 10 eligible declarations are sorted deterministically, capped,
   and report honest `count`/`truncated` values.
7. With no eligible entries, `unmatched_iac` is absent rather than an empty
   always-present object.

Note: a live infra-reader response deliberately contains BOTH the raw
`declared_not_found` and `unmatched_iac` side by side, so
`read_project_inventory_tool` consumers see both. That duplication is fine; do
not add assertions that the projection is absent from the raw tool path.

### Step 1.2: Implement one pure projection helper

Keep construction close to the existing `declared_not_found` loop so the raw
diagnostic and safe projection cannot disagree about which declarations were
unmatched. Do not change exact-match semantics.

The helper should accept the already-built raw entries or the unmatched
`DeclaredIdentity` objects and return only the allowlisted projection fields.
Avoid generic dictionary pass-through.

### Step 1.3: Run focused tests

```bash
uv run pytest tests/unit/test_infra_inventory.py -q
```

---

## Task 2: Carry the projection through the graph DTO and cache

**Files:**

- Modify: `driftscribe_lib/infra_graph.py`
- Modify: `agent/main.py`
- Test: `tests/unit/test_infra_graph.py`
- Test: `tests/integration/test_infra_graph_endpoint.py`
- Test if fixture shape requires it: `workers/infra_reader/tests/test_describe.py`

### Step 2.1: Add failing graph-builder tests

Pin all of the following:

- Valid `unmatched_iac` becomes `unmatched_declarations` with friendly type
  labels and deterministic `u0`, `u1`, ... IDs.
- Graph totals, group counts, coverage inputs, and live nodes are unchanged by
  unmatched declarations.
- An unmatched declaration whose asset type has no live group is still surfaced
  at the top level.
- Secret types are dropped again as defense in depth.
- Wrong-typed objects, entries, fields, counts, and truncation values are ignored
  or clamped without throwing.
- With no valid entries, the field is omitted and the existing DTO is unchanged.
- A degraded inventory does not surface unmatched declarations.

Do not add unmatched declarations to Mermaid output or preview overlays.

### Step 2.2: Implement defensive DTO shaping

Add a small pure helper in `infra_graph.py`; do not complicate the live group
loop. The top-level nature is deliberate: the UI must be able to show an IaC
declaration even when there are zero live resources of that type.

### Step 2.3: Preserve the projection in L1/L2

Keep `_persist_infra_inventory`'s explicit removal of raw
`declared_not_found`. Update its docstring to say that a bounded safe projection
is retained for the operator UI.

Replace the existing cache regression test
`test_declared_not_found_stripped_from_l2_but_dto_identical` with assertions
that prove:

- raw `declared_not_found` and its canonical full path are absent from the cache;
- `unmatched_iac` survives with only `asset_type`, short `name`, and `address`
  (no `confidence`, `source`, or `possible_causes`);
- secret-type raw identities do not appear in the cache or DTO;
- the first live response and subsequent L2/L1 responses are byte-identical.

This deliberately changes the earlier privacy stance for non-sensitive stale
resource names: the panel already exposes live names for those same asset types,
and this feature requires exposing the unmatched short name. Full canonical
paths and sensitive types remain excluded. Call this out in the code comment and
test name instead of silently weakening the old assertion.

Bump `_INFRA_GRAPH_L2_FORMAT_VERSION` from 3 to 4 and update its comment. Old v3
documents lack `unmatched_iac`; invalidating them prevents the feature from
appearing and disappearing depending on whether an instance served a pre-deploy
L2 record.

### Step 2.4: Run focused backend tests

```bash
uv run pytest \
  tests/unit/test_infra_inventory.py \
  tests/unit/test_infra_graph.py \
  tests/integration/test_infra_graph_endpoint.py \
  workers/infra_reader/tests/test_describe.py -q
```

---

## Task 3: Add frontend types and a deterministic investigation prefill

**Files:**

- Modify: `frontend/src/lib/infra_graph.ts`
- Test: `frontend/tests/unit/infra_graph.test.ts`

### Step 3.1: Add optional TypeScript DTO types

Add `UnmatchedDeclaration` and `UnmatchedDeclarations` interfaces matching the
backend contract, then add optional
`unmatched_declarations?: UnmatchedDeclarations` to `InfraGraph`.

Do not put these entries into `InfraNode`, `ResourceCardRow`, `resourceCards`,
`splitCards`, `scopeTotals`, `adoptRows`, `startHereAssetType`, or Mermaid.

### Step 3.2: Add a pure prefill helper

Implement a helper with a narrow signature, for example:

```ts
investigateUnmatchedPrefill(
  declaration: UnmatchedDeclaration,
  graph: InfraGraph,
): string
```

It should collect visible live nodes with the same `asset_type` that are:

- unmanaged;
- not `control_plane`; and
- non-empty after `normalizeForPrompt`.

These are candidates to inspect, not matches. Preserve graph order, de-duplicate
labels, and cap the rendered candidates at five. Use the existing
`normalizeForPrompt` boundary for every server-provided fragment.

Pin this exact intent in the generated prompt (wording may be line-wrapped, but
tests should assert the important sentences):

```text
Investigate why IaC declares the Storage bucket `bucket-a`
(`google_storage_bucket.bucket_a`) but it was not found in the latest Cloud
Asset Inventory. Visible unmanaged resources of the same type: `bucket-b`.
Determine whether any may be an intended replacement, but do not assume a
rename, change files, or open a PR. Report the evidence and ask me to confirm
the relationship first.
```

When there are no visible candidates, say so explicitly. When the graph sample
contains more than five, append a bounded `and more may exist` clause rather
than listing an unbounded prompt. Do not claim that the visible list is complete;
CAI and per-type sampling are both bounded/eventually consistent.

### Step 3.3: Add unit tests

Test one candidate, multiple candidates, no candidates, control-plane exclusion,
managed-node exclusion, cross-type exclusion, duplicate labels, candidate cap,
and control-character/long-name normalization.

Also add a regression test proving `resourceCards` output, coverage totals, and
Adopt prefill are identical with and without an unrelated
`unmatched_declarations` field.

### Step 3.4: Run the library tests

```bash
cd frontend
npm run test:unit -- tests/unit/infra_graph.test.ts
```

---

## Task 4: Render a separate unmatched-declarations band

**Files:**

- Modify: `frontend/src/components/InfraDiagram.svelte`
- Modify: `frontend/src/App.svelte`
- Modify only if a suitable icon is unavailable: `frontend/src/lib/icons.ts`
- Test: `frontend/tests/unit/InfraDiagram.test.ts`
- Test where the App bridge is already pinned: `frontend/tests/unit/App.test.ts`

### Step 4.1: Add an explicit callback

Add an optional `onInvestigate?: (prefill: string) => void` prop to
`InfraDiagram`. In `App.svelte`, pass the existing `handleAdopt` bridge to both
`onAdopt` and `onInvestigate`.

Reusing the bridge is intentional: it starts a clean chat, selects Provision,
prefills the composer, scrolls it into view, and does not submit. Do not rename
the existing `onAdopt` prop or `handleAdopt` in this scoped change; doing so adds
unrelated churn across TourCard and tests.

Use the same `adoptDisabled` condition for Investigate so it cannot replace a
draft while chat is busy or historical replay is active.

### Step 4.2: Render the band outside live resource cards

On the normal non-preview path, render a full-width band between the coverage
hero and resource-card grids when valid unmatched entries exist.

Also add a separate glanceable summary badge when the panel is collapsed or
expanded: `N IaC unmatched`, with a title such as `Declared in IaC, not found in
the latest inventory`. Give it `data-testid="infra-unmatched-badge"`. This badge
is independent of the existing drift/in-sync badge and must not change
`scope.drift`, coverage, or card ordering. When both exist, show both; do not
collapse them into a combined number.

Suggested copy:

- Heading: `Declared in IaC, not found live`
- Supporting line: `These declarations did not match the latest Cloud Asset
  Inventory snapshot. Index lag or an unapplied IaC change can cause this.`
- Per row: friendly type, declaration label, monospace HCL address, and a
  secondary `Investigate` command with an `eye` or `compass` Lucide icon.
- Truncation: `+N more declarations not shown` when `truncated > 0`.

Do not use a card inside the Infrastructure card. This is an unframed status
band with a divider, not another `.ds-card`. Do not color it as destructive
failure; use the existing neutral/warning tokens with restrained emphasis.

The declaration row is not a live resource row and must not carry a green dot,
amber drift dot, `managed`, `Adopt into IaC`, or pending-adoption marker.

### Step 4.3: Component tests

Add tests that verify:

1. The band is absent for old DTOs and empty optional data.
2. A declaration is rendered with type, label, address, and uncertainty copy.
3. The separate `N IaC unmatched` summary badge appears without changing the
   existing drift/in-sync badge.
4. It does not increase `infra-card-row`, managed, drift, or coverage counts.
5. Its label never appears with `card-managed-tag` or `card-adopt-btn`.
6. Clicking Investigate calls `onInvestigate` with the exact helper output.
7. Disabled state disables the button and suppresses the callback.
8. The existing Adopt button still invokes `onAdopt` with its existing exact
   prefill when the band is present.
9. Truncation copy is honest.

Add stable test IDs:

- `infra-unmatched`
- `infra-unmatched-row`
- `infra-unmatched-investigate`
- `infra-unmatched-trailer`

Avoid changing existing test IDs.

### Step 4.4: App bridge regression

Where `handleAdopt` behavior is already tested, add one test proving the
Investigate callback starts a fresh Provision draft and does not send `/chat`.
Do not duplicate all Adopt bridge tests.

### Step 4.5: Run frontend checks

```bash
cd frontend
npm run check
npm run test:unit -- tests/unit/infra_graph.test.ts tests/unit/InfraDiagram.test.ts tests/unit/App.test.ts
```

---

## Task 5: Document current behavior and explicitly defer reconciliation

**Files:**

- Modify: `README.md`
- Modify: `docs/OVERVIEW.md`

### Step 5.1: README operator-facing acknowledgement

Immediately after the Infrastructure-map drift explanation around
`README.md:99-107`, add a short paragraph covering all four facts:

1. The panel also shows resolved, non-sensitive IaC declarations that were not
   found in the latest CAI snapshot.
2. This is evidence to investigate, not proof of deletion or rename.
3. Investigate opens a Provision draft; it does not change or submit anything.
4. Automatic `A -> B` reconciliation is not supported: DriftScribe does not
   remove the old declaration, migrate state, import the new identity, or relax
   plan safeguards automatically.

Keep Anchor out of this paragraph except to reiterate that this is the
Infrastructure/Provision lane if needed.

### Step 5.2: Overview architecture acknowledgement

Expand `docs/OVERVIEW.md` after the CAI eventual-consistency paragraph around
lines 129-132. Use plain language similar to:

> The map can show both sides of an identity mismatch: an IaC declaration not
> found in the latest inventory and a live resource not declared in IaC. It does
> not call that a rename. The operator can ask Provision to investigate possible
> replacements, but must decide whether the resources are related. Declarations
> of sensitive types (secrets and secret versions) are never listed here, even
> as a count; the crews' raw inventory tool still reports them with the
> identity redacted.
>
> Automatic reconciliation is future work. A safe version would need explicit
> operator confirmation, IaC file deletion/update support, state migration, a
> full C2 plan, and the existing approval gate. Until then, the normal adoption
> tool remains add/import-only and cannot remove the stale declaration.

Do not describe automatic reconciliation as committed roadmap or imply that the
current apply policy permits it.

---

## Task 6: Smoke coverage and visual verification

**Files:**

- Modify: `frontend/tests/smoke/fixtures.ts`
- Modify: `frontend/tests/smoke/transparency.smoke.ts`
- Optional dedicated visual fixture/test if cleaner:
  `frontend/tests/visual/infra-unmatched.visual.ts`

### Step 6.1: Extend the mocked graph fixture

Add one unmatched Cloud Run declaration (for example `storefront-old`) while
keeping the existing unmanaged `storefront` node. Update comments, but do not
change existing totals: unmatched declarations are outside live-resource
counts.

### Step 6.2: Add one end-to-end smoke path

Open the Infrastructure panel, assert both the unmatched declaration band and
the unmanaged live resource are visible, click Investigate, and assert:

- the composer is prefilled;
- Provision is selected;
- the prompt says not to assume a rename;
- no `/chat` request was sent by the click; and
- the normal Adopt button for the live resource still exists.

### Step 6.3: Verify layout at real viewports

Run the built SPA with mocked data and capture/inspect the expanded
Infrastructure panel at:

- desktop: 1280 x 900;
- mobile: 390 x 844.

Check that long resource names and HCL addresses wrap without overlapping the
Investigate button, the band does not become a nested card, and the following
resource grid remains visible and coherent. Use Playwright bounding-box checks
for horizontal overflow in addition to screenshots.

### Step 6.4: Run smoke/build

```bash
cd frontend
npm run build
npm run test:smoke
```

---

## Task 7: Final regression pass

Run:

```bash
uv run pytest \
  tests/unit/test_infra_inventory.py \
  tests/unit/test_infra_graph.py \
  tests/integration/test_infra_graph_endpoint.py \
  workers/infra_reader/tests/test_describe.py -q

cd frontend
npm run check
npm run test:unit
npm run build
npm run test:smoke
```

Also run the repository lint command if the touched modules are included in its
configured scope:

```bash
uv run ruff check driftscribe_lib/infra_inventory.py driftscribe_lib/infra_graph.py agent/main.py
```

---

## Task 8: Deploy both services — worker first, then coordinator

`build_inventory` runs inside the **infra-reader worker**
(`workers/infra_reader/main.py:401`), while the L2 format bump and the graph
DTO shaping run in the coordinator (`agent/main.py`). `driftscribe_lib` is
baked into both images, so this is the PR #195 lesson again: deploy BOTH.

Failure mode if the coordinator ships alone: the v4 format bump invalidates
every cached L2 document, so every `/infra/graph` request falls back to the
slow live path, and the stale worker's responses still lack `unmatched_iac`.
The feature silently never appears while every graph request pays full CAI
latency. Worker-first avoids both halves of that.

### Step 8.1: Deploy the infra-reader worker

Build and deploy the infra-reader worker first. Verify with a direct worker
probe (not through the coordinator cache) that a live response is well-formed:
`unmatched_iac` present with the contract shape when there are eligible
unmatched declarations, absent when the count is zero. Zero is the likely prod
state today; absence with a well-formed rest-of-response is a pass.

### Step 8.2: Deploy the coordinator

Follow the `driftscribe-deploy` skill: the coordinator build lands at 0%
traffic and MUST be followed by the `update-traffic` step. Verify the serving
revision afterward.

### Step 8.3: Verify end-to-end on prod

- `GET /infra/graph` succeeds and, when entries exist, carries
  `unmatched_declarations`; when none exist, the DTO is byte-identical to the
  pre-change shape.
- A first (live-miss) response and a subsequent cached (L1/L2) response agree.
- The SPA Infrastructure panel renders normally; the band appears only when
  entries exist.

---

## Acceptance criteria

- An IaC declaration with a resolved non-sensitive identity and no live CAI
  match appears in a distinct Infrastructure-panel band.
- The collapsed Infrastructure summary shows a separate `N IaC unmatched`
  badge without changing the existing drift/in-sync badge.
- A new live resource remains a normal unmanaged drift row with its existing
  Adopt action.
- The UI never asserts that the two are a rename or replacement.
- Investigate starts an unsent Provision draft containing the declaration and
  visible same-type unmanaged candidates, with explicit confirmation language.
- Managed/drift counts, coverage, cards, adoption ordering, pending-PR joins,
  Mermaid preview, and graph totals are unchanged.
- Raw canonical `declared_not_found` paths and sensitive identities are not
  persisted or exposed by `GET /infra/graph`.
- The safe projection is identical on a live miss, L1 hit, and L2 hit.
- Old graph DTOs without the optional field render exactly as before.
- README and Overview explicitly state that automatic identity reconciliation
  is not supported and is future work requiring operator confirmation and the
  existing plan/approval gates.
- Both services are deployed, worker before coordinator, and the prod
  `/infra/graph` response is verified live-miss and cached (Task 8).

---

## Explicitly deferred: automatic resource identity reconciliation

This plan does **not** implement the second-stage capability discussed with the
operator: generating a combined PR that retires IaC identity `A`, adopts live
identity `B`, and migrates OpenTofu state.

That is a separate major feature because it requires, at minimum:

- a trustworthy relationship signal or explicit operator selection of `A` and
  `B`;
- a new reconciliation-specific tool rather than changing
  `propose_adoption_tool` semantics;
- typed delete/move support in tofu-editor and the GitHub writer;
- state migration semantics that distinguish deletion, replacement, import,
  `moved`, and `removed`/forget behavior;
- narrowly scoped policy changes without weakening the global delete, replace,
  and mixed-import denylist floors;
- a full C2 plan proving the resulting operation and the existing human approval
  gate; and
- rollback/recovery documentation for partial or stale-state outcomes.

Do not prepare those editor, tool, state, or denylist seams in this PR. Premature
"future-proofing" would enlarge the security surface without delivering the
visibility feature. The README/Overview acknowledgement is the only artifact for
that deferred capability in this plan.
