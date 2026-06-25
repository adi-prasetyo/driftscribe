# Infrastructure panel — scope split (adoptable vs. the rest)

Date: 2026-06-25
Status: design (pre-implementation)
Surface: `frontend/` only — no backend/DTO/worker change.

## Problem

The Infrastructure panel renders one card per resource type, derived from
`GET /infra/graph`. That DTO comes from the infra-reader's **bare
`SearchAllResources`** (no asset-type filter), so it enumerates the *entire*
project. Live today:

- **546 resources, 9 managed, 537 "drift"** → headline reads ~2% managed.
- **35 type cards, only 4 adoptable** (Storage bucket, Pub/Sub topic,
  Pub/Sub subscription, Cloud Run service). All 9 managed resources live inside
  those 4 types.
- The other 31 types are non-actionable noise: 155 Cloud Run **revisions**, 134
  Docker **images**, 45 default routes, 43 subnets, 46 API enablements, the
  project itself, KMS internals, default firewall rules, 2 API **keys** named by
  opaque UUID (`apikeys.googleapis.com/Key`), etc.

Two user-visible symptoms drove this:
1. A "Key" card showing two UUIDs + "not an adoptable type" — meaningless to the
   operator.
2. The Adopt button (`ds-btn--ghost`, transparent) blends into the beige
   `--ds-warn-surface` drift-row background.

And the deeper credibility problem: "9/546 managed · 537 drift" frames
DriftScribe as failing when it is doing fine *on its scope* — nobody puts Docker
image digests or Cloud Run revisions in Terraform.

## Decisions (confirmed with operator 2026-06-25)

- **Q1 → Collapse below grid.** Default view shows only the cards in
  DriftScribe's scope (adoptable types + anything actually managed). A collapsed
  `<details>` "Other resources DriftScribe doesn't manage (N types · M)" below
  the grid holds the rest. Nothing is deleted — honest, just folded.
- **Q2 → Scope-aware count.** The headline coverage + drift describe the
  **scope** (managed-able types), with the project-wide total shown as muted
  context. So "9 of 29 managed · 20 drift", and "546 total resources indexed
  (517 in types DriftScribe doesn't manage)".
- Adopt button: opaque surface + strong border so it lifts off the drift row.

## The "scope" / "primary" predicate

A resource-type card is **primary** (in scope, shown by default) iff:

```
card.adoptable === true  ||  card.managed > 0
```

- `adoptable` is the group's existing server flag (single source of truth:
  `ADOPTABLE_RESOURCE_TYPES`). Today exactly the 4 types.
- `managed > 0` is a defensive OR: if a future `.tf` ever declares a
  non-adoptable type, its managed resources must never be hidden. (Today no
  managed resource sits outside the adoptable types, so this OR is inert — but
  it guarantees the invariant "the default view never hides a managed
  resource".)

Everything else (incl. sensitive **secrets**, which are counts-only and never
adopted) is **other** → the disclosure.

`count == managed + drift` holds per group (managed = declared-in-IaC, drift =
not), verified against live data, so scope drift = Σ primary.drift.

## Changes

### 1. `frontend/src/lib/infra_graph.ts` (pure helpers — fully unit-tested)

- Add `adoptable: boolean` to `ResourceCard`; set from `g.adoptable === true` in
  `resourceCards()`. (Additive field; existing sort/semantics unchanged.)
- New `splitCards(cards): { primary: ResourceCard[]; other: ResourceCard[] }` —
  partitions by the primary predicate, preserving the existing sort order within
  each list. (`resourceCards` keeps returning the full sorted list; `splitCards`
  is a thin partition so the drift-first + rank ordering still drives the
  primary grid and `startHereAssetType` is unaffected.)
- New `scopeTotals(cards): ScopeTotals` where
  `ScopeTotals = { resources, managed, drift, totalResources, otherResources, otherTypes }`,
  derived entirely from the cards array (single source, perfectly consistent
  with `splitCards`):
  - `resources/managed/drift` = Σ over **primary** cards
  - `totalResources` = Σ over **all** cards (== `graph.totals.resources`, since
    every `count > 0` group yields a card)
  - `otherResources` = Σ over **other** cards
  - `otherTypes` = `other.length`
  - degraded / empty → all zeros.

### 2. `frontend/src/components/CoverageMeter.svelte`

- Add optional `subject` prop (default `"your infrastructure"`); headline becomes
  `"{pct}% of {subject} is under IaC management"`. InfraDiagram passes
  `subject="your adoptable infrastructure"`. Detail line unchanged (now shows
  scope M/N). Purely additive; the one caller opts in.

### 3. `frontend/src/components/InfraDiagram.svelte`

- Derive `const split = splitCards(cards)` and `const scope = scopeTotals(cards)`.
- **Hero:** feed `<CoverageMeter totals={scope} subject="your adoptable infrastructure" />`
  (synthesize `{resources, managed, drift}` from `scope`); add a muted context
  line: `"{scope.totalResources} total resources indexed · {scope.otherResources}
  in types DriftScribe doesn't manage"` (only when `otherResources > 0`).
- **Collapsed summary badge:** drift badge + managed count now read `scope`
  (`scope.drift` for the pill, `scope.managed`/`scope.resources` + scope pct).
- **Grid:** render `split.primary` in the existing `.infra-cards` grid.
- **Disclosure:** when `split.other.length > 0`, render
  `<details class="infra-other"><summary>Other resources DriftScribe doesn't
  manage — {otherTypes} types · {otherResources} resources</summary>` containing
  a second `.infra-cards` grid of `split.other`.
- **DRY:** extract the per-card markup into a Svelte 5 `{#snippet cardView(card)}`
  rendered via `{@render cardView(c)}` in both grids (no divergence). The
  `startHere` chip references the module-level derived (only adoptable→primary
  cards can match, so "other" cards never get it).
- **Button fix:** `.infra-card__btn` gets an opaque background (`--ds-surface`) +
  `--ds-border-strong` (drop the ghost transparency) so it reads as the row's
  action against the beige drift tint. Keep `ds-btn` base for sizing; replace the
  `ds-btn--ghost` modifier with a local treatment (or override). Confirm via
  local screenshot verify.

Preview path (Mermaid ghost overlay) is **unchanged** — it already renders the
full live map; scope-split is a normal-path concern only.

## Honesty / non-regression invariants

- The default view never hides a managed resource (primary predicate ORs
  `managed > 0`).
- `scope.resources = scope.managed + scope.drift` (the meter's denominator and
  the drift number reconcile with the primary cards shown).
- `scope.totalResources = scope.resources + scope.otherResources` (the muted
  context reconciles with the disclosure's count).
- Nothing is removed from the payload; the disclosure makes every resource still
  reachable — the panel stays "honest about what exists".
- Secrets stay counts-only and land in "other" (not adopted).

## Testing (TDD)

- `infra_graph.test.ts`: `adoptable` field on cards; `splitCards` (primary =
  adoptable, primary = managed-but-not-adoptable defensive case, sensitive →
  other, order preserved, empty/degraded → `{primary:[],other:[]}`);
  `scopeTotals` (live-shaped fixture → 9/29/20, totals/other/types; degraded →
  zeros; reconciliation identities).
- `InfraDiagram.test.ts`: primary cards in the main grid; non-adoptable inside
  the `<details>`; disclosure summary counts; no disclosure when other empty;
  hero meter + badge show scope numbers; muted total-resources line; Adopt button
  carries the non-ghost class.
- `CoverageMeter.test.ts`: `subject` override renders in the headline; default
  unchanged.
- Smoke (`transparency.smoke.ts`): expanded panel shows the adoptable cards +
  the "Other resources" disclosure; scope numbers in the hero.

## Out of scope (deliberately not doing)

- Server-side filtering of the enumeration (keep the full payload; the
  disclosure is the honest way to fold it).
- Changing `totals` semantics in the DTO (scope is computed client-side).
- Re-ranking the "other" cards (keep existing stable order).
