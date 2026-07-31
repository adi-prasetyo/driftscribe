# Desk + Estate merge — one landing page (design)

Date: 2026-07-31. Status: validated with operator (conversation), pre-implementation.
Implementation plan: `docs/plans/2026-07-31-desk-estate-merge.md`.

## Problem

The desk view is too empty. Even in its best state — a pending decision showing —
it is a ~780px column floating in a 1440px viewport, and at rest it is a headline,
one watch line, and four ledger rows. The finalist judges' only negative was
「UI/UXが非常に見づらく、フロントエンドが後付けのようである」, and the empty desk
was already flagged (2026-07-28) as "probably the highest-leverage screen" —
post-publicity visitors usually arrive when nothing needs approval.

The estate view, by contrast, reads right: dense grouped rows over the same data.

Both views are pure projections of the same overview-store snapshot (neither
fetches anything of its own), both render the same InstrumentBand in the same
corner, and ds-7ag's per-stat band routing already treats them as one logical
surface split across two URLs. The split costs navigation (the original ds-7ag
bug was "clicked a numeral, landed somewhere, got lost") and buys nothing.

## Decision

Merge the estate view into the desk. One landing page; nav becomes デスク・チャット.

### Page hierarchy, top to bottom

1. **Instrument band** — unchanged figures (managed / drift / awaiting).
   Managed/drift stats stay interactive but now **scroll to the estate section**
   on the same page instead of switching views, and **move focus with the
   scroll** (the section takes `tabindex="-1"`) so keyboard and screen-reader
   users land where the page went. Awaiting becomes **always inert**: its
   subject (the hero) sits directly beneath it, which is the exact ds-s61
   rationale that already made it inert on the desk.
2. **Hero (decision area)** — all five states preserved (`unknown`, `resting`,
   `pending`, `unresolved`, `stamped`) with the same copy keys and the same
   honesty discipline. One change: **`resting` (and the `unknown` pair, which
   shares its shape) slims from a big calm card to a one-line strip** — headline
   inline with the watch metadata. The headline keeps its `h2` semantics
   (restyled, not removed) so the hero's heading outline does not depend on
   which state is showing. The calm state no longer has to carry the page
   alone; a quiet line is the right size for "nothing needs you".
3. **Ledger strip** — unchanged (4 rows). Separates "what happened" from "what
   exists" and remains the designed empty-state anchor.
4. **Estate section** (`id="estate"`), ordered by actionability:
   - **ドリフト検出 — always expanded**, chips and all (adopt button / PR-pending
     chip / adopt-unavailable suppression untouched). The server-cap
     「…{n} more drift」 line stays.
   - **管理下 — expanded**, visually quiet (order + fold provide hierarchy; no
     new styling). Revisit folding only if it grows past ~15 rows.
   - **未追跡 — collapsed into a `<details>` fold**, count in the summary, same
     pattern as the system-managed fold. The count is the information; the rows
     are detail-on-demand.
   - **システム管理 fold + "other resources" caption** — unchanged.

### What is deleted

- The estate view id: `VIEWS` becomes `['desk', 'chat']`; `?view=estate` parses
  as a **legacy alias for desk** (old links keep working, they just land on the
  merged page).
- The インフラ nav button, and EstateView's own InstrumentBand and
  「← デスクに戻る」 button (arrival-context escape hatch — obsolete when the
  desk is the same page).
- The band's `context: 'desk' | 'estate'` prop and its estate-context i18n keys
  (`awaitingAriaEstate`, `statHintDesk`, and the now-unused plain
  managed/drift aria keys). One context remains, so the table collapses to:
  managed/drift interactive (scroll), awaiting inert.
- `estateHasAdoptTarget` in App.svelte and the conditional
  `data-tour="adopt-target"` on the nav-estate button. The tour's adopt step
  instead gets a declarative fallback target (the estate section) resolved in
  TourCard.

### What is deliberately kept independent

The hero's state machine and the estate section's own loading/degraded status.
A pending approval can coexist with a failed graph fetch; each area reports its
own truth. This discipline (ds-eh6, unknown ≠ empty) is already in the code and
the merge must not couple them.

### Tour

Steps `estate` and `adopt` navigate to `'desk'` instead of `'estate'`; their
spotlight targets (`data-tour="estate"` on the estate section,
`data-tour="adopt-target"` on the first adoptable row) are unchanged. TourCard
already scrolls targets into view, which now does the section-scroll work.

## Non-goals

- No width change: both views already share `max-width: 780px`; the merge fixes
  emptiness vertically. (If the column still feels narrow on camera, that is a
  separate, later decision.)
- No data-layer or endpoint changes → **no DEMO_ALLOWLIST change**.
- No queue depth on the hero (awaiting=2 shows one decision at a time — a
  pre-existing behavior, out of scope).
- 定期点検 ledger row stays deferred (scan runs still aren't persisted).
- No `#estate` hash deep-linking (the id enables it later if ever wanted).

## Accepted trade-offs

- **Hero state changes now reflow the estate below** (resting is slim, pending
  is tall). Accepted: state changes are rare and meaningful — the demo's money
  shot *is* drift arriving and the page growing a decision. The `min-height:
  280px` on the hero wrapper is dropped with the same reasoning.
- **`?view=estate` links land at the top of the merged page**, not scrolled to
  the estate section. The content is one scroll away; not worth a hash router.
- **The band's `*AriaDesk` key names keep their now-vestigial suffix** — renaming
  them churns two locales and tests for zero user-visible value.

## Timing

This is the pre-video-re-shoot change: it must land before the re-shoot (after
it, live and recorded UI would visibly diverge, which the 8/10–8/19 polish
window forbids). Local main is 20 commits behind origin/main — implementation
starts from a fresh worktree on origin/main (`b6b1e2a`).
