# Design: collapse the autonomy dial into a header pill + popover

**Date:** 2026-06-28
**Status:** validated with operator (brainstorming), pending Codex second opinion, then implementation
**Scope:** frontend-only. The `/autonomy` GET/POST contract is unchanged.

## Problem

The autonomy control (Observe / Propose / Propose + Apply) is a large, always-on
card pinned at the top of the chat area (`AutonomyControl.svelte`, wrapped in
`data-tour="controls"`). It carries a lot of permanent vertical real estate for a
setting that is changed rarely, and it pushes the actual chat composer, estate
panel, and timeline down the page.

Meanwhile the codebase already has the exact pattern we want for an infrequently
changed global control: **`PausePill`** — a compact status pill in the header
top-right that, on click, drops an anchored popover hosting a reason field +
confirm. Pause and Autonomy are the same *class* of control ("what is DriftScribe
allowed to do right now"), so they should look and behave like siblings.

## Decision summary (validated with operator)

1. **Location:** a compact `Mode: …` pill in the **header `app-header__actions`
   cluster, placed before `PausePill`** (so the order reads `[Autonomy] [Pause]
   [Tour] [Token]`). This is the operator's "coordinator window, top right".
2. **Editor surface:** an **anchored popover** (not a centered modal), mirroring
   `PausePill`'s popover — dismiss on Esc / outside-click, gated on `!saving`.
3. **Pill interaction:** the **whole pill is the button** (with a chevron
   affordance), one target — no separate edit/pencil button.
4. **State:** introduce a shared **`autonomyStore`** mirroring `pauseStore`, and
   migrate `CapabilityCard`'s private `/autonomy` fetch onto it (one fetch, one
   source of truth, and the capability note updates reactively on a mode change).
5. **Tour:** move the `data-tour="controls"` spotlight marker onto the new header
   pill so the "You set the pace" step still highlights a real element.

## Components & state ownership

- **New `lib/autonomyStore.ts`** — mirrors `lib/pauseStore.ts`. Owns server-truth
  state (`loading | loaded | unknown`, the parsed `AutonomyDoc`), exposes
  `refresh()` and `confirm(mode, reason?)` (the POST). The monotonic seq
  stale-guard and single-flight `saving` guard move here from `AutonomyControl`.
  **Critically, also carry over pauseStore's "commit wins" guard** (Codex #2):
  `refresh()` must no-op while `saving` (`pauseStore.ts:85`). This matters *more*
  here than for Pause, because two components (`AutonomyPill` and
  `CapabilityCard`) can now call `refresh()`/`retry` — without the guard, a refresh
  from one during the other's confirm POST would bump the shared seq and drop the
  successful POST's response.
- **New `components/AutonomyPill.svelte`** — header surface, direct sibling of
  `PausePill`. A compact clickable pill showing the current mode; on click drops
  the anchored popover hosting the full dial. Consumes `autonomyStore`.
- **`components/CapabilityCard.svelte`** — drop its own best-effort `/autonomy`
  fetch (`fetchAutonomyBestEffort`, lines ~52–69; call site ~119); read the
  autonomy-note state off the shared store. **Preserve the existing best-effort
  failure semantics** (Codex #1): today a fetch failure / non-ok / malformed body
  yields *no note* (the card stays static) — it does NOT show "unknown/fail-closed"
  copy. After migration, render the note **only when `store.kind === 'loaded'`**
  and the mode is below `propose_apply`; `loading`/`unknown` stay silent, exactly
  as now. The only intended behavior change is that the note becomes reactive to a
  mode change made in the popover.
- **`components/AutonomyControl.svelte`** — **removed** from the chat area. Its
  internals are not discarded: the 3-segment sliding-pill dial, the
  `parseAutonomyDoc` wiring, the arm-then-confirm flow, the reason field, the
  progressive-disclosure explainer, and the meta line all move into the popover
  body of `AutonomyPill`. The file is deleted once its parts are relocated.
- **`App.svelte`** — remove `<AutonomyControl {call} />` from the chat area;
  mount `<AutonomyPill {autonomy} />` (or `{call}`-driven; see store note) in the
  header before `<PausePill {pause} />`. `PauseBanner` stays where it is.

### Store vs. self-contained (decided: store)

There are genuinely two readers of `/autonomy` today — the dial and the
capability card — each fetching independently. `pauseStore` is the established
precedent for "shared server-truth control state feeding more than one
component". Going with a store fixes a real edge: changing the mode in the
popover makes the capability note update without a reload. The cost is a moderate
refactor of `CapabilityCard`'s fetch. Accepted.

## The pill (header surface) & salience

- **Content:** mode icon + mode name + chevron, e.g. `⚡ Propose + Apply ▾`.
  Whole pill is the button (`aria-haspopup="dialog"`, `aria-expanded`).
- **States** (from the store):
  - `loading` → muted `Mode: checking…` pill, not clickable, no popover.
  - `loaded` → live mode pill, clickable.
  - `unknown` → `State unknown` warn pill; the retry path lives inside the
    popover's body when openable, matching how Pause degrades.
- **Salience without alarm:** the dial's ceiling has real consequences — at
  Propose + Apply, the Patch workload may complete **routine dependency updates
  end-to-end** and **Anchor self-triggers on drift** and acts up to this ceiling
  (note: **infrastructure applies and rollbacks always require explicit approval
  at every setting** — see "Honest copy boundary" below). So the mode must stay
  legible — but the existing design rule is "modes are operator choices, not
  alarms," so **no per-mode red/warn coloring**. We honor both by always naming
  the mode in words (never icon-only) and giving each mode its existing calm icon
  (`eye` / `git-pull-request` / `zap`) on a neutral tint. Always glanceable,
  never shouting.
- **`read_error` (fail-closed to Observe) is a pill-level state, not popover-only**
  (Codex #5): "Observe" and "Observe because the state could not be read, failing
  closed" are operationally different. When the backend reports `read_error`, the
  **pill itself** shows a degraded indicator (e.g. `Observe · fail-closed` and/or
  a warn tint) — this is a degraded-state signal, NOT per-mode alarm coloring —
  and the popover keeps the existing "failing closed to Observe" warning. Never
  hidden.

### Honest copy boundary (Codex #4)

Before hiding the full dial behind a pill, the copy must state the real
permission boundary, and my earlier framing ("change live infra unattended") was
an overclaim that must not survive into code. The truth, already encoded in
`tour.ts` and the autonomy memory:

- **Infrastructure applies and rollbacks always pass the explicit approval gate,
  at every mode** (including Propose + Apply).
- At **Propose + Apply** only: the Patch/upgrade workload may complete **routine
  dependency updates** end-to-end, and **Anchor** (the only self-triggering
  workload) runs on drift up to the dial ceiling with no human kick-off.
- `MODE_BLURBS.propose_apply` currently reads "Propose changes and apply them
  after your approval (current default)" (`autonomy.ts:18`), which is the honest
  *infra* story but undersells the dependency-update autonomy. Reconcile the
  blurb, the **arm-to-confirm hint shown when raising to Propose + Apply**, and
  the tour `CONTROLS_LINE` so all three say the same thing. The persistent pill
  stays calm; the confirm step is where the boundary is spelled out plainly.

## The popover (editor)

Anchored under the pill, right-aligned; structurally the `PausePill` popover.
Content top-to-bottom (all lifted from `AutonomyControl`):

1. **The 3-segment dial** with the sliding active pill — segment refs,
   `ResizeObserver` re-measure, and the `--ready` first-paint guard come along.
   **Wrinkle (extended per Codex #3):** the measurement effect currently keys on
   `stateKind === 'loaded'`; in a popover the segments exist only while open, so
   the effect must also gate on `open` and re-measure via `tick()` on each open.
   **Additionally reset `pillMeasured` and `pillReady` on open/close** — leaving
   stale geometry from a previous open (or from before a header wrap/resize) makes
   the active pill flash or animate from an old width/position on the next open.
   The `ResizeObserver` is created per-open and disconnected on close (it already
   guards `typeof ResizeObserver !== 'undefined'` for jsdom).
2. **Current-mode caption + blurb.**
3. **Reason field + Confirm / Cancel** — the arm-then-confirm flow: clicking a
   different segment arms it, Confirm POSTs `{mode, reason?}` via
   `autonomyStore.confirm()`.
4. **Collapsed explainer** — "How does the agent act on its own?", collapsed by
   default.
5. **Meta line** — set-by / when / reason; plus the `read_error` warning.

Behavioral guards (carried from both predecessors):

- **Dismiss gated on `!saving`** — never tear down mid-POST (PausePill's lesson),
  or a failed save writes `postError` into a closed panel and is lost.
- **Reset on close** — clear armed/pending/reason/postError so a stale armed
  segment can't reappear on the next open.
- **Focus** — on open, focus moves into the popover; on close, returns to the
  pill. The reason input focuses when a segment is armed.
- **Auto-close on successful confirm** — the pill itself now reflects the new
  mode, so we close on success (matches Pause), rather than lingering.
- **One-header-popover coordination** (Codex #7): the Autonomy pill and the Pause
  pill are two anchored popovers in the same corner. A pointer-click outside one
  closes it (each has PausePill's window `pointerdown` handler), but **keyboard**
  activation of the other pill's toggle does not go through that path, so both
  could end up open and visually overlapping. Lift a single parent-owned
  "which header popover is open" key into `App.svelte` (e.g. `null | 'pause' |
  'autonomy'`) and have each pill open by setting it / close when it isn't theirs.
  This also prevents two popovers stacking on top of each other top-right. Esc
  closes the open one (gated on `!saving`).

## Tour spotlight

The tour resolves a step's `target` via
`document.querySelector('[data-tour="${target}"]')`, adds the `.tour-spotlight`
outline, and `scrollIntoView({block:'center'})`. It works on any element,
including a small pill.

- Move `data-tour="controls"` **off** the chat-area wrapper (which held
  `PauseBanner` + the removed dial) and **onto the autonomy pill's always-rendered
  outer wrapper** — NOT the loaded-only button (Codex #8). The pill has
  `loading`/`unknown` states; if the marker lives only on the loaded button, the
  spotlight `querySelector` finds nothing when the tour opens before `/autonomy`
  resolves. Anchor it on the wrapper that renders in every state. The chat-area
  wrapper keeps just `PauseBanner`.
- **Update `CONTROLS_LINE`** (Codex #8): it currently opens "This dial governs…"
  while the spotlit element is now a compact pill, which weakens the teaching.
  Reword to name the visible control (e.g. "The Mode control in the top bar
  governs…"). It already lists all three modes in prose, so the operator still
  learns what the dial does without the popover being open. The closing sentence
  ("The Pause control in the top bar suspends all agent activity…") now reads
  naturally, since both controls share the top bar. Also reconcile this copy with
  the honest Propose + Apply boundary above.
- **Decided: spotlight the autonomy pill specifically** (the step's subject is
  pacing/autonomy), not the whole actions cluster. Pause is mentioned as its
  neighbor in the copy. The popover stays **collapsed** during the step — no
  auto-open (auto-opening couples the tour to the pill's internal open state and
  would fight the floating-popover-vs-docked-card layout for marginal gain). A
  possible future enhancement, not v1.
- `scrollIntoView` centering a header element simply scrolls to the top; fine.

## Knock-on changes, testing, deploy

- **Layout:** removing the dial card reclaims a sizable block at the top of the
  chat area; chat/estate/timeline move up. The `data-tour="controls"` wrapper now
  wraps only `PauseBanner`.
- **Header responsive (Codex #6):** `.app-header` wraps but `.app-header__actions`
  is `inline-flex` with no `flex-wrap` (`App.svelte:793`). Adding a third, longer
  control (`Mode: Propose + Apply`) risks horizontal overflow on narrow viewports.
  Make the actions cluster wrap (or drop to a second full-width row on small
  screens), and prefer shortening/hiding the title subtitle ("The agent proposes,
  you approve.") before shrinking the safety controls. The autonomy pill may also
  abbreviate its label on very narrow widths (icon + short mode), but must always
  keep the mode legible in words at normal widths.
- **Tests:**
  - Retire/rework `AutonomyControl.test.ts` → `AutonomyPill.test.ts` (pill renders
    the live mode; opens popover; arms + confirms; dismiss-gated-on-saving;
    fail-closed / unknown states) + `autonomyStore.test.ts` (loaded/unknown parse;
    confirm POST success/failure; seq guard) — modeled on the existing
    `pauseStore` tests.
  - `CapabilityCard` tests: drive the autonomy-note through the store instead of
    its own fetch mock.
  - Playwright smoke: repoint any fixture referencing the old `autonomy-control`
    testid. New testids: `autonomy-pill`, `autonomy-popover`; reuse
    `autonomy-mode-*` / `autonomy-confirm` / `autonomy-reason` inside the popover.
  - Gate: full vitest, `svelte-check` 0/0, `vite build`, Playwright smoke.
- **Deploy:** SPA is baked into the coordinator image → ships via a coordinator
  rebake + traffic shift. No backend change.

## Out of scope / non-goals

- No change to the `/autonomy` backend contract or `agent/autonomy.py`.
- No per-mode alarm coloring (preserves the "modes are choices, not alarms"
  stance).
- No auto-opening the popover during the tour (possible later enhancement).
- No new modal/overlay pattern (we reuse the anchored-popover pattern).

## Codex review — disposition (thread 019f0a05)

Reviewed pre-implementation per the standing second-opinion rule. Verdict: "proceed
with the store + header pill approach; not over-engineering." 8 points raised; I
verified each against the code and **folded all 8** (the inline sections above now
carry them). Highlights:

1. **CapabilityCard best-effort semantics** — keep `unknown`/`loading` silent;
   note only when `loaded`. Folded into the CapabilityCard bullet.
2. **"Commit wins" guard** — `refresh()` no-ops while `saving`; matters more with
   two refresh callers. Folded into the store bullet.
3. **Measurement** — also reset `pillMeasured`/`pillReady` on open/close, not just
   gate on `open`. Folded into the popover dial item.
4. **Propose + Apply copy** — corrected my own overclaim; added the "Honest copy
   boundary" subsection reconciling blurb + confirm hint + tour line.
5. **`read_error` on the pill**, not popover-only — degraded-state indicator (not
   alarm coloring). Folded into the salience bullet.
6. **Header crowding / responsive** — actions cluster must wrap. Folded into
   knock-on changes.
7. **Sibling-popover coordination** — parent-owned "one header popover open" key.
   Folded into the popover behavioral guards.
8. **Tour** — reword `CONTROLS_LINE` ("Mode control" not "dial"); put `data-tour`
   on the always-rendered pill wrapper, not the loaded-only button. Folded into
   the tour section.

No points were rejected — all were correct and improve the design.
