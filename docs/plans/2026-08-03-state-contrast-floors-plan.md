# State contrast floors — design record (ds-dce, ds-16e, ds-b42)

**Goal:** Design-token defects that fail *silently*, and mostly only in a
transient state — fix them, and pin the arithmetic so a future palette re-tune
cannot re-break them.

**Architecture:** Two are contrast floors that hold at rest and fail only while a
control is focused (ds-dce) or pointed at (ds-16e); the third is a token that
does not exist (ds-b42). The durable half is a contrast helper that reads the
REAL declared values out of `tokens.css`, so the pins track the palette instead
of a copy of it.

> Revised after review. The first draft proposed a single opaque ring, which is
> wrong; §1 records why. Findings that contradict the beads are kept here
> because the implementation follows the audit, not the bead.

---

## 1. ds-dce — no single color can be the ring

A focus ring abuts **two** things: the ground outside it, and the control
inside it. The first draft mixed those models — it counted `--ds-navy` as an
adjacent color but not `--ds-border-strong` — and that inconsistency is what
made a single color look sufficient.

Applied consistently, the palette holds controls at *both* ends of the ramp:
paper-light borders and near-black fills. The two constraints are contradictory.

Two sets are in play and they must not be conflated. The **adjacency set** (21
colors: the eight grounds, the borders, the fills) is the design argument; the
**full palette** (all 32 opaque tokens) is what the test sweeps, deliberately
wider so it needs no maintenance.

| candidate | fails / 21 adjacency | fails / 32 palette |
|---|---|---|
| current `rgba(66,133,244,.30)` | 21 — 1.37–1.53 everywhere | 32 |
| opaque `--ds-stream` | **12** | **22** |
| opaque `--ds-stream-ink` | **7** | **17** |

**No third value rescues it**, and the interval is provably empty — which is why
the earlier "reject a fourth blue" reasoning was answering the wrong question.
Three constraints are needed, not two:

| token | admissible ring luminance |
|---|---|
| `--ds-navy` | ≥0.15110 — its darker branch is negative, so it forces the light side |
| `--ds-ok` | ≥0.44033 **or** ≤0.00448 — navy eliminates the second branch |
| `--ds-border-strong` | ≤0.18218 — its lighter branch exceeds 1 |

0.44033 > 0.18218. Citing `--ds-ok` alone would have been an incomplete proof:
it leaves a near-black ring admissible, and only `--ds-navy` closes that branch.

**Adopted — a two-tone ring.** Each layer takes the end of the ramp the other
cannot, and WCAG lets a multi-color indicator satisfy the floor when *one*
component does:

```css
--ds-ring: 0 0 0 2px var(--ds-surface), 0 0 0 4px var(--ds-stream-ink);
```

| | carries | range |
|---|---|---|
| inner 2px `--ds-surface` | dark fills — navy 15.66, ok 6.42, warn 5.41, seal 5.44 | ≥5.41 |
| outer 2px `--ds-stream-ink` | the eight light grounds, and the light borders | 3.56–5.64 |

Outer vs inner is 5.64, so the layers read as two. Worst over the adjacency set
is **3.56** against 3.069 for the best single color; over all 32 tokens it is
**3.216** (white against `--ds-faint`, a text ink a ring is unlikely to abut).
`--ds-seal` stops needing a scope argument: the inner band covers it.

**Geometry is load-bearing, and colors alone do not capture it.** Paint order is
declaration order, so a first layer with a *larger* spread covers the ones
behind it: swapping the two spreads yields a ring whose blue band is entirely
hidden, invisible on a white control, with every color assertion still passing.
And that is only the first of the family. A geometry check that *scans* for
numbers accepts `1em` as smaller than `2px` while it is 16px, reads digits out
of `calc()`, lets a bare number or `%` through (invalid CSS: the browser drops
the declaration and renders no ring at all), ignores a fifth length, and accepts
two colors in one layer. So each layer is matched **anchored** as four canonical
px lengths plus exactly one color, empty entries and unbalanced parentheses are
surfaced rather than discarded, and the spreads are pinned to exactly 2px/4px —
bounds only invite the next near-miss (3.5px/4px leaves half a pixel of blue;
100000px/100002px clears any minimum while putting the ring nowhere near the
control). The "outer" band is derived by widest spread, not declaration
position, so that assertion does not quietly depend on the geometry pin holding.

Cost: a 4px ring where the old was 3px. `box-shadow` does not affect layout but
IS clipped by an ancestor's `overflow: hidden` — `AutonomyPill`'s segmented
control is a pre-existing instance, untouched by this change.

## 2. ds-16e — the bead names a numeral that never faded

The fade rule was `.instrument-band__stat:not(--static):hover .__num`, and
`awaiting` is declared `interactive: false` (`InstrumentBand.svelte:166`), so it
carries `--static` and was **excluded**. The awaiting numeral — the bead's title
and its only cited measurement — could not fade.

The real defect is on `[data-unknown]`, which the bead never counted: it takes
`--ds-faint`, and `data-unknown` (`n === null`) is independent of `--static`, so
it lands on managed and drift, both interactive. `ApprovalDesk` passes `null` for
both whenever the graph is unavailable.

Reachable fading numerals, on paper, 44px/600 → 3:1 floor:

| numeral | rest | @.75 (was) | @.91 | @.98 | @.99 |
|---|---|---|---|---|---|
| managed `--ds-navy` | 15.016 | 7.051 | 11.841 | 14.333 | 14.678 |
| drift `--ds-warn` | 5.190 | 3.229 | 4.363 | 4.993 | 5.090 |
| unknown `--ds-faint` | 3.083 | **2.225** | **2.731** | **3.000** | 3.041 |
| *(awaiting `--ds-stream` — static, never fades)* | 3.416 | — | — | — | — |

`--ds-faint` needs alpha ≥ **~0.981**. The first draft claimed "only 1.0 works";
that was wrong, and the correction does not change the outcome — a fade nobody
can perceive is not a fade. **Removing it is the only honest option.** The hover
hint under the label (ds-7ag.2) is a separate element and still carries the
affordance.

## 3. ds-b42 — and a fourth instance the guard found

`--ds-shadow-md` is declared nowhere, so three popovers silently took the
`var()` fallback — `--ds-shadow-sm`, the lightest tier — while `tokens.css` names
popovers as one of the three things that earn elevation.

Generalizing that into a guard ("no `var()` reads an undeclared `--ds-*`")
immediately found a fourth: `PrBodyDisclosure.svelte:202` read
`var(--ds-accent, var(--ds-fg))`. `--ds-accent` has never existed, so links
inside a rendered PR body were painted in **body ink** — distinguishable from
prose by their underline alone. Now `--ds-stream-ink`, matching bare `a`.

The pre-existing retired-token test could not have caught either: it needs a
human to add each dead name, and a token that never existed was never on a list.

## 4. What is pinned

`tests/unit/contrast.ts` + `contrast.test.ts`, all figures derived from source:

- every ring layer is opaque (the original defect, as a property not a number)
- the ring's **geometry**: exactly two non-inset layers, each matched anchored
  as four canonical px lengths plus exactly one color, zero offset and blur,
  and the canonical 2px/4px spreads pinned exactly rather than by bounds
- the outer band clears 3:1 on all eight grounds it is drawn on
- the layers clear 3:1 against **each other**
- **every color in the palette is carried by some layer** — swept rather than
  listed, because a curated list cannot notice a newly added filled control,
  which is the omission that produced ds-dce
- every numeral color clears 3:1 at rest
- the numeral cannot be attenuated by **any** mechanism: every `opacity` /
  `filter` / `animation*` declaration (case-insensitively, vendor prefixes
  included), a custom property set under `:hover`/`:focus` that a base rule
  could read, and a translucent `color` — failing **closed** on anything it
  cannot prove harmless. Scoped to the numeral and every element that can
  CONTAIN it (`.instrument-band`, `__stats`, `__stat`, `__num`), since opacity
  on an ancestor fades it just as surely; siblings like `__label`, which carries
  a legitimate hover fade for the hint swap, are excluded. Ancestors are checked
  whether or not the rule is interactive: a RESTING `.instrument-band__stat {
  opacity: .75 }` attenuates the numeral permanently rather than transiently,
  and evades the rest-contrast test too, which measures the declared color and
  knows nothing about an ancestor's opacity. Reads the `<style>`
  block only, so template markup is never brace-matched as CSS

Verified by **43 injections**, each reddening only its own test: the bead's
proposed single-color ring; spreads swapped, equalised, sub-pixel, half-pixel,
or 100000px; `inset` (upper and lower case), blur, offset; `1em` against `px`,
`calc()`, a bare number, `%`, a fifth length, three layers, two colors in one
layer, a trailing comma, unbalanced parentheses; `opacity: var(--x)`,
`opacity: 1` then `filter: opacity(.5)`, `animation-name` alone,
`Animation-Name`, `-webkit-animation-name`, a bare `:focus`, a custom property
set on hover and read by a base rule, a translucent `color`, the fade moved up
to the parent stat, `opacity` on the `.instrument-band` / `__stats` / `__stat`
ANCESTORS (transient *and* resting, which is worse), and an uppercase `:HOVER`.

Plus `rgba(255,255,255,1.)` — invalid CSS, because a decimal point needs a digit
after it, so the browser drops the declaration; `Number('1.')` is 1 and a
lenient parser reports a perfectly opaque white. Paired with a control
(`rgba(255,255,255,1)`) that must still pass, so the pin is proven to reject the
invalid number rather than the notation.

**What the sweep does not prove.** It covers the 32 tokens declared as direct
`#rrggbb`. It would not automatically see a token later written as `rgb()`,
`oklch()`, `color-mix()` or an alias; a color hard-coded in a component; a
translucent ground; or a ring clipped by an ancestor's `overflow: hidden`. It is
a strong floor, not a universal visibility proof.
