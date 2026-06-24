# Infrastructure panel: resource map → resource cards

Date: 2026-06-24
Status: design (approved prototype, light-touch guided order)
Supersedes the Mermaid resource-map view from
[[2026-06-03-driftscribe-checkout-demo-and-infra-graph-design]] §5 Phase 1 and the
three-zone rework in [[2026-06-24-infra-panel-hierarchy]] (hero + legend kept; the
Mermaid map and the standalone adopt zone are replaced).

## Why

The expanded **Infrastructure** panel renders the live inventory as a Mermaid
`flowchart` plus a separate "Unmanaged resources" adopt list. Two problems:

1. **The "graph" is not a graph.** `edges` is always `[]` (Phase 4 never shipped),
   so Mermaid is a grouped node list cosplaying as a diagram. dagre auto-layout
   gives us no control over columns or box width (the user's literal ask: two
   columns, uniform-width type boxes), and it ships ~500 KB of Mermaid on the
   normal path.
2. **Two surfaces for one estate.** The map shows what exists; the adopt zone
   re-lists the unmanaged subset with the action. A reader cross-references.

The user prototyped and approved a **CSS card grid**: one card per resource type,
each resource a row, drift rows carrying an inline **Adopt** button. It is fully
layout-controllable (uniform width, 2-col → 1-col responsive), reads as a modern
product surface, merges "what exists" and "what to adopt" into one place, and
drops Mermaid from the normal path entirely.

Mermaid stays for its **one genuine use**: the PR-preview ghost overlay (dashed
"will be created / destroyed" nodes), which a card grid cannot express. It is
lazy-imported only when a preview is active.

## Scope

Frontend-only. No backend, DTO, or prop changes — the card grid is derived from
the SAME `GET /infra/graph` payload. `frontend/src/lib/infra_graph.ts` (new pure
helpers) + `frontend/src/components/InfraDiagram.svelte` (view) + tests + the one
Playwright smoke that expanded the panel expecting an `<svg>`.

## Layout (normal mode)

```
Infrastructure                         [2 drift]  2/6 managed · 33%   ← summary
├─ hero band (framed)  CoverageMeter ……………………………………  [ Refresh ]   ← zone 1, unchanged
├─ Legend  ● managed  ◐ drift  ○ counts-only   (ⓘ)                  ← zone 2 legend, kept
├─ card grid  (repeat(auto-fill, minmax(208px,1fr)), gap 12px)      ← NEW (was Mermaid + adopt zone)
│   ┌ Storage bucket ───── 1 drift ┐  ┌ Cloud Run service ─ 1 drift ┐
│   │ ● prod-tfstate    managed    │  │ ● storefront     managed     │
│   │ ◐ my-old-uploads  [ Adopt ]  │  │ ◐ payment-demo   [ Adopt ]   │
│   │ ○ _cloudbuild  system-managed│  └──────────────────────────────┘
│   └──────────────────────────────┘  ┌ Secret ───────── counts-only ┐
│   ┌ Pub/Sub topic ──── in sync ┐    │ ○ 2 secrets · hidden          │
│   │ ● order-events  managed    │    └───────────────────────────────┘
│   └────────────────────────────┘
└─ caveat (CAI eventually consistent)                               ← unchanged
```

Preview mode is unchanged: the banner + the lazy Mermaid ghost map render in place
of the card grid (the grid is gated on `!previewActive`).

## Card model — pure helpers in `lib/infra_graph.ts`

Keeps the component thin and unit-testable (same precedent as `toMermaid`,
`adoptRows`). The existing `adoptRows`/`adoptGroupRank`/`adoptPrefill` helpers stay
(still used; `adoptGroupRank` is reused here).

```ts
export type ResourceRowStatus = 'managed' | 'drift' | 'control_plane';

export interface ResourceCardRow {
  nodeId: string;          // each-key (unique, server-assigned)
  label: string;           // UNTRUSTED resource name → Svelte text interpolation only
  status: ResourceRowStatus;
  adoptable: boolean;      // status==='drift' && group.adoptable===true && !control_plane
  prefill: string;         // adoptPrefill(...) only when adoptable, else ''
}

export interface ResourceCard {
  assetType: string;       // each-key — UNIQUE (label is not: two "Project" types exist)
  label: string;
  sensitive: boolean;      // counts-only secret card (rows === [])
  count: number;
  managed: number;
  drift: number;
  rows: ResourceCardRow[];
  hiddenUnmanaged: number; // max(0, drift − unmanaged rows shown) → "+N more" trailer
  rank: number | null;     // adoptGroupRank(group)
}

export function resourceCards(graph: InfraGraph): ResourceCard[];
export function startHereAssetType(cards: ResourceCard[]): string | null;
```

**`resourceCards(graph)`**
- `graph.degraded` → `[]`.
- Per group:
  - **sensitive** → `{ sensitive:true, rows:[], count, managed, drift, hiddenUnmanaged:0, rank:null }`
    (counts-only; the DTO carries no per-secret node by design, so no name can leak).
  - **non-sensitive** → one row per node:
    - `managed` → `status:'managed'`, not adoptable.
    - unmanaged + `control_plane` → `status:'control_plane'`, not adoptable (denylist-refused).
    - unmanaged + not control-plane → `status:'drift'`, `adoptable = group.adoptable===true`,
      `prefill` composed only when adoptable.
  - `hiddenUnmanaged = max(0, group.drift − (rows where status!=='managed').length)` —
    parity with today's adopt trailer (counts only the unmanaged delta, never
    truncated managed nodes; Codex 019eb572 round-2 invariant).
- **Sort (light-touch guided order):** stable sort by `(tier, rank ?? +Infinity)`:
  - `tier 0` = non-sensitive with `drift > 0` (actionable),
  - `tier 1` = non-sensitive, `drift === 0` (in sync),
  - `tier 2` = sensitive (counts-only).
  - Within tier 0, `adopt_rank` orders (rank 1 first); unranked keep server order
    (stable). Net effect: drift cards first (guide order), then in-sync, then
    counts-only — matching the approved prototype.

**`startHereAssetType(cards)`** — first card with `rank != null` AND an adoptable
row → its `assetType`, else `null`. Parity with today's `startHereAssetType`
derived: a ranked group whose every drift row is control-plane cannot claim the
chip. No chip when the server sent no ranks (stale coordinator).

## Component (`InfraDiagram.svelte`)

- New deriveds: `cards = resourceCards(graph)` (when `graph && !degraded`),
  `startHere = startHereAssetType(cards)`. Remove `adoptGroups`,
  `adoptShownTotal`, `hasAdoptRows`, old `startHereAssetType`. Keep `clickAdopt`
  (reused by the card Adopt buttons), `renderable`, all preview/refresh machinery.
- **Mermaid only in preview.** Guard render call-sites: `refresh()` renders the
  diagram only `if (open && previewActive)`; `exitPreview()` just clears `svgHtml`
  (the grid takes over — no re-render). `fetchOverlay` (preview-only) still renders.
  Net: the normal path never imports mermaid.
- **View order:** preview banner (if preview) → hero → error → legend (live and/or
  ghost keys) → `{#if previewActive && svgHtml}` Mermaid map `{:else if !previewActive
  && graph && !degraded && renderable}` card grid `{:else if … !renderable}` empty
  note → caveat.
- **Card markup:** header (`infra-card-type` label + a `infra-card-badge` pill:
  `N drift` warn / `in sync` neutral / `counts-only` neutral); body rows
  (`infra-card-row`) with a status dot, the name, and either an Adopt button
  (`card-adopt-btn`, disabled on `adoptDisabled`, fires `clickAdopt(row.prefill)`),
  a `managed` tag, a short control-plane note (`card-control-plane`, keeps the word
  "denylist", claims no ownership), or a "not adoptable" tag (`card-not-adoptable`);
  a counts-only line (`card-counts-only`) for sensitive cards; a `card-trailer`
  ("+N more unmanaged … not shown") when `hiddenUnmanaged > 0`. The top adoptable
  card's header carries the `card-start-here` chip.
- **a11y / security:** names reach text interpolation + the chat input only (no
  HTML sink) — unchanged from the adopt list. Dots are decorative; tags carry the
  meaning. Adopt buttons keep the `adoptDisabled` title + swallow.

## CSS

Port the prototype tokens verbatim (all `--ds-*`, already in `tokens.css`).
`.infra-cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(208px,1fr)); gap:var(--ds-sp-3); }`.
Cards: `--ds-surface` + `--ds-border` + radius; header on `--ds-surface-2`; drift
rows tinted `--ds-warn-surface`; Adopt button on the `--ds-ok-*` tokens. Remove the
`.infra-diagram` normal-path styling concerns (kept for the preview svg) and the
entire `.infra-adopt*` block.

## Tests (TDD)

New lib unit tests (`infra_graph.test.ts`): `resourceCards` row mapping
(managed/drift/control-plane/not-adoptable), sensitive counts-only card,
`hiddenUnmanaged` delta, tier+rank ordering, stale-coordinator (no rank) server
order, `startHereAssetType` (top ranked adoptable, control-plane skip, none when
unranked).

Rewrite the adopt-* `InfraDiagram.test.ts` blocks against the card grid
(`infra-card`, `infra-card-row`, `card-adopt-btn`, `card-control-plane`,
`card-start-here`, `card-counts-only`, `card-trailer`) preserving every behavior:
managed rows present (not absent now — cards show them), Adopt prefill string,
disabled swallow, control-plane suppression, duplicate-label no-crash, ordering,
stale-coordinator. Update the preview-exit test (svg cleared, no re-render; the
in-flight-overlay-after-exit guard stays). Keep coverage/hero/legend-help/
preview-activation/unavailable/retry/refresh-both/focus/no-preview/degraded+overlay/
coalescing/onGraph tests. Update `transparency.smoke.ts` + `fixtures.ts`
(`infraDiagram`→`infraCards`): expand shows the cards (names + "1 secret"
counts-only), not an `<svg>`.

## Out of scope / deferred

- Verbose guided-order prose (per-type hints, the order-note paragraph) — dropped
  per the light-touch decision; the chip + drift-first ordering carry the steer.
- Edges / real topology (still no server `edges`).
- The global `adopt-count` badge — redundant now (hero subline carries the global
  drift number, per-card pills carry per-type).
