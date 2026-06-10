# Before/After Ghost Nodes on the Infra Diagram (ClickOps Wave 2, item 6)

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> One implementer subagent per task, two-stage review (spec → quality), fix
> loops via SendMessage, final whole-branch review before the PR.

**Goal:** An operator looking at a pending IaC change can see it ON THE MAP
before approving: the Infrastructure resource map gains ghost nodes — dashed
green "will be created", amber "will be modified", red "will be destroyed" —
derived from the same integrity-checked `iac_plan_summary` entries that power
the Wave-1 "What this change does" card, plus a "See this change on the
resource map" link from the approval page into the SPA.

**Audience anxiety served (roadmap §item 6, anxiety C):** ClickOps→IaC
migrants can't read HCL. The Wave-1 summary card explains a plan in words;
this item shows the same facts *spatially*, on the map they already use to
understand their estate. The preview must therefore be exactly as trustworthy
as the summary card: derived only from an integrity-checked artifact, never
partial, sensitive names never shown.

**Architecture:** One new advisory read-only endpoint
(`GET /infra/graph/preview?pr=N`) resolves the PR's C2 plan artifact through
the SAME ladder the approval page uses and returns a redaction-safe **overlay
DTO** (per-resource verb + CAI asset type + real resource name). The SPA
composes ghosts client-side in `toMermaid` (the server never emits Mermaid —
existing invariant). `InfraDiagram` gains a preview mode activated by a
`?preview_pr=N` query param the approval page links to.

**Tech stack:** FastAPI route + pure functions in `driftscribe_lib`
(`iac_plan_summary`, `infra_graph`); Svelte 5 + pure TS lib + Mermaid
(existing lazy import); Jinja2 template link. No new dependencies.

---

## Grounding facts (verified 2026-06-10 against `main` @ 8074fe6)

1. **`driftscribe_lib/iac_plan_summary.py`** — `summarize_plan(plan_json) ->
   PlanSummary | None` (never-partial; `None` = no faithful summary).
   `ChangeEntry(verb, rtype, type_label, name, address, location, imported,
   deposed, action_reason, attr_changes, attrs_truncated)`. Verbs:
   `create|update|destroy|replace|import|forget|change`. **`ChangeEntry.name`
   is the HCL block name** (`rc["name"]`), NOT the GCP resource name — e.g.
   `resource "google_storage_bucket" "assets" { name = "driftscribe-assets" }`
   has `name="assets"`. The real GCP name lives in `change.before["name"]` /
   `change.after["name"]` and must be extracted **mask-aware** (the existing
   `location` extraction at `_build_entry` is the exact pattern:
   `isinstance(v, str) and v and not _mask_any(_sub_mask(a_sens, k))`).
   Counts (`n_create`…`n_hidden`) are computed over ALL entries
   pre-truncation; `entries` is capped at `MAX_ENTRIES = 40`.
2. **`driftscribe_lib/infra_graph.py`** — `build_graph(inventory)` groups live
   resources by CAI `asset_type`; per-node `{id, label, asset_type, managed,
   location}`. **Live node `label` is the SHORT name** — the last
   `/`-segment of the normalized CAI name (`infra_inventory.build_inventory`:
   `display = norm.rsplit("/", 1)[-1]`). `SENSITIVE_ASSET_TYPES`
   (`secretmanager.googleapis.com/Secret`, `…/SecretVersion`) are counts-only
   — never a per-resource node, enforced defensively in `build_graph` even
   against a malformed inventory.
3. **`driftscribe_lib/iac_hcl.py`** — `_SUPPORTED_RESOURCE_ASSET_TYPES` maps 5
   tofu rtypes → CAI asset types (cloud_run_v2_service, storage_bucket,
   pubsub_topic, pubsub_subscription, service_account). Secrets are
   DELIBERATELY absent there (identity needs project NUMBER) — that
   constraint is about *identity resolution*, not display grouping, so the
   overlay needs its own (display-only) mapping table.
4. **`frontend/src/lib/infra_graph.ts`** — `toMermaid(graph)` is the pure
   client-side composer: literal-hex `classDef`s (Mermaid can't read CSS
   custom props), `escapeMermaidLabel` (entity codes, 60-char clamp on the
   RAW string), one `subgraph sg<i>` per group, hidden placeholder nodes,
   `idMap` for future edges, `empty[...]` fallback when nothing drew.
5. **`frontend/src/components/InfraDiagram.svelte`** — `call` +
   `appliedEpoch` props; separate `fetchRun`/`renderRun` monotonic guards
   (the Refresh-wedge bug fix — do not merge them); `RefreshScheduler`
   drives `refresh()` (→ `/infra/graph`) on expand / focus / ~45s poll /
   applied-ladder. `renderDiagram(g)` early-returns (`svgHtml=''`) when
   `g.degraded || !hasRenderableNodes(g)`. Mermaid lazy-imported on first
   render, `securityLevel:'strict'`, `htmlLabels:false`.
6. **`agent/main.py`** route facts:
   - `GET /infra/graph`: `Depends(verify_token)`, `Cache-Control: no-store`,
     soft-fail degraded 200.
   - `_resolve_iac_plan(s, pr_number) -> (ref, view)`: GitHub comment lookup
     + GCS fetch + verify; `(None, None)` no config/comment; `(ref, None)`
     unverifiable resolution; never raises.
   - `_iac_artifact_consistent(ref, view, pr_number)`: artifact↔PR pin.
   - The approval GET's gate ladder (lines ~2503–2541): severity `"error"`
     for unverifiable / integrity mismatch / denylist / inconsistent;
     severity `"pending"` for no-plan / token-unset / dry-run / paused.
     **`show_summary = reason_severity != "error" and not resolved_decision`**
     — i.e. the plain-language card (and therefore this preview) SHOWS even
     when approve is suppressed by token-unset/dry-run/pause.
   - Terminal-decision suppression (best-effort, always-200): event key via
     `_iac_event_key(s.github_repo, pr_number, view.head_sha,
     view.generation_metadata)` → `get_state().find_decision_for_event` →
     terminal iff (`apply_status == "applied" and merge_state == "merged"`)
     or `apply_status in {"failed", "failed_state_suspect", "ambiguous"}`.
     Any other recorded status (`waiting_for_rebake`, applied+failed) is
     still actionable → card/preview stay visible.
   - `view.change_summary` is a `cached_property` reading `_plan_json`,
     which `load_plan_view` assigns AFTER construction — safe for any view
     returned by `_resolve_iac_plan`; do not construct `IacPlanView`
     fixtures and read `change_summary` before setting `_plan_json`.
7. **`agent/templates/iac_approval.html`** — the summary card is
   double-gated (route `show_summary` + template re-check of
   `view.unverifiable / integrity_ok / denylist_violations`); three render
   arms: `summary-unavailable` note (s is none), `change-summary-empty`
   (no entries), `change-summary` card.
8. **`agent/auth.py: verify_token`** — CF Access JWT OR
   `X-DriftScribe-Token`; raises HTTPException otherwise (existing
   integration tests pin exact codes; reuse their fixtures).
9. **`frontend/src/App.svelte`** — `call(path, init)` token-aware wrapper;
   `<InfraDiagram {call} {appliedEpoch} />` at line ~359. SPA is served at
   `GET /` (query strings ignored by the server).
10. **Design tokens** (`frontend/src/styles/tokens.css`) — greens
    `#ecf6ef/#bfe3cb/#176b3b`, ambers `#fcf3dc/#ecd79a/#7d5700`, reds
    `--ds-danger #c5303f`, `--ds-danger-ink #9e2531`, `--ds-danger-surface
    #fdeef0`, `--ds-danger-border #f0c2c8`. Mermaid classDefs mirror tokens
    as literal hex (existing precedent in `CLASS_DEFS`).
11. **Cost/rate reality:** each `_resolve_iac_plan` = 1 GitHub comment-list
    call + 2 GCS fetches (~1–2 s). The infra panel polls `/infra/graph`
    every ~45 s while open. **The preview endpoint must never be wired into
    any polling path** — fetch on demand only.
12. **Pause (item 5)** gates mutations only; this is a read-only route — no
    pause rung. Preview availability is deliberately INDEPENDENT of
    pause/dry-run/token-unset (mirrors `show_summary`, see Decision 2).

---

## Settled decisions

### Decision 1 — new endpoint `GET /infra/graph/preview?pr=N`, not an extension of `/infra/graph`

`/infra/graph` stays cheap and pollable (worker-only). The preview hits
GitHub + GCS, so it gets its own route fetched only on operator intent.
`Depends(verify_token)`, `Cache-Control: no-store`, `pr: int` query param
validated `ge=1` (FastAPI 422 below that — the route is token-guarded, so
422 leaks nothing). **Always-200 for every resolvable outcome** (probe-safe
parity with the approval page): not-available conditions return
`{available: false, reason: <machine-token>}`, never 4xx/5xx.

### Decision 2 — availability ladder follows the approval page's artifact gate, with one documented terminal-state divergence

| Condition (in order) | Preview result |
|---|---|
| no GitHub config / no C2 comment / unresolvable artifact (`view is None`) | `available:false, reason:"no_plan"` |
| `view.unverifiable` or `not integrity_ok` or `denylist_violations` or `not _iac_artifact_consistent(...)` | `available:false, reason:"artifact_error"` (one token — the approval page is the diagnosis surface) |
| terminal decision recorded (same set as the GET: applied+merged, or `failed/failed_state_suspect/ambiguous`) | `available:false, reason:"resolved"` |
| `view.change_summary is None` | `available:false, reason:"summary_unavailable"` |
| otherwise | `available:true` + overlay payload |

**Deliberate asymmetries vs the approve gate (each pinned by a test):**
token-unset, dry-run, and PAUSED do **not** block the preview — exactly as
they don't hide the summary card (`show_summary` ignores them). The
decision lookup is best-effort: a raised lookup falls through to "preview
available" (advisory display; same fail direction as the GET's always-200
contract) with a `iac_preview_decision_lookup_failed` WARNING log.

**Deliberate DIVERGENCE from `show_summary` (Codex plan-review must-fix):**
in the approval GET, the terminal-decision lookup only runs when
`can_approve` is already true (`agent/main.py` ~2558), so under
token-unset/dry-run/paused the page shows the summary card even for a
terminally-resolved plan — an accident of ladder ordering, harmless there
because the card is still a truthful description of what the change
does/did. The preview route runs the terminal lookup UNCONDITIONALLY
(after the artifact rungs): a ghost overlay for an already-applied plan
would misrepresent the live map, while the `resolved` copy ("the map below
shows what is live now") is the truthful answer. The parity test encodes
this as INTENDED: for `terminal + token-unset`, `terminal + dry_run`, and
`terminal + paused` fixtures, assert the page's summary region renders
while the preview returns `reason:"resolved"`.

We deliberately DUPLICATE this small ladder in the new route instead of
refactoring the battle-tested approval GET; the relationship is enforced by
tests, not by shared code (a fixture matrix walks both surfaces and asserts
`show_summary`-visibility ⇔ preview-availability agreement everywhere
EXCEPT the documented terminal-divergence rows above).

### Decision 3 — overlay DTO built by pure `plan_overlay()` in `driftscribe_lib/infra_graph.py`

The graph domain owns map-shaped DTOs. Two new module-level functions plus
one mapping table (full code in Task 1):

```
plan_overlay(pr_number, summary)            -> dict   # available:true payload
plan_overlay_unavailable(pr_number, reason) -> dict   # available:false payload
PLAN_RTYPE_TO_ASSET_TYPE: dict[str, str]              # display-only mapping
```

DTO shape (snake_case, mirrors sibling DTO conventions):

```json
{
  "pr_number": 47,
  "available": true,
  "reason": null,
  "counts": {"create": 1, "update": 2, "destroy": 0, "replace": 0,
              "import": 0, "forget": 0, "change": 0},
  "hidden": 0,
  "entries": [
    {"verb": "create", "rtype": "google_pubsub_topic",
     "type_label": "Pub/Sub topic",
     "name": "order-events", "address": "google_pubsub_topic.order_events",
     "asset_type": "pubsub.googleapis.com/Topic",
     "sensitive": false, "location": "asia-northeast1"}
  ]
}
```

- `counts`/`hidden` come from the PlanSummary `n_*` fields (true totals,
  pre-truncation); `entries` from `summary.entries` (already capped at 40).
- `name` is the **real GCP resource name** (new `ChangeEntry.resource_name`,
  Decision 4), `""` when unextractable — the client falls back to `address`.
- `asset_type` from `PLAN_RTYPE_TO_ASSET_TYPE` (else `null` → client places
  the ghost in a "Planned changes" fallback subgraph).
- **Sensitive redaction is RTYPE-aware, not mapping-dependent (Codex
  plan-review must-fix):** a new module constant in `infra_graph.py`

  ```python
  # Plan rtypes whose names/addresses must never reach the map. Mirrors the
  # static gate's SECRET_MATERIAL_RESOURCE_TYPES (drift-pinned in tests):
  # the REGIONAL variants are deliberately unmapped (their CAI asset typing
  # is unverified) but must still redact — redaction keyed only on the
  # mapped asset type would leak a regional secret's block name through the
  # "Planned changes" fallback path.
  SENSITIVE_PLAN_RTYPES = frozenset({
      "google_secret_manager_secret",
      "google_secret_manager_secret_version",
      "google_secret_manager_regional_secret",
      "google_secret_manager_regional_secret_version",
  })
  ```

  and the entry rule is
  `sensitive = e.rtype in SENSITIVE_PLAN_RTYPES or (atype is not None and
  atype in SENSITIVE_ASSET_TYPES)`. A sensitive entry emits
  `sensitive: true` with `name`, `address`, and `location` ALL blanked
  (HCL block names routinely equal the secret_id, so the address leaks
  too); the client renders a name-free ghost ("Secret · will be created").
  Drift pins (test-time imports only — no runtime dependency on `tools/`):
  `SENSITIVE_PLAN_RTYPES >= tools.iac_static_gate.SECRET_MATERIAL_RESOURCE_TYPES`,
  both NON-regional secretmanager rtypes present in
  `PLAN_RTYPE_TO_ASSET_TYPE` mapping into `SENSITIVE_ASSET_TYPES`, and a
  redaction test for EACH of the four rtypes (the regional ones exercise
  the unmapped+redacted path).

`PLAN_RTYPE_TO_ASSET_TYPE` (display-only; conservative, no fuzzy mapping):
the 5 pairs from `iac_hcl._SUPPORTED_RESOURCE_ASSET_TYPES` (a drift-pin test
asserts equality with that table for those 5 keys) plus:

```
google_secret_manager_secret          -> secretmanager.googleapis.com/Secret
google_secret_manager_secret_version  -> secretmanager.googleapis.com/SecretVersion
google_artifact_registry_repository   -> artifactregistry.googleapis.com/Repository
google_firestore_database             -> firestore.googleapis.com/Database
google_compute_network                -> compute.googleapis.com/Network
google_compute_subnetwork             -> compute.googleapis.com/Subnetwork
google_compute_firewall               -> compute.googleapis.com/Firewall
google_eventarc_trigger               -> eventarc.googleapis.com/Trigger
```

IAM member/binding/custom-role rtypes stay unmapped (no 1:1 CAI node) →
fallback subgraph, which is honest. The regional secret rtypes are also
deliberately unmapped (see the redaction block above).

**Honest limitation — red ghosts are forward-looking today (Codex
plan-review):** the C1 denylist's v1 floor hard-denies ALL
`delete`/`forget`/replace actions, and a denylist violation makes the
preview `artifact_error`. So under TODAY'S policy no available preview can
contain destroy/replace/forget entries — red ghosts will not appear on real
DriftScribe-authored plans until the denylist gains an allowlist phase. We
still implement and test all seven verbs: the summary lib already models
them, the policy is explicitly a "v1 floor" expected to loosen, and the
amber `change` verb (unaudited action combos) IS reachable. Do not promise
red ghosts in any user-facing copy beyond the legend keys.

### Decision 4 — `ChangeEntry.resource_name`: additive, mask-aware lib change

New frozen field `resource_name: str = ""` on `ChangeEntry`, extracted in
`_build_entry` by a small helper (full code in Task 1):

- create / import → `change.after["name"]`;
- update / change / forget → `change.before["name"]`, falling back to
  `change.after["name"]` (before is the live name; the fallback covers a
  malformed-but-summarizable before side);
- destroy / replace → `change.before["name"]` only.
- A candidate counts only when it is a non-empty `str` AND its position is
  not sensitive (`not _mask_any(_sub_mask(<side>_sensitive, "name"))`) —
  byte-for-byte the `location` extraction discipline. Anything else → `""`.

The approval-page template keeps rendering `e.name` (block name) — no
template change for existing rows. Some providers store full paths in
`name` (service accounts: `projects/.../serviceAccounts/<email>`); the
CLIENT normalizes with last-`/`-segment matching (Decision 5) rather than
the lib guessing.

**Provider vocabulary expectations (Codex plan-review — accepted, with
fixtures):** `name` is the display attribute for the types we care most
about (bucket = bucket name; pubsub topic/subscription = short name;
cloud_run_v2_service = short name; SA = full path, handled by client
`shortName`; firestore/compute = short name). Types whose primary id lives
elsewhere (`repository_id`, `account_id`, …) may extract `""` or a
computed full path — both degrade honestly to the HCL `address` fallback
on the client. Task 1 adds per-rtype fixtures for the 5 identity-resolver
types + a no-`name` rtype to pin the fallback, and we accept
address-labeled ghosts for the rest (never a wrong name, only a more
technical one).

### Decision 5 — client-side ghost composition in `toMermaid(graph, overlay?)`

Pure extension; `overlay` optional — **omitted ⇒ output byte-identical to
today** (regression-pinned). New exported types `PlanOverlay`,
`OverlayEntry`, plus pure helpers so each rule is unit-testable.

- **Ghost classDefs** (literal hex mirroring tokens, appended only when the
  overlay has entries or hidden > 0):

  ```
  classDef ghostCreate fill:#ecf6ef,stroke:#1f8a4c,color:#176b3b,stroke-width:2px,stroke-dasharray:6 4;
  classDef ghostUpdate fill:#fcf3dc,stroke:#9a6b00,color:#7d5700,stroke-width:2px,stroke-dasharray:6 4;
  classDef ghostDestroy fill:#fdeef0,stroke:#c5303f,color:#9e2531,stroke-width:2px,stroke-dasharray:6 4;
  ```

- **Verb → class + suffix** (constants):

  | verb | class | suffix |
  |---|---|---|
  | create | ghostCreate | `will be created` |
  | import | ghostCreate | `will be imported` |
  | update | ghostUpdate | `will be modified` |
  | change | ghostUpdate | `will change` |
  | forget | ghostUpdate | `will leave IaC management` |
  | destroy | ghostDestroy | `will be destroyed` |
  | replace | ghostDestroy | `will be replaced` |

- **Matching & placement:**
  - `shortName(name)` = last `/`-segment of `entry.name` (handles full-path
    provider names); empty name never matches anything.
  - Target group = the graph group with `group.asset_type ===
    entry.asset_type` (when mapped and the group exists).
  - **update/change/forget/destroy/replace:** if the target group exists,
    is non-sensitive, and has node(s) whose `label === entry.name || label
    === shortName(entry.name)` → **reclass ALL matching nodes**: class
    override to the ghost class and label becomes
    `${escapeMermaidLabel(node.label)} · ${suffix}`. No match (or no
    group, or empty name) → ADD a ghost node (below).
  - **create/import:** always ADD a ghost node (never reclass — a live node
    with the same label would be CAI lag or coincidence; claiming identity
    would be a lie).
  - **Added ghost node label:** `${escapeMermaidLabel(base)} · ${suffix}`
    where `base` = `entry.sensitive ? \`${entry.type_label} (name hidden)\`
    : (shortName(entry.name) || entry.address)`. Placed inside the target
    group's subgraph when it exists; otherwise in a fallback
    `subgraph sgplan["Planned changes"]` where the label is prefixed with
    the type: `${escapeMermaidLabel(entry.type_label)}: ` + base+suffix.
    All dynamic parts go through `escapeMermaidLabel`; the suffix constants
    are trusted literals appended afterwards (they contain no
    escape-relevant chars).
  - **Ghost insertion must work in the counts-only branch too (Codex
    plan-review):** `toMermaid`'s group loop currently splits on
    `group.sensitive || group.nodes.length === 0` — ghosts must be emitted
    in BOTH arms. A create ghost into an existing-but-empty (or capped)
    group, and a sensitive ghost into the counts-only secrets group, both
    render inside that group's subgraph alongside the counts-only
    placeholder. Restructure so ghost emission happens per-group after the
    existing arm logic (it sets `drew = true` either way); a group that was
    previously skipped (zero inner lines) must still emit its subgraph when
    it receives a ghost.
  - `hidden > 0` → one `:::hidden` node in the fallback subgraph:
    `+${hidden} more planned change(s)` (create the subgraph if needed).
  - Ghost nodes set `drew = true` (a degraded/empty live graph with an
    available overlay renders a planned-changes-only diagram — a CAI outage
    must not blind the preview).
- New pure helper `overlayRenderable(overlay)` =
  `overlay.available && (entries.length > 0 || hidden > 0)`.
- New pure helper `overlayCountsLine(counts)` → calm operator phrasing,
  non-zero verbs only, ` · `-joined, in this order:
  `N will be created`, `N will be modified`, `N will be replaced`,
  `N will be destroyed`, `N will be imported`, `N will leave management`,
  `N will change` (create/update/replace/destroy/import/forget/change).
  Zero-entry overlay → `"No infrastructure changes"`.

### Decision 6 — `InfraDiagram` preview mode

New props: `previewPr?: number | null = null`, `onExitPreview?: () => void`.

- **Activation:** `previewPr` is set once at boot (App, Decision 7) and only
  transitions `N → null` (exit). On mount with `previewPr` set: the panel
  renders OPEN (`<details open={open}>`, initial `open = previewPr != null`)
  and the overlay is fetched exactly once. **Do NOT rely on the browser
  firing `toggle` for the initial open attribute:** `onMount` itself must,
  when `previewPr != null`, (a) call `scheduler.open(appliedEpoch)` so the
  focus/poll/applied refresh machinery runs for an initially-open panel
  (otherwise `scheduler.opened` stays false and the open panel never
  polls), and (b) fetch the overlay — guarded so a browser-fired `toggle`
  on the same mount cannot double-fetch the PREVIEW (e.g. fetch the overlay
  only from `onMount`/Refresh/Retry, never from `onToggle`; a
  double-`refresh()` of the cheap graph is harmless under `fetchRun`, and
  `scheduler.open` being called twice just re-runs `onFetch`, which the
  same guard absorbs). Tests pin behavior (open attribute present; exactly
  ONE preview fetch at mount; focus event while open triggers a graph
  fetch — proving the scheduler actually entered the open state), not
  event mechanics.
- **Overlay fetch discipline:** `GET /infra/graph/preview?pr=${previewPr}`
  via the `call` prop, with its OWN monotonic guard (`overlayRun`) — never
  reuse `fetchRun`/`renderRun` (grounding fact 5). Re-fetched ONLY on
  (a) mount-activation, (b) explicit Refresh-button click while preview is
  active, (c) the preview-error Retry button. **Never** from
  `RefreshScheduler` paths (focus/poll/applied-ladder) — pinned by a test
  (dispatch a focus event → only `/infra/graph` is fetched).
- **Preview banner** (top of `.infra-body`, before the toolbar), shown
  whenever preview mode is active, `data-testid="preview-banner"`:
  - line 1 (exact copy):
    `Previewing PR #${previewPr} — dashed nodes show what approving this
    change would do. The live map does not change until the change is
    applied.`
  - line 2 `data-testid="preview-counts"` (only when `available`):
    `overlayCountsLine(counts)`, plus, when `hidden > 0`:
    ` · +${hidden} more not shown`.
  - `Exit preview` button `data-testid="preview-exit"`: clears local
    overlay state, re-renders the diagram WITHOUT ghosts (when open), calls
    `onExitPreview?.()`.
- **Unavailable / error states** (calm `ds-note`,
  `data-testid="preview-unavailable"`; exact copies):
  - `no_plan`: `No pending plan was found for PR #${previewPr} — nothing to
    preview.`
  - `artifact_error`: `The plan for PR #${previewPr} could not be verified,
    so it cannot be previewed. Open the approval page for details.`
  - `resolved`: `PR #${previewPr} has already reached a final outcome — the
    map below shows what is live now.`
  - `summary_unavailable`: `This plan could not be summarized into a
    preview. Review the approval page instead.`
  - unknown reason token (forward-compat): fall back to the
    `summary_unavailable` copy.
  - transport/HTTP/parse failure → `data-testid="preview-error"`:
    `Could not load the change preview.` + `Retry` button
    (`data-testid="preview-retry"`). The live map is unaffected.
- **Rendering:** `renderDiagram` passes the current overlay (only when
  preview active AND `overlayRenderable`) to `toMermaid(g, overlay)`. The
  skip condition becomes: skip iff
  `(g.degraded || !hasRenderableNodes(g)) && !overlayRenderable(overlay)`.
  After overlay state changes (arrival or exit), the open panel re-renders.
  **Template restructure required (Codex plan-review):** the current body
  is one `{#if degraded} … {:else if graph && !renderable} … {:else if
  svgHtml} …` chain, so a degraded note structurally suppresses the
  diagram. Split it: render the degraded note in its OWN `{#if degraded}`
  block, then a separate chain for the diagram region (`{#if svgHtml} …
  {:else if mermaidLoading || loading} …` plus the not-renderable empty
  note only when NOT degraded) — so degraded + ghost-only preview shows
  BOTH the note and the planned-changes diagram, and all existing
  non-preview states render exactly as before (pinned by the existing
  component tests passing unmodified).
- **Legend** gains three ghost keys while preview is active: `will be
  created` / `will be modified` / `will be destroyed`, with dashed swatches
  using REAL tokens (`border: 1px dashed var(--ds-ok-border)` +
  `--ds-ok-surface`; warn pair; `--ds-danger-border` + `--ds-danger-surface`).
  Bare tokens only — NO fallback hexes (repo convention, item-5 review).
- **Accepted limitation (documented, not built):** the overlay does not
  auto-refresh when the plan is superseded or applied mid-session; the
  operator Refreshes or exits. No auto-exit on `appliedEpoch`.

### Decision 7 — App wiring + approval-page link

- **App.svelte:** parse once at init via a new pure helper
  `previewPrFromSearch(window.location.search)` (in
  `frontend/src/lib/infra_graph.ts`): returns the positive-integer value of
  `preview_pr` or `null` (rejects `0`, negatives, non-integers, junk —
  `Number.isSafeInteger` + `> 0`; the raw string must be ALL digits).
  `let previewPr = $state(previewPrFromSearch(...))`. Pass
  `<InfraDiagram {call} {appliedEpoch} {previewPr} onExitPreview={exitPreview} />`.
  `exitPreview()`: `previewPr = null` + remove ONLY the `preview_pr` param
  (preserve any other query params and the hash):
  `const u = new URL(window.location.href); u.searchParams.delete('preview_pr');
  history.replaceState(null, '', u);` — so reload/share doesn't resurrect
  the preview and future params survive.
- **iac_approval.html:** inside the non-empty `change-summary` card only
  (NOT the empty card, NOT the unavailable note — both have nothing to
  preview), directly after the destructive/safe note block and before
  `ds-summary-list`:

  ```html
  <p class="ds-subtle">
    <a href="/?preview_pr={{ pr_number }}" data-testid="preview-map-link">See this change on the resource map →</a>
  </p>
  ```

  `pr_number` is the int route param (safe to interpolate). Root-relative,
  same-origin — no CSP change (the iac CSP governs scripts/styles, not
  links). The card is already double-gated (route `show_summary` + template
  Gate 2), so the link only appears when the preview endpoint would
  actually return a renderable overlay (the one residual divergence — a
  terminal decision recorded between page-load and click — lands on the
  calm `resolved` copy).

### Decision 8 — out of scope (v1)

- Edges/topology (Phase 4 of the infra-graph design) — ghosts are nodes.
- Preview entry points other than the approval-page link + direct URL (no
  rail-row "preview" buttons, no chat-CTA changes — revisit with item 7).
- Attribute-level diffs on the map (the card already shows them).
- Multi-PR simultaneous preview; auto-refresh/polling of the overlay;
  auto-exit on apply.
- Blast-radius copy (item 8) and notifications (item 7).
- Worker changes: none (artifact resolution is coordinator-side, exactly
  like the approval page).

---

## Visual contract

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ▾ Infrastructure                                [1 drift] 13/50 · 26%    │
├──────────────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ Previewing PR #47 — dashed nodes show what approving this change     │ │
│ │ would do. The live map does not change until the change is applied.  │ │
│ │ 1 will be created · 1 will be modified              [Exit preview]   │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│  Resource map · current project                            [Refresh]     │
│  ┌─ Pub/Sub topic ──────────────┐  ┌─ Cloud Run service ─────────────┐   │
│  │ [drift-events]               │  │ [storefront · will be modified] │   │
│  │ [order-events · will be      │  │  (amber dashed)   [orders-worker]│  │
│  │  created] (green dashed)     │  └─────────────────────────────────┘   │
│  └──────────────────────────────┘                                        │
│  legend: managed · drift · counts-only · will be created ·               │
│          will be modified · will be destroyed                            │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Task 1 — backend lib: `resource_name` + `plan_overlay` (implementer: Sonnet 4.6)

**Files:**
- Modify: `driftscribe_lib/iac_plan_summary.py`
- Modify: `driftscribe_lib/infra_graph.py`
- Test: `tests/unit/test_iac_plan_summary.py`, `tests/unit/test_infra_graph.py`

**Step 1 — failing tests, `resource_name`** (`tests/unit/test_iac_plan_summary.py`):

```python
def _rc(verb_actions, *, before=None, after=None, b_sens=None, a_sens=None,
        rtype="google_pubsub_topic", name="t", address=None, mode="managed",
        importing=None):
    rc = {
        "address": address or f"{rtype}.{name}", "type": rtype, "name": name,
        "mode": mode,
        "change": {"actions": list(verb_actions), "before": before, "after": after,
                   "before_sensitive": b_sens, "after_sensitive": a_sens},
    }
    if importing is not None:
        rc["change"]["importing"] = importing
    return rc

class TestResourceName:
    def test_create_uses_after_name(self):
        s = summarize_plan({"resource_changes": [
            _rc(("create",), after={"name": "order-events"})]})
        assert s.entries[0].resource_name == "order-events"

    def test_update_prefers_before_name(self):
        s = summarize_plan({"resource_changes": [
            _rc(("update",), before={"name": "live-name"}, after={"name": "new-name"})]})
        assert s.entries[0].resource_name == "live-name"

    def test_update_falls_back_to_after_when_before_has_no_name(self):
        s = summarize_plan({"resource_changes": [
            _rc(("update",), before={}, after={"name": "n2"})]})
        assert s.entries[0].resource_name == "n2"

    def test_destroy_uses_before_only_never_after(self):
        s = summarize_plan({"resource_changes": [
            _rc(("delete",), before={}, after={"name": "ghost"})]})
        assert s.entries[0].resource_name == ""

    def test_sensitive_name_is_never_extracted(self):
        s = summarize_plan({"resource_changes": [
            _rc(("create",), after={"name": "secret-ish"},
                a_sens={"name": True})]})
        assert s.entries[0].resource_name == ""

    def test_non_string_or_empty_name_yields_empty(self):
        for bad in ({"name": 7}, {"name": ""}, {"name": None}, "not-a-dict", None):
            s = summarize_plan({"resource_changes": [
                _rc(("create",), after=bad)]})
            assert s.entries[0].resource_name == ""

    def test_unknown_after_create_yields_empty(self):
        # name "known after apply": after carries no name value
        s = summarize_plan({"resource_changes": [
            _rc(("create",), after={})]})
        assert s.entries[0].resource_name == ""
```

(Adapt `_rc` to the file's existing fixture helpers if equivalents exist —
do not duplicate an existing builder. `("delete",)` must be whatever tuple
the existing tests use for destroys, i.e. a member of
`DELETE_ACTION_TUPLES`.)

**Step 2 — run, verify FAIL** (`uv run pytest tests/unit/test_iac_plan_summary.py -q` — AttributeError/assert on `resource_name`).

**Step 3 — implement** in `iac_plan_summary.py`:

```python
@dataclass(frozen=True)
class ChangeEntry:
    ...
    resource_name: str = ""   # real GCP resource name (mask-aware; "" if unknown)
```

```python
def _extract_name(side: Any, mask: Any) -> str:
    """change.<side>["name"] for display — only when scalar, non-empty, and
    its mask position is not sensitive (same discipline as `location`)."""
    if isinstance(side, dict):
        v = side.get("name")
        if isinstance(v, str) and v and not _mask_any(_sub_mask(mask, "name")):
            return v
    return ""


def _resource_name(verb: str, change: dict) -> str:
    before = _extract_name(change.get("before"), change.get("before_sensitive"))
    after = _extract_name(change.get("after"), change.get("after_sensitive"))
    if verb in ("create", "import"):
        return after
    if verb in ("destroy", "replace"):
        return before
    return before or after  # update / change / forget
```

Wire into `_build_entry`'s return: `resource_name=_resource_name(verb, change)`.

**Step 4 — failing tests, overlay** (`tests/unit/test_infra_graph.py`):

```python
from driftscribe_lib.iac_plan_summary import summarize_plan
from driftscribe_lib.infra_graph import (
    PLAN_RTYPE_TO_ASSET_TYPE, plan_overlay, plan_overlay_unavailable,
)
from driftscribe_lib.infra_inventory import SENSITIVE_ASSET_TYPES
from driftscribe_lib.iac_hcl import _SUPPORTED_RESOURCE_ASSET_TYPES


class TestPlanOverlay:
    def test_shape_and_counts_passthrough(self): ...
        # build a PlanSummary via summarize_plan over a 2-entry plan
        # (create topic + update bucket); assert pr_number/available/reason,
        # counts == n_* fields, hidden == n_hidden, entries length/order, and
        # each entry's verb/rtype/type_label/name/address/asset_type/
        # sensitive/location

    def test_hidden_reflects_truncation(self): ...
        # 42 create rows -> entries 40, hidden 2, counts["create"] == 42

    def test_sensitive_rtypes_fully_redacted(self): ...
        # parametrized over ALL FOUR secret rtypes (incl. the two REGIONAL
        # variants, which are unmapped): create with after.name + location
        # set -> entry has sensitive True and name == address == location
        # == "" (the regional rows additionally have asset_type None)

    def test_unmapped_rtype_gets_null_asset_type(self): ...
        # google_project_iam_member -> asset_type None, sensitive False

    def test_unavailable_shape(self): ...
        # plan_overlay_unavailable(7, "no_plan") -> available False,
        # reason "no_plan", zero counts, hidden 0, entries []

    def test_resource_name_fixture_per_identity_rtype(self): ...
        # one create row per identity-resolver rtype (bucket/topic/sub/
        # cloud-run/SA) with a realistic after.name; assert the emitted
        # entry "name"; plus one rtype whose after carries NO name ->
        # name "" (client falls back to address)

class TestRtypeMapping:
    def test_iac_hcl_pairs_match(self):
        for rtype, atype in _SUPPORTED_RESOURCE_ASSET_TYPES.items():
            assert PLAN_RTYPE_TO_ASSET_TYPE[rtype] == atype

    def test_secret_rtypes_map_to_sensitive_asset_types(self):
        for rtype in ("google_secret_manager_secret",
                      "google_secret_manager_secret_version"):
            assert PLAN_RTYPE_TO_ASSET_TYPE[rtype] in SENSITIVE_ASSET_TYPES

    def test_sensitive_plan_rtypes_cover_static_gate(self):
        from tools import iac_static_gate
        from driftscribe_lib.infra_graph import SENSITIVE_PLAN_RTYPES
        assert SENSITIVE_PLAN_RTYPES >= iac_static_gate.SECRET_MATERIAL_RESOURCE_TYPES
```

**Step 5 — implement** in `infra_graph.py` (import `PlanSummary` for typing
only if needed — avoid a runtime import cycle; `iac_plan_summary` does not
import `infra_graph`, so a plain import is safe):

```python
# Display-only mapping: tofu resource type -> CAI asset type, used to place a
# planned change in the live map's matching type group. The 5 identity-resolver
# pairs are mirrored verbatim (drift-pinned in tests); the rest are
# display-grouping additions. Unmapped types render in a "Planned changes"
# fallback group client-side — never guessed.
PLAN_RTYPE_TO_ASSET_TYPE: dict[str, str] = {
    "google_cloud_run_v2_service": "run.googleapis.com/Service",
    "google_storage_bucket": "storage.googleapis.com/Bucket",
    "google_pubsub_topic": "pubsub.googleapis.com/Topic",
    "google_pubsub_subscription": "pubsub.googleapis.com/Subscription",
    "google_service_account": "iam.googleapis.com/ServiceAccount",
    "google_secret_manager_secret": "secretmanager.googleapis.com/Secret",
    "google_secret_manager_secret_version": "secretmanager.googleapis.com/SecretVersion",
    "google_artifact_registry_repository": "artifactregistry.googleapis.com/Repository",
    "google_firestore_database": "firestore.googleapis.com/Database",
    "google_compute_network": "compute.googleapis.com/Network",
    "google_compute_subnetwork": "compute.googleapis.com/Subnetwork",
    "google_compute_firewall": "compute.googleapis.com/Firewall",
    "google_eventarc_trigger": "eventarc.googleapis.com/Trigger",
}

# Plan rtypes whose names/addresses must never reach the map (Decision 3).
# Mirrors the static gate's SECRET_MATERIAL_RESOURCE_TYPES (drift-pinned ⊇
# at test time; no runtime import of tools/). The REGIONAL variants are
# deliberately unmapped above but must still redact — keying redaction only
# on the mapped asset type would leak a regional secret's block name
# through the "Planned changes" fallback path.
SENSITIVE_PLAN_RTYPES = frozenset({
    "google_secret_manager_secret",
    "google_secret_manager_secret_version",
    "google_secret_manager_regional_secret",
    "google_secret_manager_regional_secret_version",
})

_OVERLAY_VERBS = ("create", "update", "destroy", "replace", "import", "forget", "change")


def plan_overlay_unavailable(pr_number: int, reason: str) -> dict:
    """The not-available overlay DTO (same shape, empty payload)."""
    return {
        "pr_number": pr_number,
        "available": False,
        "reason": reason,
        "counts": {v: 0 for v in _OVERLAY_VERBS},
        "hidden": 0,
        "entries": [],
    }


def plan_overlay(pr_number: int, summary) -> dict:
    """Reshape a PlanSummary into the redaction-safe map-overlay DTO.

    Sensitive parity with build_graph: a planned change whose type maps into
    SENSITIVE_ASSET_TYPES carries NO name, address, or location — block names
    routinely equal the secret_id, so the address would leak it.
    """
    entries: list[dict] = []
    for e in summary.entries:
        atype = PLAN_RTYPE_TO_ASSET_TYPE.get(e.rtype)
        sensitive = e.rtype in SENSITIVE_PLAN_RTYPES or (
            atype is not None and atype in SENSITIVE_ASSET_TYPES
        )
        entries.append({
            "verb": e.verb,
            "rtype": e.rtype,
            "type_label": e.type_label,
            "name": "" if sensitive else e.resource_name,
            "address": "" if sensitive else e.address,
            "asset_type": atype,
            "sensitive": sensitive,
            "location": "" if sensitive else e.location,
        })
    return {
        "pr_number": pr_number,
        "available": True,
        "reason": None,
        "counts": {
            "create": summary.n_create, "update": summary.n_update,
            "destroy": summary.n_destroy, "replace": summary.n_replace,
            "import": summary.n_import, "forget": summary.n_forget,
            "change": summary.n_change,
        },
        "hidden": summary.n_hidden,
        "entries": entries,
    }
```

**Step 6 — run + gates:** `uv run pytest tests/unit/test_iac_plan_summary.py
tests/unit/test_infra_graph.py -q` PASS → full `uv run pytest -q` (no
regressions — the new dataclass field is additive with a default) →
`uv run ruff check .`.

**Step 7 — commit** `feat(ghost-nodes): Task 1 — ChangeEntry.resource_name + plan_overlay DTO builder`.

---

## Task 2 — backend route + approval-page link (implementer: Sonnet 4.6)

**Files:**
- Modify: `agent/main.py` (new route after `get_infra_graph`; add `Query` to
  the fastapi import)
- Modify: `agent/templates/iac_approval.html`
- Test: `tests/integration/test_infra_graph_preview.py` (new),
  `tests/integration/test_iac_approval_get.py` (link assertions)

**Step 1 — failing tests** (`tests/integration/test_infra_graph_preview.py`
— model the client/monkeypatch setup on `test_infra_graph_endpoint.py` for
auth/headers and on `test_iac_approval_get.py` for the `_resolve_iac_plan` /
view fixtures; reuse their fixture helpers rather than inventing new ones):

- `test_requires_token` — no header → 401 (or the exact code
  `test_infra_graph_endpoint.py` pins; mirror it).
- `test_no_store_header` — happy path sets `Cache-Control: no-store`.
- `test_pr_must_be_positive_int` — `?pr=0` → 422; `?pr=abc` → 422.
- `test_no_plan_when_unconfigured` — `_resolve_iac_plan` → `(None, None)` ⇒
  200 `{available: False, reason: "no_plan", entries: []}`.
- `test_no_plan_when_view_none` — `(ref, None)` ⇒ `no_plan`.
- `test_artifact_error_unverifiable` / `_integrity` / `_denylist` /
  `_inconsistent` — each condition ⇒ `artifact_error` (for `_inconsistent`,
  patch `agent.main._iac_artifact_consistent` to return False).
- `test_resolved_when_applied_and_merged` — decision
  `{"apply_status": "applied", "merge_state": "merged"}` ⇒ `resolved`.
- `test_resolved_when_failed_state_suspect` — ⇒ `resolved` (and `ambiguous`).
- `test_waiting_for_rebake_stays_available` — non-terminal decision ⇒
  `available: True`.
- `test_decision_lookup_failure_is_best_effort` — `find_decision_for_event`
  raises ⇒ still `available: True`.
- `test_paused_preview_still_available` — set the pause flag via the store
  (the `test_pause_gates.py` fixtures show how) ⇒ `available: True` —
  pins the read-route asymmetry.
- `test_dry_run_preview_still_available` — dry-run settings ⇒ available.
- `test_terminal_outranks_pending_gates` — the documented DIVERGENCE
  (Decision 2): terminal decision + paused (and + dry-run, + token-unset)
  ⇒ preview `resolved` while the approval GET still renders its summary
  region — both assertions in one test so the intent is unmissable.
- `test_summary_unavailable` — view with `_plan_json = None` ⇒
  `summary_unavailable`.
- `test_happy_path_matches_plan_overlay` — view whose `_plan_json` is a
  small real plan dict ⇒ body `== plan_overlay(pr, view.change_summary)`.
- Parity matrix: for each condition above that also has an approval-GET
  representation — EXCLUDING the documented terminal-divergence rows
  (covered by `test_terminal_outranks_pending_gates` instead) — assert
  agreement: render `GET /iac-approvals/{pr}` with the same patched
  fixtures and check (`"change-summary"` or `"change-summary-empty"` or
  `"summary-unavailable"` marker present) ⇔ (`available` is True or reason
  == `summary_unavailable`) — i.e. the page shows the summary region
  exactly when the preview resolves past the artifact ladder. Keep this as
  ONE table-driven test.

Link assertions (in `test_iac_approval_get.py`):
- non-empty summary card page contains
  `data-testid="preview-map-link"` with `href="/?preview_pr={pr}"`;
- empty-plan page (`change-summary-empty`) does NOT contain it;
- unverifiable page does NOT contain it.

**Step 2 — run, verify FAIL.**

**Step 3 — implement route** (after `get_capabilities_route`'s section or
directly after `get_infra_graph` — keep the infra-graph routes adjacent):

```python
@app.get("/infra/graph/preview")
def get_infra_graph_preview(
    response: Response,
    pr: int = Query(ge=1),
    _: None = Depends(verify_token),
) -> dict:
    """Advisory map overlay for a pending IaC PR (ClickOps Wave 2 item 6).

    Resolves the PR's C2 plan artifact through the SAME ladder as the
    /iac-approvals GET and reshapes its integrity-checked plan summary into
    the redaction-safe ghost-node overlay DTO
    (driftscribe_lib.infra_graph.plan_overlay). Read-only and advisory:
    always 200 with {available: false, reason} for every not-available
    outcome (probe-safe parity with the approval page); no pause rung
    (pause gates mutations; this mirrors show_summary, which renders the
    summary card even while approve is suppressed by pause/dry-run/token).

    NOT wired into any polling path: each call costs a GitHub comment list
    + two GCS fetches, so the SPA fetches it only on explicit operator
    intent (preview activation / Refresh / Retry).
    """
    response.headers["Cache-Control"] = "no-store"
    s = get_settings()
    ref, view = _resolve_iac_plan(s, pr)
    if view is None:
        return plan_overlay_unavailable(pr, "no_plan")
    if (
        view.unverifiable
        or not view.integrity_ok
        or view.denylist_violations
        or not _iac_artifact_consistent(ref, view, pr)
    ):
        return plan_overlay_unavailable(pr, "artifact_error")
    # Terminal-decision suppression — best-effort, same identity + terminal
    # set as the approval GET; a lookup failure must not take the preview
    # down (advisory display).
    if s.github_repo:
        existing = None
        try:
            _event_key = _iac_event_key(
                s.github_repo, pr, view.head_sha, view.generation_metadata
            )
            existing = get_state().find_decision_for_event(_event_key)
        except Exception:  # noqa: BLE001 — best-effort, advisory route
            log.warning(
                "iac_preview_decision_lookup_failed", extra={"pr_number": pr}
            )
        if existing is not None:
            _st = existing.get("apply_status")
            _ms = existing.get("merge_state")
            if (_st == "applied" and _ms == "merged") or _st in {
                "failed", "failed_state_suspect", "ambiguous",
            }:
                return plan_overlay_unavailable(pr, "resolved")
    summary = view.change_summary
    if summary is None:
        return plan_overlay_unavailable(pr, "summary_unavailable")
    return plan_overlay(pr, summary)
```

Imports: add `Query` to the `from fastapi import …` line; add
`plan_overlay, plan_overlay_unavailable` to the existing
`driftscribe_lib.infra_graph` import.

**Step 4 — template link** (Decision 7 snippet, placed after the
destructive/safe-note `{% else %}` block closes and before
`<ul class="ds-summary-list">`).

**Step 5 — run + gates:** new file + `test_iac_approval_get.py` PASS → full
`uv run pytest -q` → `uv run ruff check .`.

**Step 6 — commit** `feat(ghost-nodes): Task 2 — GET /infra/graph/preview + approval-page map link`.

---

## Task 3 — frontend: overlay composition + preview mode (implementer: Opus 4.8)

**Files:**
- Modify: `frontend/src/lib/infra_graph.ts`
- Modify: `frontend/src/components/InfraDiagram.svelte`
- Modify: `frontend/src/App.svelte`
- Test: `frontend/tests/unit/infra_graph.test.ts`,
  `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1 — failing lib tests** (`infra_graph.test.ts`), per Decision 5:

- `toMermaid(graph)` with NO overlay → byte-identical to current output for
  a representative graph (compute once with the current implementation and
  pin the string, or assert no `ghost` token appears — pin BOTH: identical
  classDef block + no ghost classes).
- ghost classDefs present iff overlay has entries or hidden > 0.
- create entry, mapped type, group exists → new dashed node inside that
  group's subgraph with label `order-events · will be created` and class
  `ghostCreate`; `drew` true.
- update entry matching a live node by exact label → that node's line
  becomes `…["storefront · will be modified"]:::ghostUpdate` (reclassed, not
  duplicated — assert the original `:::managed` line for it is gone and node
  count in the group is unchanged).
- full-path name matches short label (service-account case):
  `entry.name = "projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com"`,
  live label `sa@p.iam.gserviceaccount.com` → reclassed.
- update entry with NO matching node → added ghost in the group.
- destroy entry matching → `:::ghostDestroy` + `will be destroyed`.
- replace → ghostDestroy + `will be replaced`; forget → ghostUpdate +
  `will leave IaC management`; import → ghostCreate + `will be imported`.
- unmapped `asset_type: null` → ghost in `subgraph sgplan["Planned changes"]`
  with the `type_label: ` prefix.
- sensitive entry → label `Secret (name hidden) · will be created`; the
  string `entries[i].name`/`address` (blank server-side anyway) never
  required; assert no other name appears.
- `hidden: 2` → `+2 more planned change(s)`:::hidden in the fallback
  subgraph.
- degraded graph (`groups: []`) + overlay with 1 create → parseable diagram
  containing the Planned-changes subgraph (no `empty[` fallback node).
- escaping: entry name `evil]"x` and type_label with `[` round-trip through
  entity codes (reuse the existing escaping test style).
- multiple live nodes sharing the matched label → ALL reclassed.
- `overlayRenderable`: false for `available:false`; false for available with
  0 entries + 0 hidden; true otherwise.
- `overlayCountsLine`: zero-only → `No infrastructure changes`; mixed →
  exact ` · ` join and order; singular/plural is NOT inflected (counts read
  fine: `1 will be created`).
- **One REAL Mermaid parse check (Codex plan-review):** lazy
  `await import('mermaid')` in the test and run `mermaid.parse(src)` (parse
  only — no render; render needs real SVG measurement jsdom lacks) on
  (a) a graph+overlay composition exercising ghost classDefs +
  reclass + fallback subgraph, and (b) the degraded+overlay ghost-only
  output — pins that `stroke-dasharray` classDefs and `sgplan` are valid
  Mermaid, not just expected strings. If `mermaid.parse` proves
  jsdom-incompatible, STOP and report back (do not silently drop the test).
- `previewPrFromSearch`: `'?preview_pr=47'` → 47; `''` → null; `'?preview_pr=0'`
  → null; `'?preview_pr=-3'` → null; `'?preview_pr=1.5'` → null;
  `'?preview_pr=abc'` → null; `'?preview_pr=00012'` → 12 is NOT required —
  digits-only parse may accept it; pin whichever the implementation does,
  but `'?other=1'` → null.

**Step 2 — run, verify FAIL** (`npm run test:unit -- infra_graph`).

**Step 3 — implement lib** (Decision 5): types `OverlayEntry`, `PlanOverlay`;
constants `GHOST_CLASS_DEFS`, verb→class/suffix maps; helpers
`overlayRenderable`, `overlayCountsLine`, `previewPrFromSearch`,
`shortName` (internal); extend `toMermaid(graph, overlay?)`. Suggested
internal structure: precompute per-group `Map<asset_type, OverlayEntry[]>` +
a `planOnly: OverlayEntry[]` list, then inside the existing group loop apply
reclass/additions; emit the fallback subgraph after the group loop. Keep the
function pure; no new exports beyond the above.

**Step 4 — failing component tests** (`InfraDiagram.test.ts`, following the
existing injected-`call` + jsdom-`Response` pattern):

- `previewPr=47` at mount → details has `open`; exactly ONE fetch to
  `/infra/graph/preview?pr=47` (count by URL); banner text contains the
  exact line-1 copy; counts line rendered from a 1-create overlay.
- exit: click `preview-exit` → banner gone; `onExitPreview` called once;
  (when open) a re-render without ghosts is triggered.
- unavailable reasons → exact copies (all four + unknown-token fallback).
- transport error (call rejects) → `preview-error` + `preview-retry`; click
  retry → second preview fetch.
- Refresh click while preview active → BOTH `/infra/graph` and
  `/infra/graph/preview?pr=47` fetched.
- window focus event while preview active → ONLY `/infra/graph` fetched
  (pins the no-poll discipline).
- no `previewPr` → ZERO preview fetches, no banner, legend has no ghost
  keys (regression).
- degraded graph + available overlay → degraded note AND a rendered
  diagram region are both present (mermaid mocked as in existing tests; if
  the existing suite never renders mermaid in jsdom, assert via
  `toMermaid`-level coverage in Step 1 plus the component's non-empty
  `svgHtml` path with the module mocked — follow the suite's existing
  mermaid handling, do not invent a new mock style).

**Step 5 — implement component + App** (Decisions 6–7). Style notes: banner
is a `ds-note`-style block INSIDE `.infra-body` with its own class
`infra-preview`; legend ghost keys reuse `.infra-key` with new modifier
classes; bare design tokens only.

**Step 6 — run + gates:** `npm run test:unit` PASS → `npm run check` →
`npm run build` → backend untouched but run `uv run pytest -q` once for the
branch state.

**Step 7 — commit** `feat(ghost-nodes): Task 3 — ghost-node overlay composition + InfraDiagram preview mode`.

---

## Final gates (CI-equivalent, run before the PR)

- `uv run pytest -q` (baseline 2303 — expect +~30)
- `uv run ruff check .`
- `cd frontend && npm run test:unit` (baseline 361 — expect +~35)
- `cd frontend && npm run check` (0 errors / 0 warnings)
- `cd frontend && npm run build`

Deploy after merge: coordinator rebake (SPA is baked into the image) +
`update-traffic` (traffic is pinned), live-verify via the run.app URL
(custom domain 302s curl to CF Access): bundle markers
(`preview-banner`, `will be created`, `ghostCreate`, `preview_pr`) +
`/infra/graph/preview?pr=1` → 401 unauthenticated, and with the operator
token → 200 `{available:false, reason:"no_plan"}` (or a real overlay if an
open plan PR exists).

---

## Plan-review record (Codex thread 019eb20d-1354-7d23-a369-c5ffd370b25e)

First round: **NO-GO**, 2 must-fix + 5 should-fix + 1 nit — all folded:

1. **MUST-FIX — parity claim was wrong for terminal+pending-gate combos**
   (the approval GET's terminal lookup only runs when `can_approve`):
   re-specified as a deliberate, documented DIVERGENCE — preview runs the
   terminal lookup unconditionally; parity matrix gains
   `test_terminal_outranks_pending_gates` (Decision 2).
2. **MUST-FIX — regional secret rtypes would leak via the unmapped
   fallback:** redaction is now rtype-aware (`SENSITIVE_PLAN_RTYPES`, all 4
   secret-material rtypes, drift-pinned ⊇ the static gate's set)
   (Decision 3).
3. SHOULD-FIX — red destroy/replace ghosts unreachable under today's
   denylist v1 floor: documented honestly (Decision 3 note); verbs kept.
4. SHOULD-FIX — degraded+overlay needs a template restructure, not just a
   render-skip change: explicit split-the-`{#if degraded}`-chain
   instruction (Decision 6).
5. SHOULD-FIX — ghost insertion must work in the counts-only/empty group
   branch of `toMermaid`: explicit instruction + tests (Decision 5).
6. SHOULD-FIX — at least one real `mermaid.parse` check, not only string
   assertions (Task 3 Step 1).
7. SHOULD-FIX — provider `name` vocabulary varies per rtype: per-rtype
   fixtures + accepted address-fallback documented (Decision 4, Task 1).
8. NIT — `exitPreview` removes only the `preview_pr` param (Decision 7).

Second round: **GO** after three plan cleanups, all folded: parity-matrix
bullet now excludes the terminal-divergence rows explicitly; Task 1's
implementation block now contains the actual `SENSITIVE_PLAN_RTYPES`
frozenset; preview activation no longer relies on the browser firing
`toggle` for an initial `<details open>` (`onMount` explicitly calls
`scheduler.open` + fetches the overlay, with overlay fetches excluded from
`onToggle` so a browser-fired toggle can't double-fetch); Decision 2
heading renamed to match the divergence.
