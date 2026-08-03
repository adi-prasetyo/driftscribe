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

| candidate | palette colors it fails against |
|---|---|
| current `rgba(66,133,244,.30)` | all 32 — composites to 1.37–1.53 everywhere |
| opaque `--ds-stream` | **12** — every border, every semantic fill, seal |
| opaque `--ds-stream-ink` | **7** — ok/warn/danger fills and inks, navy, seal |

Ring luminance would need to be ≤0.183 to clear `--ds-border-strong` and ≥0.478
to clear `--ds-ok`. Empty interval. **No third value rescues it** — which is why
the earlier "reject a fourth blue" reasoning was answering the wrong question.

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

Outer vs inner is 5.64, so the layers read as two. Worst figure anywhere is
**3.56**, against 3.069 for the best single color — more robust *and* more
headroom. `--ds-seal` stops needing a scope argument: the inner band covers it.

Cost: a 4px ring where the old was 3px. `box-shadow` does not affect layout but
IS clipped by an ancestor's `overflow: hidden`.

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
- the outer band clears 3:1 on all eight grounds it is drawn on
- the layers clear 3:1 against **each other**
- **every color in the palette is carried by some layer** — swept rather than
  listed, because a curated list cannot notice a newly added filled control,
  which is the omission that produced ds-dce
- every numeral color clears 3:1 at rest
- no hover/focus rule attenuates the numeral, failing **closed** on an opacity
  it cannot parse

Verified by 8 injections, each reddening only its own test — including the
bead's own proposed single-color ring, `opacity: var(--x)`, and the fade moved
up to the parent stat.
