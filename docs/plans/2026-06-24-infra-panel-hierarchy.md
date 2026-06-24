# Infrastructure-panel visual hierarchy + legend help Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** The expanded "Infrastructure" panel (`InfraDiagram.svelte`) is currently a flat, equal-weight vertical stack (toolbar → coverage meter → diagram → legend → adopt list → caveat). A first-time judge can't tell the headline from the supporting detail, and the managed/drift/counts-only colors are only explained by a tiny `aria-hidden` swatch row. Rework the body into **three visually-distinct zones with clear hierarchy** and add an inline **`HelpHint` ⓘ** that explains the legend colors in words.

**Architecture:** Pure frontend reorganization of one component's markup + CSS. No backend, no DTO, no new props. Reuses the existing `CoverageMeter`, `HelpHint`, and `Icon` components unchanged. `CoverageMeter.svelte` is **not** modified (a parent `:global` margin reset tightens it inside the hero).

**Tech Stack:** Svelte 5 runes + Vite SPA (`frontend/`), Vitest + @testing-library/svelte (`frontend/tests/unit/InfraDiagram.test.ts`).

---

## The three zones (target structure of `.infra-body`)

```
[ preview banner ]        ← unchanged; transient mode overlay, stays above everything
┌─ Zone 1: HERO (framed, --ds-surface-2) ─────────────┐
│  <CoverageMeter> (or degraded note / loading)  [Refresh] │
└──────────────────────────────────────────────────────┘
[ error alert ]           ← unchanged {#if error}
┌─ Zone 2: MAP UNIT ──────────────────────────────────┐
│  <diagram svg>                                        │
│  Legend  🟩managed 🟨drift ⬜counts-only  ⓘ           │
└──────────────────────────────────────────────────────┘
┌─ Zone 3: ADOPT (framed, --ds-surface-2) ────────────┐
│  Unmanaged resources  (N)                            │
│  These exist in your project but are not under IaC…  │
│  • row • row …                       [Adopt into IaC]│
└──────────────────────────────────────────────────────┘
[ freshness caveat ]      ← unchanged; de-emphasized meta at the very bottom
```

## Decisions & non-goals (read before starting)

- **Single column, hierarchy by weight (chosen over tabs / two-column).** Tabs hide the Adopt CTA behind a click (bad for a skim-once judge) and nest a switcher inside an already-collapsible panel. Two-column cramps the variable-length adopt list into a narrow rail and adds responsive breakpoints. A single column with two framed zones bracketing the map gives the hierarchy fix at the lowest risk and keeps the adopt list full-width where it needs the room.
- **Hero band always renders** so the Refresh button is reachable in every state. Its left content is state-dependent: healthy → `CoverageMeter`; degraded → the existing degraded note (moved *into* the hero, keeps `data-testid="infra-degraded"`); loading-with-no-graph-yet → a muted "Loading inventory…" line. The old separate degraded block is removed (folded into the hero) to avoid duplicating the message.
- **The toolbar + its `"Resource map · current project"` caption are removed.** The panel summary already says "Infrastructure"; the caption was redundant. Refresh moves into the hero (top-right). The `infra-refresh` testid is preserved.
- **One legend ⓘ, not three.** A single `HelpHint` placed at the **end** of the legend line (so its inline `flex-basis:100%` panel drops cleanly onto its own row below all the keys). It explains all three colors in one panel. Three per-key icons would add clutter for no clarity gain.
- **Legend loses `aria-hidden="true"`.** The swatch labels ("managed in IaC", "drift (not in IaC)", "counts-only") are now meaningful content paired with a real labelled help button, so they should be in the a11y tree. The color is conveyed by the text label, not by the `::before` swatch alone (the swatch is decorative CSS).
- **Adopt-zone count = `adoptShownTotal`** = `adoptGroups.reduce((a,g)=>a+g.rows.length+g.hiddenUnmanaged,0)`, i.e. the named rows shown **plus** the per-group `+N more` trailers. This is **provably equal to what the zone renders** (a judge can add the visible rows and the trailer numbers and get exactly the badge), so it can never read as "header says 3, I see 2". It equals `totals.drift` in every existing fixture (no sensitive/fully-truncated drift), so the badge and this count agree in the common case; they only diverge when `totals.drift` includes counts-only (e.g. secret) drift that this section deliberately does not list — and in that case the hero's "D not yet in IaC" line carries the global number while this count stays locally exact. (Codex review 019ef9a4 flagged that a raw `totals.drift` here could mislead vs. visible rows and wanted caveat copy; using the locally-exact sum removes the need for any caveat.)
- **Copy voice:** matches the recent de-AI pass (PR #144) — no em dashes; use colons/periods. The legend help text:
  > Every box is a real resource in your project. Green means managed in IaC: it is defined in OpenTofu, so DriftScribe tracks it and can change it through the approval flow. Yellow means drift: the resource exists but is not in any .tf file, so it is outside management. Grey means counts-only: sensitive types such as secrets, shown as a number with no name.
- **No change to:** the diagram rendering pipeline, the preview/overlay machinery, the refresh/scheduler concurrency guards, the adopt-row logic (`adoptGroups`, ranks, hints, control-plane suppression, trailers), `onGraph`, or any data flow. This is markup grouping + CSS + two copy additions only.
- **Out of scope:** per-node tooltips on the diagram (blocked by Mermaid `securityLevel:'strict'`/`htmlLabels:false`), smart open/collapse of the adopt zone, and any backend/worker change.

---

## Task 1: Hero band (Zone 1) — fold coverage + degraded + Refresh into one framed header

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte` (markup: replace `.infra-toolbar` + standalone `CoverageMeter` + standalone degraded block with one `.infra-hero`; CSS: add `.infra-hero*`, drop `.infra-toolbar`/`.infra-caption`)
- Test: `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1 — failing tests.** Add a `describe('InfraDiagram — hero band')` block:
- healthy graph → `coverage-meter` is inside an element with `data-testid="infra-hero"`, and `infra-refresh` is inside it too.
- degraded graph → `infra-degraded` is present **and** inside `infra-hero`; `coverage-meter` is absent (existing degraded test at line 80 still asserts this — keep it green).
- loading (a never-resolving `call`) → `infra-hero` and `infra-refresh` render before any graph arrives (so Refresh is reachable while loading).

**Step 2 — implement.** Wrap a new `<div class="infra-hero" data-testid="infra-hero">` containing:
- `<div class="infra-hero__main">` with `{#if graph && !degraded}<CoverageMeter {totals} />{:else if degraded}<p class="ds-note" data-testid="infra-degraded">…existing copy…</p>{:else if loading}<span class="ds-subtle">Loading inventory…</span>{/if}`
- the Refresh `<button data-testid="infra-refresh">` (unchanged attrs/handler) on the right.
Remove the old `.infra-toolbar` div, the `.infra-caption` `<p>`, the standalone `{#if graph && !degraded}<CoverageMeter/>` block, and the standalone `{#if degraded}` block. CSS: `.infra-hero{display:flex;justify-content:space-between;align-items:flex-start;gap;background:var(--ds-surface-2);border:1px solid var(--ds-border);border-radius:var(--ds-radius);padding;margin-top}`, `.infra-hero__main{flex:1;min-width:0}`, `.infra-hero :global(.coverage){margin-bottom:0}`. Keep `.infra-refresh`.

**Step 3 — verify:** the moved degraded+overlay test (line 447) still finds both `infra-degraded` and `infra-diagram`.

## Task 2: Legend ⓘ help (Zone 2) — explain managed/drift/counts-only

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte` (import already has `Icon`; import `HelpHint`; restructure `.infra-legend`)
- Test: `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1 — failing tests.** Add to a `describe('InfraDiagram — legend help')`:
- healthy graph, panel open (use a `previewPr`-free graph but the legend renders on `graph && !degraded` regardless of open — it is in the `<details>` body which is in the DOM even when closed) → `legend-help` button present; clicking it reveals `legend-help-panel` whose text contains `'managed in IaC'` and `'drift'` and `'counts-only'`.
- the legend `<p data-testid="infra-legend">` no longer has `aria-hidden` (assert `getByTestId('infra-legend').getAttribute('aria-hidden')` is null). (Add the `infra-legend` testid — Codex nice-to-have: assert against a stable testid, not the `.infra-legend` class.)
- regression: the no-`previewPr` test (line 430) still finds zero `.infra-key--ghost-*`.

**Step 2 — implement.** Add `import HelpHint from './HelpHint.svelte';`. Define a module/script const `LEGEND_HELP` with the copy above. Restructure the legend `<p class="infra-legend">` (drop `aria-hidden`): a `<span class="infra-legend__lead ds-label">Legend</span>`, then the existing steady keys under `{#if graph && !degraded}`, then the ghost keys under `{#if previewActive}`, then **at the very end** `{#if graph && !degraded}<HelpHint text={LEGEND_HELP} ariaLabel="Explain the resource map colors" testid="legend-help" />{/if}`. CSS: add `.infra-legend__lead{color:var(--ds-muted)}` and confirm the legend `align-items:center` so the ⓘ sits inline.

## Task 3: Adopt zone (Zone 3) — frame it + glanceable count

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte` (`.infra-adopt` markup + CSS)
- Test: `frontend/tests/unit/InfraDiagram.test.ts`

**Step 1 — failing tests.** Add to `describe('InfraDiagram — adopt zone framing')`:
- `adoptGraph()` (drift 2, 2 shown rows, 0 trailers) → a `data-testid="adopt-count"` element with textContent `'2'`.
- the `+N more` fixture (line 588: 4 shown rows + a `+3 more` trailer) → `adopt-count` textContent `'7'` (4 shown + 3 trailered), proving the count equals rows + trailers, not just visible rows.
- update the *old* exact-match at line 542 (which expects the contiguous `'Unmanaged resources shown on the map. These exist…'`) to assert the new split: `adopt-list` contains `'Unmanaged resources'` AND `'These exist in your project but are not under IaC management'`.

**Step 2 — implement.** Add a derived `const adoptShownTotal = $derived(adoptGroups.reduce((a, g) => a + g.rows.length + g.hiddenUnmanaged, 0));`. Replace the single heading `<p>` with a header row:
```svelte
<div class="infra-adopt__head">
  <span class="ds-label infra-adopt__title">Unmanaged resources</span>
  <span class="ds-pill ds-pill--muted infra-adopt__count" data-testid="adopt-count">{adoptShownTotal}</span>
</div>
<p class="ds-subtle infra-adopt__heading">
  These exist in your project but are not under IaC management.
</p>
```
("shown on the map" is dropped from the sentence: the exact count + `+N more` trailers now carry exhaustiveness precisely, so the looser scoping phrase is no longer needed.) CSS: give `.infra-adopt` the framed treatment matching the hero (`background:var(--ds-surface-2);border:1px solid var(--ds-border);border-radius:var(--ds-radius);padding`) and drop its `border-top`-only look; add `.infra-adopt__head{display:flex;align-items:center;gap}` and `.infra-adopt__count` sizing. Also add `flex-wrap:wrap` to `.infra-adopt__row` (Codex nice-to-have: the new horizontal padding can crowd the type/name/button row on mobile).

## Task 4: Full suite + type-check + build + visual sanity

- `cd frontend && npm run test:unit` (vitest run) — all prior + new green. (NOT `npm run test` — no such script; Codex must-fix.)
- `npm run check` — svelte-check type pass.
- `npm run build` — bundle compiles (new bundle hash; `agent/static` is gitignored, built in Docker).
- Optional local visual verify per `live_probe_recipes` if time permits.

---

## Risks / regression watch
- **Testid preservation:** `infra-refresh`, `infra-degraded`, `coverage-meter`, `infra-coverage-count`, `adopt-list`, `adopt-row`, `adopt-control-plane`, `adopt-start-here`, `adopt-hint`, `adopt-order-note`, `adopt-trailer`, `infra-diagram`, `infra-drift-badge`, all `.infra-key--ghost-*` must survive. Only **new**: `infra-hero`, `legend-help`, `adopt-count`.
- **Degraded + ghost-preview coexistence** (test line 447): degraded note now lives in the hero, diagram renders below — both must still be findable.
- **HelpHint panel layout:** placed at the end of the flex legend so its `flex-basis:100%` panel wraps to its own row and never splits the keys.
