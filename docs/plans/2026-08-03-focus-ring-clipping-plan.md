# Focus-ring clipping (ds-2fp) — design record

**Goal:** In the states this suite sweeps, no focus indicator is clipped away by
an ancestor, and every control has one. "No ring is clipped" becomes an enforced
CI invariant rather than a one-off audit.

**Scope, stated up front because the goal sentence overclaims if read loosely:**
Chromium only, and only the enumerated states. Rounded-corner clipping, occlusion,
viewport clipping, transform scaling, and anything visible only *while animating*
are not modelled — see the closing section.

**Architecture:** Five CSS fixes plus a Playwright smoke test that measures the
real thing — it Tabs through the built app and compares each control's settled
focus indicator against the padding box of every ancestor that actually clips it.

---

## What the sweep found

The bead named one instance. A grep for `overflow: hidden` produced twelve
candidate components. **Eleven of the twelve never clip anything**, and the
measurement found five defects the grep could not have ranked — including one in
a component the grep never flagged at all:

| # | control | clipped by | cut |
|---|---|---|---|
| 1 | all three `.autonomy-segment`s | `.autonomy-segments` `overflow:hidden` | top+bottom on all, plus the outer edge on the first and last |
| 2 | `autonomy-confirm`, `autonomy-cancel`, `autonomy-reason` | `.autonomy-confirm-row` `overflow:hidden` | 4px |
| 3 | `pause-confirm`, `pause-cancel` | `.pause-confirm-row` `overflow:hidden` | 4px |
| 4 | a row at the scroll boundary | `.modal__body` `overflow:auto` | 4.2px |
| 5 | `cap-workload-*-summary` | `.cap-workload` `overflow:hidden` | **all four sides** |

(2) and (3) are the same defect in two components, and neither was reachable
until the suite gained a state that opens a confirm row. (4) is a *scroll*
container, which the bead did not consider at all: `scrollIntoView` aligns a
focused element's border box, not its ring.

(5) is the worst of the set and the last one found — a keyboard user tabbing the
capabilities modal's workload list saw **no indicator at all**. It was missed
twice over: the first version of that state used the shared `{ capabilities: [] }`
mock, which renders no `<summary>` rows, so the test passed while covering
nothing; and the second used a payload missing `human_gates`/`denylist`, which
CapabilityCard correctly routes to its error row — again rendering no summaries.
A state that cannot reach the thing it exists to check is worse than no state,
because it reads as coverage.

## The fixes

**1. The segmented dial takes an inset ring.** Its container's `overflow: hidden`
is load-bearing (it clips the sliding pill and rounds the corners), and padding
it would break the flush segmented look. So the ring moves inside:

```css
--ds-ring-inset-on-light: 2px solid var(--ds-stream-ink);
```

```css
.autonomy-segment:focus-visible {
  outline: var(--ds-ring-inset-on-light);
  outline-offset: -3px;
  box-shadow: none;
}
```

*Why `outline` and not an inset `box-shadow`:* `.autonomy-segment--armed` already
owns `box-shadow`, the property does not compose across rules, and this rule
outranks the armed one — a box-shadow ring here would erase the armed indicator
at exactly the moment you are armed and focused.

*Why `-3px` and not `-2px`:* a 2px outline at `-2px` occupies the outermost 2px
band, which is where the armed state's `inset 0 0 0 1px` stroke lives. Measured,
not reasoned about: rendering it that way produced **zero pixels of `#4285f4`**
in the whole control. At `-3px` the edge walk reads border `#d6d2ca`, armed
`#4285f4`, ring `#2a63c9` ×2, then the armed tint.

*Why one tone when `--ds-ring` needs two:* `--ds-ring` is drawn outside, so it
abuts both the page ground and the control fill, and the palette holds controls
at both ends of the ramp. Drawn inside a control whose every fill is light, one
dark tone clears the floor against all of them: surface `#ffffff` 5.635,
surface-2 `#f6f5f1` 5.165, stream-surface `#ecf3fe` 5.050, armed-over-white
`#f4f8fe` 5.288, border-strong `#d6d2ca` 3.738. **The name carries the
precondition** so the token cannot be casually reused on a dark control.

**5. The workload summary takes the same inset ring.** `.cap-workload`'s
`overflow: hidden` rounds the summary's `--ds-neutral-surface` fill against the
container border, so it is load-bearing exactly as the dial's is.
`--ds-neutral-surface` (#efeeea) reads 4.854:1 against `--ds-stream-ink`, so the
on-light precondition holds — and because the token now has **two** consumers,
`contrast.test.ts` proves that precondition for *every* consumer it finds in the
source, not just the one it was written for.

**2 & 3. Delete `overflow: hidden` from both confirm rows.** It was only ever
needed for `transition:slide`, and Svelte's slide emits its own `overflow: hidden`
for the duration of the animation (`svelte/src/transition/index.js:139`). The
static rule bought nothing at rest and broke the ring permanently.

**4. `scroll-padding: 4px` on `.modal__body`.** Padding does not help — `overflow`
clips at the *padding box*, which is outside the padding. `scroll-padding` insets
the snapport itself. This fixes the keyboard path, which is the one that matters
for a focus ring; it is advisory to scrolling and does not reserve space, so a
wheel-scroll while a row is focused can still clip.

## Five ways the probe was confidently wrong

Worth recording because they run in **both** directions — a guard that only fails
safe is still lying, and three of these were false *negatives*:

1. **False negative.** `.ds-btn` transitions `box-shadow`, so a ring read straight
   after Tab is mid-flight and computes to `rgba(255,255,255,0.275) … 0.547px` —
   an outset of ~0.5px, which a clipping check scores as "fits". Every
   transitioned control was silently cleared.
2. **False positive.** The search modal is a top-layer `<dialog>`, so `.rails`
   cannot clip it — yet a naive ancestor walk reported all 32 rows as "clipped by
   .rails by 658px". Fixed with a containment precondition.
3. **False negative.** Any persistent shadow counted as a focus indicator, so an
   elevation shadow or an armed stroke satisfied a control that does nothing at
   all on focus. The indicator must now differ from the element's resting style.
4. **False positive.** That resting snapshot was taken *after* a modal had
   autofocused its input, recording the focused style as "resting" and then
   accusing the control of not changing.
5. **False positive, and flaky.** A CSS-only motion override does not stop
   Svelte's JS transitions, so popovers were measured mid-slide — 34px tall on
   the way to 186px, buttons flush with the container edge. Three runs gave three
   different heights and three different "cut" figures. Fixed by using the app's
   own `prefers-reduced-motion` path, which zeroes CSS transitions *and* makes
   `motionMs()` return 0.

## What is pinned

- `tests/smoke/focus-ring.smoke.ts` — 11 states (desk, desk in **Japanese**, chat,
  the dial, an **armed** segment, the pause banner's confirm row, the pause
  popover, the scrolled search modal, the capabilities modal, and both views at
  390px). Asserts: nothing clipped; every control has an indicator that actually
  *changes* on focus and paints a non-zero alpha; every Tab-reached control really
  matches `:focus-visible`; and positive controls — minimum stop counts, a
  per-state sentinel testid, and a traversal that must return to its FIRST stop,
  so neither the Tab cap nor a focus trap can pass as coverage.
- `tests/unit/contrast.test.ts` — the inset ring's **premise**, at two levels.
  Every background the dial can take resolves to something clearing 3:1, with
  `transparent` and `color-mix(…, transparent)` resolved explicitly against the
  pill and popover behind them. And separately, **every consumer of the token
  found in the source** must be drawn on a light fill — because a precondition
  proven for one caller and assumed for the rest is the shape of bug this file
  exists to stop, and the token acquired its second caller inside this change.
  Fail-closed on any colour notation it cannot resolve, and on any rule that
  declares two competing backgrounds.
- The one exemption (`chat-prompt`, no indicator by design) is allowlisted by
  testid and cites **ds-tr5**, so it is visible rather than silent.

## Verification

- 1914 unit tests, `svelte-check` 0 errors, `npm run build`, full `test:smoke` 45/45
- **17 defect injections**, each reddening only its own test: the outward ring
  restored on segments and on the workload summary; `outline-offset` flipped
  positive; `overflow:hidden` put back on each confirm row; `scroll-padding`
  removed; the composer allowlist entry deleted; a segment fill turned dark; the
  token's grammar widened; a fill in an unresolvable notation; three states
  renamed out of the premise sweep; a second consumer's fill turned dark, then
  given no background at all, then given two competing background declarations
- Pixel-walked screenshots of the focused dial at all three positions and armed

Two of those injections found holes in **my own guards**, both on the coverage
check: "at least four substrates" still passed when a state was renamed away, and
its replacement `selector.includes('--active')` passed via the hover rule's
`:not(.autonomy-segment--active)` — the modifier appearing inside a `:not()`,
naming the state it *excludes*. Anchored on the rule's subject with `:not()`
stripped.

## What this does NOT prove

- Chromium only; a ring clipped solely in WebKit passes.
- Only the swept states. Coverage is exactly the state list.
- Axis-aligned boxes against DOM ancestors only: no rounded-corner clipping,
  occlusion by siblings, clipping at the visual viewport, or transform scaling.
  On the corner: the band spans 1px..3px inside the border box, and against the
  r=4px arc its OUTER corner sits 0.243px outside — a quarter-pixel clip at the
  extreme corner, invisible in the captures. An earlier note did this sum on the
  inner edge (1.414px, comfortably inside) and drew the right conclusion from the
  wrong edge.
- **Anything that only happens while animating.** The suite runs under
  `prefers-reduced-motion`, which is what makes popover geometry deterministic —
  and which also means a ring clipped only mid-transition is invisible to it.
  Not hypothetical: AutonomyPill focuses its reason input on the tick after the
  confirm row mounts, so on the normal 200ms path that input is focused while
  Svelte's own injected `overflow: hidden` is still on the row (**ds-b74**).
- Nothing about whether an indicator is *attractive*, only that it exists, is
  unclipped, changes on focus, and clears 3:1.
