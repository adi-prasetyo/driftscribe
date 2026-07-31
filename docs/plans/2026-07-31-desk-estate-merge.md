# Desk + Estate Merge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Merge the estate view into the desk so the SPA has one landing page (band → hero → ledger → estate sections) and a two-item nav (デスク・チャット).

**Architecture:** Pure frontend recomposition — both views are already projections of the same overview-store snapshot, so no data or endpoint changes. The estate view id is retired in stages so every commit compiles and stays green: first slim the two components, then one atomic "merge" commit (App composition + view-alias + tour retarget), then dead-code removal that the compiler proves safe.

**Tech Stack:** Svelte 5 (`$props`/`$derived`/`$state`), Vitest (jsdom unit + smoke), hand-run Playwright visual specs, vite. Design doc: `docs/plans/2026-07-31-desk-estate-merge-design.md`.

**Base:** origin/main — `b6b1e2a` at plan time; origin has since advanced (config-only + non-frontend commits, verified: no frontend/test/doc paths changed through `00657e9`). Branch from CURRENT origin/main. The local main worktree is ~20 commits stale AND shared by other agents. All line numbers below are `b6b1e2a` positions. Work in a fresh worktree, always via `git -C` (see memory `bash_cwd_persists_shared_worktree`).

**Verify at every deploy-adjacent step:** frontend-only delta → coordinator-only build; `cpu-throttling=false` must survive (eventarc fast-ack requirement).

---

### Task A: Worktree, bead, baseline

**Step 1: Confirm empty index, then create the bead**

`bd` mutating commands commit everything staged (project CLAUDE.md warning):

```bash
git -C /home/adi/driftscribe status --porcelain   # index column MUST be empty
bd create --title="Merge Desk and Estate into one landing page" \
  --description="Desk is too empty even with a pending decision; estate is dense and right. Both are projections of the same overview snapshot. Merge estate under the desk (band→hero→ledger→estate), retire the estate view id, nav becomes desk/chat. Pre-video-re-shoot; answers the judges' 見づらい・後付け. Design: docs/plans/2026-07-31-desk-estate-merge-design.md" \
  --type=feature --priority=1
bd update <new-id> --claim
```

**Step 2: Create the worktree from origin/main**

```bash
git -C /home/adi/driftscribe fetch origin main
git -C /home/adi/driftscribe worktree add .worktrees/desk-estate-merge -b ui/desk-estate-merge origin/main
cd is forbidden — from here on use:  WT=/home/adi/driftscribe/.worktrees/desk-estate-merge
```

**Step 3: Install and baseline**

```bash
(cd "$WT/frontend" && npm ci)
(cd "$WT/frontend" && npm run check && npm run test:unit && npm run test:smoke)
```
Expected: 0 check errors; all unit + smoke tests pass (ds-7ag-era counts: ~1647 unit / 28 smoke; #274–#277 may have added more). If baseline fails, STOP — the failure predates this work.

**Step 4: Commit the design doc + this plan**

```bash
cp /home/adi/driftscribe/docs/plans/2026-07-31-desk-estate-merge-design.md \
   /home/adi/driftscribe/docs/plans/2026-07-31-desk-estate-merge.md \
   "$WT/docs/plans/"
git -C "$WT" add docs/plans/2026-07-31-desk-estate-merge*.md
git -C "$WT" commit -m "docs(plan): desk+estate merge design and implementation plan"
```

---

### Task B: Slim EstateView to a section

EstateView loses its own InstrumentBand and the 「← デスクに戻る」 arrival-context button (obsolete once it lives on the desk page), gains `id="estate"` as the scroll/anchor target, and folds the untracked group.

**Files:**
- Modify: `frontend/src/components/EstateView.svelte`
- Modify: `frontend/src/App.svelte` (estate branch call site ~1964: drop `onNavigate`)
- Test: `frontend/tests/unit/EstateView.test.ts`

**Step 1: Write the failing tests**

In `EstateView.test.ts` (match the file's existing render-helper conventions):

```ts
it('renders no instrument band of its own (the desk owns the band)', () => {
  // render with a normal graph fixture
  expect(screen.queryByTestId('instrument-band')).toBeNull();
});

it('renders no back-to-desk button (it lives on the desk now)', () => {
  expect(screen.queryByTestId('estate-back-desk')).toBeNull();
});

it('collapses the untracked group into a fold, count in the summary', () => {
  // graph fixture with >0 untracked rows
  const fold = screen.getByTestId('estate-untracked-fold');
  expect(fold.tagName).toBe('DETAILS');
  expect(fold.hasAttribute('open')).toBe(false);
  // summary carries the same desk.estate.untrackedGroup copy with the count
  // rows render INSIDE the fold, testid stays estate-row
});

it('carries id="estate" on the section root for band scroll targeting', () => {
  expect(screen.getByTestId('estate-view').id).toBe('estate');
});
```

Delete/repoint tests that assert: the band renders on estate, back-to-desk navigates, `estate-group-untracked` h2 exists (that testid moves to the fold summary or disappears — follow the system-fold test's pattern), and any `onNavigate` prop assertions. This is a MECHANICAL MIGRATION, not just added cases (Codex round 1): the shared render fixture at `EstateView.test.ts:62–69` carries `onNavigate` — remove it there once — and the entire routing/back-button suites at `EstateView.test.ts:364–437` describe behavior that no longer exists; delete them whole rather than patching assertions.

**Step 2: Run to verify they fail**

```bash
(cd "$WT/frontend" && npx vitest run tests/unit/EstateView.test.ts)
```
Expected: new tests FAIL (band present, button present, untracked is an open list).

**Step 3: Implement**

In `EstateView.svelte`:
- Delete the `InstrumentBand` import and its render (~lines 118–127), the whole
  arrival-context block with `estate-back-desk` (~101–116) INCLUDING its styles
  (~272–291), and the `onNavigate` prop. Then chase the dead code the band
  leaves behind (lines ~17–23, 25–72): the `bandManaged`/`bandDrift`/`awaiting`
  derivations, and the `locale`/`resourceCards`/`scopeTotals`/`awaitingCount`
  imports plus the aggregate `degraded` prop IF `npm run check` + grep confirm
  nothing else in the component reads them (the status line reads
  `graph.degraded`, not the aggregate) — drop removed props from App's call
  site in the same commit. Also delete the ~line 66 comment about gating
  band+rows together; it no longer has a band to gate.
- Add `id="estate"` to the root `<section>` (keep `data-tour="estate"` and
  `data-testid="estate-view"`).
- Replace the untracked group (h2 `estate-group-untracked` + open rows) with a
  `<details class="estate-view__fold" data-testid="estate-untracked-fold">`
  whose `<summary>` is the existing `$t('desk.estate.untrackedGroup', { n: model.untracked.length })`
  copy — mirror the system-managed fold markup exactly (~192–205), keeping the
  row markup and `estate-row` testid unchanged inside it.
- Delete `desk.estate.backToDesk` from BOTH locales in `frontend/src/locales/desk.ts`
  (locales parity test will catch a one-sided deletion).

In `App.svelte` (~1964 estate branch): remove the `onNavigate={navigate}` prop
from `<EstateView … />`. The estate view still exists at this commit.

**Step 4: Run the tests**

```bash
(cd "$WT/frontend" && npm run check && npx vitest run tests/unit/EstateView.test.ts tests/unit/locales.test.ts tests/unit/App.test.ts)
```
Expected: PASS. If App.test.ts asserts the band on the estate view or backToDesk, update those cases here (they are asserting the old world).

**Step 5: Commit**

```bash
git -C "$WT" add -A frontend
git -C "$WT" commit -m "refactor(ui): EstateView becomes a section — own band and back-button out, untracked folded (ds-<id>)"
```

---

### Task C: Slim the hero's calm states

`resting` and the `unknown` pair become a one-line strip. They keep their copy keys, testids, and the resting/unknown copy separation (ds-eh6: loading/degraded share a shape with resting but never copy).

**Files:**
- Modify: `frontend/src/components/ApprovalDesk.svelte` (calm markup ~280–350 — the ds-eh6 graph gates at ~337–349 must survive byte-identical; styles ~493–506)
- Test: `frontend/tests/unit/ApprovalDesk.test.ts`

**Step 1: Write the failing test**

The h2 STAYS — dropping it would make the hero's heading outline depend on
state (pending/stamped keep headings, calm states lose them; Codex round 1).
The slimming is layout, not semantics:

```ts
it('renders resting as a slim strip: headline and watch line share one row', () => {
  // resting fixture (settled, usable graph, no pending)
  const resting = screen.getByTestId('approval-desk-resting');
  expect(resting.classList.contains('approval-desk__calm--slim')).toBe(true);
  expect(resting.querySelector('h2')).not.toBeNull();      // semantics preserved
  // watch segments unchanged
});
```

Mirror for the `unknown` state (`approval-desk-unknown` keeps `data-reason`).
Existing copy-content assertions (lastScan / scanStale / scanPending /
scanUnavailable / resourceCount / noNewDrift segments) must keep passing — this
is a markup change, not a copy change.

**Step 2: Run to verify it fails**

```bash
(cd "$WT/frontend" && npx vitest run tests/unit/ApprovalDesk.test.ts)
```

**Step 3: Implement**

- In both calm blocks, keep the `<h2>` and the `<p class="approval-desk__watch">`
  but put them on one visual row: add `approval-desk__calm--slim` to the calm
  wrapper and lay it out as a baseline-aligned flex row (h2 restyled to body
  size / 600 weight / `--ds-fg` inside `--slim` — the 31px Mincho rule stays for
  the tall states). Keep every conditional watch segment and its comments
  byte-identical — they encode ds-eh6 honesty rules (~337–349).
- Styles — the wrapper ALREADY pads `40px 40px 26px` (`__deskwrap`, ~503–506),
  so do NOT add padding to the nested calm block (double-inset trap, Codex
  round 1). Instead: slim via a state-scoped wrapper rule
  `.approval-desk__deskwrap[data-state='resting'], .approval-desk__deskwrap[data-state='unknown'] { padding: 18px 40px; }`
  (the `data-state` attr already exists on the wrapper), reset the watch line's
  `margin-top: 18px` inside `--slim`, and **delete `min-height: 280px` from
  `.approval-desk__deskwrap`** — with the estate below, reserving blank height
  is the emptiness we are removing; hero growth on state change is deliberate
  (design doc "Accepted trade-offs").

**Step 4: Run tests, then commit**

```bash
(cd "$WT/frontend" && npm run check && npx vitest run tests/unit/ApprovalDesk.test.ts)
git -C "$WT" add -A frontend
git -C "$WT" commit -m "feat(ui): resting/unknown hero states slim to a one-line strip (ds-<id>)"
```

---

### Task D: The merge — desk absorbs the estate, view id becomes an alias

The atomic commit. After it: desk renders both components, nav has two buttons, `?view=estate` lands on the desk, the tour targets the desk, and the band's managed/drift stats scroll instead of navigate. `VIEWS` still contains `'estate'` (type retirement is Task F) but nothing can reach it.

**Files:**
- Modify: `frontend/src/App.svelte` (nav ~1775–1795, view branches ~1947–1981, `estateHasAdoptTarget` ~693–703)
- Modify: `frontend/src/lib/deeplink.ts` (`viewFromSearch` ~124–129)
- Modify: `frontend/src/lib/tour.ts` (TOUR_STEPS ~84–94, TourStep interface)
- Modify: `frontend/src/components/TourCard.svelte` (target resolver ~72–79)
- Modify: `frontend/src/components/ApprovalDesk.svelte` (band consumer ~279–291)
- Modify: `frontend/src/locales/desk.ts` (delete `desk.nav.estate`, both locales)
- Tests: `frontend/tests/unit/App.test.ts`, `deeplink.test.ts`, `tour.test.ts`, `TourCard.test.ts`, `ApprovalDesk.test.ts`

**Step 1: Write the failing tests**

`deeplink.test.ts`:
```ts
it('treats ?view=estate as a legacy alias for the desk', () => {
  expect(viewFromSearch('?view=estate')).toBe('desk');
});
```

`App.test.ts` — repoint every `?view=estate` case (~78, 115–119, 635–652, 731–732, 883, 1002–1036): estate deep links now render the DESK (which contains `estate-view`); the nav has exactly `nav-desk` and `nav-chat`; the desk view contains BOTH `approval-desk` and `estate-view`; the mid-tour-Back teardown case (~635) keeps its scenario with the tour borrowing `'desk'`. ⚠ ALSO the generic round-trip loop at `App.test.ts:804–812` — it iterates every `VIEWS` entry, clicks `nav-${v}`, and round-trips `viewFromSearch()`; with `'estate'` still in `VIEWS` until Task F it would fail on the missing button (Codex round 1). Rewrite it to iterate the NAV VIEWS (`['desk','chat']` — derive from the rendered buttons, with a comment pointing at Task F's `VIEWS` retirement). Add:
```ts
it('renders the estate section inside the desk view', () => {
  history.replaceState(null, '', '/');
  // render; expect getByTestId('approval-desk') AND getByTestId('estate-view')
});
it('has no estate nav button', () => {
  expect(screen.queryByTestId('nav-estate')).toBeNull();
});
```

`ApprovalDesk.test.ts`:
```ts
it('managed/drift stat clicks call onShowEstate (scroll, not navigation)', () => {
  const onShowEstate = vi.fn();
  // render pending fixture with onShowEstate
  await fireEvent.click(screen.getByTestId('instrument-band-managed'));
  expect(onShowEstate).toHaveBeenCalled();
});
```

`tour.test.ts`: the estate/adopt steps carry `view: 'desk'`; the adopt step carries `fallback: 'estate'`.
`TourCard.test.ts`: when `[data-tour="adopt-target"]` is absent, the spotlight falls back to `[data-tour="estate"]`; the estate-navigation expectations at `TourCard.test.ts:162,169` flip to `'desk'`.

`ApprovalDesk.test.ts` — a MECHANICAL migration, not additive (Codex round 1):
~70 `onNavigate` references. The shared prop fixture (`:115`) renames to
`onShowEstate`; the behavioral suites asserting `onNavigate('estate')`
(`:242–249`, `:306–314`) become `onShowEstate`-called assertions — same
scenarios, new contract. Do the rename as one sweep, then re-read the file for
assertions that now test nothing.

**Step 2: Run to verify they fail, and enumerate the blast radius**

```bash
(cd "$WT/frontend" && npx vitest run tests/unit/deeplink.test.ts tests/unit/App.test.ts tests/unit/tour.test.ts)
grep -rn "view=estate\|'estate'" "$WT/frontend/src" --include='*.ts' --include='*.svelte'
```
Every `'estate'` hit must be accounted for by a bullet in Step 3 (or by Task F).

**Step 3: Implement**

1. `deeplink.ts` `viewFromSearch`: before the generic `VIEWS.includes` check:
   ```ts
   // Legacy alias: the estate merged into the desk (2026-07-31 design doc).
   // Old ?view=estate links land on the merged page rather than 404-ing
   // into a blank main.
   if (raw === 'estate') return 'desk';
   ```
2. `App.svelte` desk branch (~1947): render `<ApprovalDesk … onShowEstate={scrollToEstate} />`
   followed by the `<EstateView … />` element moved up from the estate branch
   (all its remaining props unchanged); delete the `{:else if view === 'estate'}`
   branch entirely. Add next to the other scroll helpers, reusing the
   already-imported `prefersReducedMotion()` helper (the conversation-form
   scroll at ~849–852 is the in-file model — it is NOT the tour spotlight):
   ```ts
   function scrollToEstate(): void {
     const el = document.getElementById('estate');
     if (!el) return;
     el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'start' });
     el.focus({ preventScroll: true });
   }
   ```
   Focus management is required, not optional (Codex round 1): without it a
   keyboard user's focus stays on the band button above the viewport. Give the
   estate `<section>` `tabindex="-1"` alongside its `id="estate"` (amend Task
   B's section root here if it didn't). A `tabindex="-1"` scroll target is not
   keyboard-interactive, so suppressing its focus outline
   (`.estate-view:focus { outline: none; }`) is fine if a ring appears — check
   visually in Task I.
3. `App.svelte` nav: delete the `nav-estate` button and the
   `estateHasAdoptTarget` derived (+ its `firstAdoptableRow` import if now
   unused). Delete `desk.nav.estate` from both locales.
4. `ApprovalDesk.svelte`: replace the `onNavigate: (view: AppView) => void`
   prop with `onShowEstate: () => void`; `onStat` calls it for managed/drift
   (awaiting stays inert at the band level — unchanged). Update the ~279–290
   comment: the destination is now this page's own estate section; keep the
   ds-s61 story.
5. `tour.ts`: `TourStep` gains `fallback?: string` ("data-tour attribute to
   spotlight when `target` matches nothing"); estate/adopt steps get
   `view: 'desk'`, adopt gets `fallback: 'estate'`; rewrite the ~89–91 comment
   (the nav-estate fallback host is gone).
6. `TourCard.svelte` resolver (~79):
   ```ts
   const found = document.querySelector(`[data-tour="${target}"]`)
     ?? (step.fallback ? document.querySelector(`[data-tour="${step.fallback}"]`) : null);
   ```

**Step 4: Run the full unit suite, then grep-prove the estate is unreachable**

```bash
(cd "$WT/frontend" && npm run check && npm run test:unit)
grep -rn "navigate('estate')\|navigate(\"estate\")\|view: 'estate'" "$WT/frontend/src"
```
Expected: tests PASS including every repointed App case, and the grep returns
NOTHING. ⚠ The type system does NOT prove this yet — `AppView` still admits
`'estate'` until Task F — so the grep is the proof at this commit (Codex
round 1). Runtime entry paths are covered: boot and popstate go through
`viewFromSearch` (which aliases), the nav caller is deleted, and both tour
steps moved to `'desk'`.

**Step 5: Commit**

```bash
git -C "$WT" add -A frontend
git -C "$WT" commit -m "feat(ui): one landing page — the desk absorbs the estate (ds-<id>)"
```

---

### Task E: Collapse the band to a single context

With one page there is one context: managed/drift interactive (destination-bearing aria), awaiting always inert.

**Files:**
- Modify: `frontend/src/components/InstrumentBand.svelte`
- Modify: `frontend/src/locales/desk.ts`
- Test: `frontend/tests/unit/InstrumentBand.test.ts`

**Step 1: Failing tests** — rewrite the context-table cases:

```ts
// managed/drift render as <button> with the destination-bearing aria
// (desk.band.managedAriaDesk / driftAriaDesk); awaiting renders as an inert
// <span role="img"> with the PLAIN awaiting aria, for known AND unknown values;
// unknown managed/drift stay interactive with *UnknownAriaDesk.
```
Keep the meter cases untouched.

**Step 2: Run to verify failures**

**Step 3: Implement**

- Delete `BandContext`, the `context` prop, and the estate columns of the
  routing table comment (record the merge date there). `DEST_ARIA`/`HINT`
  become flat `Partial<Record<BandStat, CatalogKey>>` with only managed/drift
  entries; awaiting takes the inert-span branch unconditionally.
- Callers: `ApprovalDesk.svelte` drops `context="desk"` (only caller left).
- Locales (both EN and JA): delete `desk.band.awaitingAriaEstate` and
  `desk.band.statHintDesk`; ALSO delete the now-unreachable plain
  `managedAria`/`driftAria`/`managedUnknownAria`/`driftUnknownAria` keys **iff**
  `npm run check` + a grep confirm no remaining reference — the aria-key rule
  (`<stat>Aria` = inert) now only applies to awaiting. Keep the vestigial
  `*AriaDesk` names (design doc "Accepted trade-offs").

**Step 4: Run and commit**

```bash
(cd "$WT/frontend" && npm run check && npx vitest run tests/unit/InstrumentBand.test.ts tests/unit/locales.test.ts tests/unit/ApprovalDesk.test.ts)
git -C "$WT" add -A frontend
git -C "$WT" commit -m "refactor(ui): instrument band drops the two-context routing table (ds-<id>)"
```

---

### Task F: Retire the estate view id — compiler-proven dead code

**Files:**
- Modify: `frontend/src/lib/deeplink.ts` (`VIEWS` ~66)
- Test: `frontend/tests/unit/deeplink.test.ts`

**Step 1:** Change `VIEWS` to `['desk', 'chat'] as const`. The Task D alias line
in `viewFromSearch` stays (it matches on the raw string, not on VIEWS).

**Step 2:**
```bash
(cd "$WT/frontend" && npm run check && npm run test:unit)
```
Expected: PASS with zero edits elsewhere. Any `'estate'`-typed straggler
(tourReturnView, navigate caller, test helper) surfaces HERE as a type error —
fix it in this commit; that is what this task exists to prove.

**Step 3: Commit** — `refactor(ui): AppView drops 'estate' (ds-<id>)`

---

### Task G: History smoke + visual specs

**Files:**
- Modify: `frontend/tests/smoke/history.smoke.ts` (real Back/Forward; jsdom cannot)
- Modify: `frontend/tests/visual/wayfinding.visual.ts`, `estate.visual.ts` (the dedicated estate rig — it enters via `/?view=estate` at ~:33 and ~:237, which now ALIASES to the desk so it silently keeps running with stale intent; repoint its entry, comments, and frames at the desk's estate section, and use its fixtures to verify the untracked fold), `desk.visual.ts` (~:397–438 captures the hero states and full-page shape, both of which change materially), `header.visual.ts` (hand-run)

**Step 1:** In `history.smoke.ts`, repoint `?view=estate` scenarios: an estate
deep link must land on the desk and leave the app in ONE Back press; the
"clicked a desk numeral" journey becomes scroll (no history entry at all —
assert the URL does not change on a managed-stat click).

**Step 2:** `(cd "$WT/frontend" && npm run test:smoke)` — expected PASS.

**Step 3:** Update the visual specs: nav has two tabs; the estate frames become
desk-scrolled-to-estate frames; keep the 390–1600px × JA/EN matrix. Hand-run
them (vite dev rig). ⚠ Route-mock patterns must stay PRECISE — a loose
`**/conversations**` glob also matches `/src/lib/conversations.ts` and kills
boot (ds-7ag lesson).

Note on the header: removing a nav tab only NARROWS the nav (~271px measured for
three JA tabs), so the ds-7ag responsive breakpoints (single row ≥1560px, tagline
<900px, nav row <640px) stay valid — they were sized for the wider nav. Verify
the single-row mode visually at ≥1560px anyway; do not retune breakpoints.

**Step 4: Commit** — `test(ui): history smoke + visual specs follow the merge`

---

### Task H: Copy and docs sweep

**Step 1:** Sweep the old world's vocabulary, not just identifiers (ds-s61
lesson — grep the BEHAVIOR's words). Two vocabularies, two greps: the
view-split language AND the POSITIONAL language, because tour copy orients by
layout ("the infrastructure panel below", "coverage meter below" —
`frontend/src/locales/tour.ts:33–56` EN, ~:174–199 JA) and those directions
change meaning when the sections merge onto one page (Codex round 1):

```bash
git -C "$WT" grep -inE "view=estate|estate view|インフラビュー|estate tab|nav-estate" -- frontend docs README.md README.ja.md
git -C "$WT" grep -inE "below|above|下に|下の|上に" -- frontend/src/locales/tour.ts frontend/src/locales/desk.ts
```

**Step 2:** Fix what now lies: tour step copy that says the estate is a
separate view/screen or points "below" at something now above, runbook lines
that navigate to the インフラ tab, OVERVIEW/README screen descriptions. Plan
docs under `docs/plans/` are history — leave them. Public EN docs take no em
dashes (standing rule).

**Step 3:** Copy changes break PINNED tests — `tour.test.ts:224` and `:261`
pin exact tour strings (Codex round 1). Update the pins alongside the copy and
run the suite in THIS task, not Task I:

```bash
(cd "$WT/frontend" && npx vitest run tests/unit/tour.test.ts tests/unit/locales.test.ts)
```

**Step 4: Commit** — `docs(ui): copy follows the desk+estate merge`

---

### Task I: Gates + visual verification

**Step 1: Full gates**

```bash
(cd "$WT/frontend" && npm run check && npm run test:unit && npm run test:smoke && npm run build)
```
All green. (CI will re-run these; `ui-smoke` is a REQUIRED check.)

**Step 2: Visual pass at the pitch width** — dev server + Playwright at
1440×900, BOTH locales, FOUR states: resting (estate visible without scrolling —
this is the emptiness fix, screenshot it), pending (band + decision + ledger
fill the fold, drift rows peeking), managed-stat click scrolls to the estate
section, untracked fold opens. Band testids are
`instrument-band-{managed,drift,awaiting}`; screenshot AFTER numerals settle
(skeleton-state trap, ds-7ag). Also 390px: no horizontal scroll
(`scrollWidth == clientWidth`).

**Step 3:** Fix anything found; commit as its own small fixes.

---

### Task J: PR, review, merge

**Step 1:** Push and open the PR (branch `ui/desk-estate-merge` → `main`), body
linking the design doc + before/after screenshots from Task I.
**Step 2:** Codex review via `mcp__codex__codex-reply` on this plan's thread —
findings are advisory: verify, push back, or fix on merits.
**Step 3:** CI must be green INCLUDING GitGuardian (scans every commit — no
realistic-looking tokens in fixtures, ever) and `ui-smoke`.
**Step 4:** Merge. Multi-commit branch → merge commit (repo convention for the
big UI branches #255/#256/#272); each commit already stands alone at bisect.
**Step 5:** `bd close <id> -r "merged as <sha>"` — empty index first.

---

### Task K: Deploy + prod verification

**Step 1:** REQUIRED SUB-SKILL: `driftscribe-deploy`. Frontend-only delta →
coordinator-only build (verify with `git diff --stat` against the deployed tag:
no `driftscribe_lib/`, no `workloads/`, no worker code). Pinned-traffic
two-step; verify `cpu-throttling=false` and all env keys carry over; digest-check
the tag; smoke on the isolated tag URL before `update-traffic`.

**Step 2:** REQUIRED SUB-SKILL: `driftscribe-live-probe`. Adapt the ds-7ag
17-check recipe (run.app host + `sessionStorage` token seed + locale init
script): desk shows band+hero+ledger+estate; `?view=estate` lands on the desk;
nav shows two tabs; managed-stat click scrolls (URL unchanged, ONE Back press
leaves the app); tour estate/adopt steps spotlight on the desk; JA + EN.

**Step 3:** Update memory: coordinator rev pointer, this merge's memory file,
and mark the video re-shoot as unblocked-by-this (next binding step in the
pitch sequence).
