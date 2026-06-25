# Open-trace: scroll-into-view + clean historical-empty timeline

**Date:** 2026-06-25
**Branch:** `fix/open-trace-scroll-empty-state` (worktree off `main` @ 44b29bf)
**Scope:** frontend-only (`frontend/src/App.svelte`, `frontend/src/components/Timeline.svelte` + unit tests)

## Problem (both confirmed live against prod rev 00098 @ 44b29bf)

The **Past decisions** rail gives each row an "open trace →" button. It replays that
decision's trace: `openTrace()` calls `GET /trace/{trace_id}`, flips the chat column into
read-only **historical** mode (HistoricalBanner + TraceBadge `historical` + the reasoning
Timeline + a decision summary card).

1. **The click looks dead.** The historical output renders at the *bottom* of the right
   chat column — below InfraDiagram, CapabilityCard, and the ChatForm composer — i.e. below
   the fold on a normal laptop viewport (measured: button at y≈180, historical region at
   y≈843 on a 768px-tall viewport). `openTrace()` updates state but **never scrolls the
   region into view** (unlike `handleAdopt`/onMount, which both `scrollIntoView` the
   composer). After clicking, `window.scrollY` stays `0`; the only visible change is a 3px
   left-accent on the rail row — easy to miss. From the user's seat, *nothing happens*.

2. **Empty timeline reads as broken — and is noisy.** PR rows are `iac_apply` decisions.
   `/trace` returns `events: []` for them (the approve POST never runs the ADK reasoning
   agent, so Cloud Logging holds no timeline events under that trace_id) and the decision
   carries no `rationale`/`rendered_body`. The current settled UI (status `historical`,
   `events.length===0`) already shows a good one-liner — *"No reasoning timeline for this
   decision. It was recorded directly, not produced by an agent reasoning run."*
   (`Timeline.svelte:113-118`) — **but then still renders the three empty group accordions
   below it**: "Coordinator reasoning → No coordinator reasoning yet.", "Tools & workers",
   "MCP traffic". That's redundant clutter that undercuts the clear message.

   > Correction vs first diagnosis: the bare "No coordinator reasoning yet." I saw live was
   > the **mid-fetch** state (status still `pending` during the ~1.6s `/trace` call), not the
   > settled state. The accurate message already ships; the remaining defect is the redundant
   > empty groups.

## Fix

### Fix 1 — scroll the historical region into view on `openTrace` (the real bug)

In `App.svelte`:

- Import `tick` from `svelte` (currently only `onMount` is imported) and `prefersReducedMotion`
  from `./lib/motion` (existing helper, already used by Timeline via `motionMs`).
- In `openTrace(tid)`, immediately after the synchronous state block (`status = 'pending';`)
  and **before** the `try`/fetch, add:

  ```ts
  // The historical trace renders at the bottom of the chat column (below the
  // estate panel + composer), so the click otherwise looks dead. Bring the
  // banner — and the region under it — into view. #historical-badge exists once
  // historicalActive flips true and the DOM settles (await tick()). Mirror the
  // existing getElementById(...).scrollIntoView pattern (handleAdopt/onMount).
  await tick();
  if (myRun !== runSeq) return; // a newer run superseded us during the tick
  document.getElementById('historical-badge')?.scrollIntoView({
    behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    block: 'start',
  });
  ```

Decisions:
- **Target `#historical-badge`** (HistoricalBanner's root, a hard e2e contract id). It's the
  topmost element of the historical region; `block: 'start'` lands it at the top and reveals
  the timeline beneath it. Only exists when `historicalActive` → guarded with `?.`.
- **Scroll before the fetch resolves** so feedback is immediate: banner + pending timeline
  scroll up on click, then events populate in place. Best answer to "nothing happens".
- **Reduced motion:** `behavior: 'auto'` when `prefersReducedMotion()` (no smooth animation),
  else `'smooth'`. Stricter than the existing unconditional-`smooth` calls — correct for a11y.
- **Stale-run guard:** `await tick()` is a new await boundary, so re-check `myRun === runSeq`
  (the established bail pattern) before scrolling; returning also abandons a superseded run's
  fetch, which is the desired behavior.

### Fix 2 — collapse the historical-empty timeline to just the message

In `Timeline.svelte`, when `status === 'historical' && events.length === 0`, render **only**
the existing `timeline-empty` `<p>` and **suppress** the three `<Group>` accordions.

```svelte
const historicalEmpty = $derived(status === 'historical' && events.length === 0);
...
<div class="timeline">
  {#if historicalEmpty}
    <p class="timeline-empty ds-subtle" data-testid="timeline-empty"> …existing copy… </p>
  {:else}
    <Group key="coordinator" … />
    <Group key="tools" … />
    <Group key="mcp" … />
  {/if}
</div>
```

- Copy unchanged (already accurate + generic for any directly-recorded decision).
- Only the `historical && empty` branch changes. `pending`/`streaming` empty (live, waiting
  for events) still shows the groups + "No coordinator reasoning yet." placeholder. Historical
  **with** events still shows the full grouped timeline. `error` unchanged. → the Playwright
  e2e (which drives a trace **with** events and toggles `#group-tools`) is untouched.

## Design corroboration

A 3-lens design panel (UX / a11y-correctness / minimal-diff) + opus synthesis independently
arrived at this exact spec. Notable: the a11y lens proposed adding a *new* anchor id; synthesis
rejected that in favor of **reusing the existing `#historical-badge` contract id read-only** (no
rename, no new markup) — the choice this plan already made. The panel also confirmed
DriftDiffCard self-suppresses for iac_apply (no `diffs[]`) and DecisionSummary already carries the
iac_apply metadata, so there is no card duplication to fix.

## TDD test plan (`frontend/tests/unit/`)

**Confirmed harness facts:**
- jsdom has no `Element.prototype.scrollIntoView`; `App.test.ts` already installs
  `window.HTMLElement.prototype.scrollIntoView = vi.fn()` in `beforeEach`. To assert, override
  with a captured spy at the top of the test.
- `setup.ts:70-72` stubs `matchMedia` so any `reduce` query returns `matches: true` → in unit
  tests `prefersReducedMotion()` is **true** → the scroll asserts `behavior: 'auto'`.
- Extend the existing `tests/unit/timeline.test.ts` (currently pure-fn tests only) with
  component-render blocks; extend `tests/unit/App.test.ts` for the scroll test (re-stub `fetch`
  in the test to return an openable iac_apply `/decisions` row + a `/trace/` branch with
  `events: []`).
- No error-path scroll test: scroll is **pre-fetch**, so it fires before any `/trace` failure;
  a "no scroll on error" assertion would mis-lock the intended optimistic ordering. Skip it.

1. **Timeline historical-empty (`Timeline.test.ts`, new or extend):**
   - `render(Timeline, { props: { events: [], status: 'historical' } })` → asserts
     `getByTestId('timeline-empty')` present AND `queryById('group-coordinator')` /
     `queryByText('No coordinator reasoning yet.')` is **null** (groups suppressed).
   - `render(Timeline, { props: { events: [], status: 'pending' } })` → groups still render
     (regression guard: live placeholder unchanged).
   - `render(Timeline, { props: { events: [<one llm_thought>], status: 'historical' } })` →
     groups render, no `timeline-empty` (historical-with-events unchanged).

2. **openTrace scroll (`App.test.ts`, extend):** mount App with a token + a `/trace` fetch
   mock returning an iac_apply decision with `events: []`; capture a spy on
   `window.HTMLElement.prototype.scrollIntoView`; click the `open-trace-button`; `waitFor` the
   historical banner; then assert the spy was called with `{ behavior: 'auto', block: 'start' }`
   AND its receiver (`spy.mock.contexts.at(-1)` / `instances.at(-1)`) is
   `document.getElementById('historical-badge')` (Codex: `scrollIntoView`'s `this` is the
   element, options is the arg).

3. **Smoke locks (`tests/smoke/transparency.smoke.ts`, extend the existing historical iac_apply
   test at ~220, Desktop Chrome 1280×720):** after the banner is visible, add
   `await expect(page.locator('#historical-badge')).toBeInViewport();` (Fix 1 — at 720px tall
   the region sits below the fold, so this fails pre-fix) and
   `await expect(page.locator('#group-coordinator')).toHaveCount(0);` (Fix 2 — the empty groups
   are suppressed). Per Codex, jsdom only proves the call fired; this locks the real-browser
   layout/viewport behavior. **Kept only after verifying green locally** (`npm run build` →
   smoke webServer boots uvicorn on :8765); if the local harness can't run reliably here, drop
   the smoke edits and rely on the unit tests + a manual Playwright check against the built app.

## Rejected alternatives
- *Move the historical region above the estate panel* — large layout churn, reorders the
  page for the live-chat case too; rejected for minimal-diff.
- *New empty-state copy / action-aware message* — the existing copy is already accurate and
  generic; adding iac_apply-specific text buys little and couples Timeline to action types.
- *`bind:this` on a wrapper div* — unnecessary; `getElementById('#historical-badge')` matches
  the codebase's existing scroll pattern and reuses the stable e2e id.

## Risks
- **jsdom `scrollIntoView`** must be stubbed or the App test throws — covered above.
- **Scroll vs reduced-motion / test env:** `prefersReducedMotion()` swallows matchMedia
  errors (returns false), so jsdom without matchMedia is safe.
- **e2e contract:** `#historical-badge` and `#group-*` ids are Appendix-B contracts — neither
  is renamed; Fix 2 only conditionally renders the groups in a state the e2e never exercises.
- **Double-scroll feel:** only one `scrollIntoView` fires per openTrace; newChat/submit paths
  unchanged.

## Verify
- `npx vitest run` green (689 baseline + new).
- `npm run build` clean; `npm run check` (svelte-check) clean.
- Local/live visual: click open-trace → region scrolls into view; historical-empty shows one
  line, no empty accordions.
