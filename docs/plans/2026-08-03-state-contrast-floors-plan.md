# State contrast floors — implementation plan (ds-dce, ds-16e, ds-b42)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Three design-token defects that fail *silently* and only in a transient
state — fix them, and pin the arithmetic in a test so a future palette re-tune
cannot re-break them.

**Architecture:** Two of the three are contrast floors that hold at rest and fail
only while the control is focused (ds-dce) or pointed at (ds-16e); the third is a
token that does not exist (ds-b42). The durable half of the change is a contrast
test helper that reads the REAL declared values out of `tokens.css`, so the pins
track the palette instead of a copy of it.

**Tech Stack:** Svelte 5, vitest, plain CSS custom properties.

---

## Findings that change the fix

The audit contradicts both bead descriptions. Recorded here because the
implementation follows the audit, not the bead.

### ds-dce: `--ds-stream-ink` is NOT the safer choice

The bead's design note says "Codex notes `--ds-stream-ink` would give more
headroom." Against light grounds it does. Against a **navy filled control**
(`--ds-navy #0e1b5f`, the Send button and the desk approve CTA) it measures
**2.780:1 and fails.** Raising contrast against paper lowers it against navy —
the two constraints pull in opposite directions.

Ring luminance must satisfy both. For ≥3:1 against the neutral chip (`#efeeea`,
the lightest ground) *and* ≥3:1 against navy, relative luminance must land in
**[0.154, 0.248]**. `--ds-stream` (#4285f4) sits at 0.2446 — inside, near the top.
`--ds-stream-ink` (#2a63c9) at 0.0868 is below the floor.

| ring | paper | white | surface-2 | neutral chip | ok | warn | danger | stream | NAVY |
|---|---|---|---|---|---|---|---|---|---|
| current `rgba(66,133,244,.30)` | 1.397 | 1.416 | 1.391 | 1.374 | 1.374 | 1.380 | 1.385 | 1.376 | 1.536 |
| **opaque `--ds-stream`** | 3.416 | 3.564 | 3.267 | **3.069** | 3.145 | 3.150 | 3.167 | 3.193 | 4.395 |
| opaque `--ds-stream-ink` | 5.402 | 5.635 | 5.165 | 4.854 | 4.973 | 4.982 | 5.008 | 5.050 | **2.780 FAIL** |

**Vermilion is out of scope, and that is a finding, not an omission.** The ring
measures 1.526 against `--ds-seal`. But `--ds-seal` is only ever a `color` or a
`border` — `grep` finds no `background: var(--ds-seal)` anywhere. No focus ring is
ever drawn on vermilion, so the ground does not exist. If a filled seal is ever
introduced, this floor breaks; the test comment must say so.

**Rejected: a fourth blue.** Balancing the two constraints exactly
(luminance 0.1967) would yield 3.628 on *both* worst grounds instead of
3.069/4.395. It is measurably better and still wrong: `tokens.css` governs blue to
exactly three jobs, and 0.5 ratio points does not buy a fourth. Record the 3.069
margin (2.3% over the floor) instead of hiding it.

### ds-16e: raising the fade to `.91` does not fix it

The bead says "Either raise the fade to ~opacity:.91 ... or drop the numeral fade
entirely," and checks "the other two stats" — managed (navy) and drift (amber).
There is a **fourth** numeral color it does not count:
`.instrument-band__stat[data-unknown] .instrument-band__num` takes `--ds-faint`
(`InstrumentBand.svelte:317`).

`data-unknown` is set from `n === null`; `--static` is set from
`!spec.interactive` (`:213`, `:217`). They are **independent**, so an interactive
stat with an unknown value is a hoverable `<button>` with a faint numeral.

Contrast on paper, 44px/600 → 3:1 large-text floor:

| numeral | rest | @.75 (today) | @.91 (bead's fix) | @1.0 |
|---|---|---|---|---|
| managed `--ds-navy` | 15.016 | 7.041 | 11.850 | 15.016 |
| drift `--ds-warn` | 5.190 | 3.224 | 4.370 | 5.190 |
| awaiting `--ds-stream` | 3.416 | **2.462** | 3.021 | 3.416 |
| unknown `--ds-faint` | 3.083 | **2.235** | **2.719 FAIL** | 3.083 |

So the alpha option is dead on arithmetic: `--ds-faint` needs alpha **1.0** to
clear 3:1 — it rests at 3.083, only 2.8% over the floor, and *any* fade sinks it.
**Removing the numeral fade is the only fix that clears all four.** That is also
what the bead author and Codex preferred on taste; now there is a reason.

The affordance survives: the hover hint under the label (`ds-7ag.2`) is a separate
element with its own fade and is untouched.

---

### Task 1: Contrast helper + failing pins

**Files:**
- Create: `frontend/tests/unit/contrast.ts`
- Create: `frontend/tests/unit/contrast.test.ts`

**Step 1: Write the helper.** Pure functions, no framework:
`relativeLuminance(hex)`, `contrastRatio(a, b)`, `composite(fg, alpha, bg)`, and
`readToken(css, name)` which pulls a declared value out of `tokens.css` source so
the pins read the real palette rather than a stale copy.

**Step 2: Write the failing tests.**

```ts
const FLOOR = 3.0; // WCAG 1.4.11 non-text / 1.4.3 large text

// Every ground the ring is DRAWN on, plus every component color it sits
// ADJACENT to. --ds-seal is deliberately absent: it is only ever a color or a
// border, never a background, so no ring is drawn on it. Add it here the day a
// filled vermilion surface appears - the ring measures 1.526 against it.
const RING_GROUNDS = ['--ds-bg', '--ds-surface', '--ds-surface-2',
  '--ds-neutral-surface', '--ds-ok-surface', '--ds-warn-surface',
  '--ds-danger-surface', '--ds-stream-surface', '--ds-navy'];

it('focus ring clears 3:1 on every ground it renders against', () => { ... });
it('every instrument-band numeral clears 3:1 at rest AND while hovered', () => { ... });
```

The numeral test must derive the hover alpha from `InstrumentBand.svelte` source
(regex the `opacity` in the `:hover .instrument-band__num` rule), defaulting to
1.0 when the rule is absent — so deleting the rule is what makes it pass, and
re-adding any fade re-reddens it.

**Step 3: Run — expect FAIL.**
`npm run test:unit -- --run contrast` → ring 1.397 vs 3.0, faint numeral 2.235.

**Step 4: Commit the failing test** (`test:` prefix, so the red is in history).

### Task 2: ds-dce — opaque ring

**Files:** Modify `frontend/src/styles/tokens.css:176`

```css
/* Focus ring. OPAQUE, not a tint: at 30% alpha this composited to 1.37-1.54:1
   on every ground and failed WCAG 1.4.11's 3:1 everywhere (ds-dce). --ds-stream
   is the only palette blue that clears the floor on BOTH sides - the light
   grounds and a navy filled control. --ds-stream-ink looks safer and is not:
   it measures 2.780 against --ds-navy. Worst case is 3.069 on the neutral chip,
   2.3% of headroom, so re-tuning --ds-stream requires re-running the pins in
   tests/unit/contrast.test.ts. */
--ds-ring: 0 0 0 3px var(--ds-stream);
```

**Verify:** ring test passes; `npm run test:unit -- --run` stays green.

### Task 3: ds-16e — delete the numeral fade

**Files:** Modify `frontend/src/components/InstrumentBand.svelte:280-284`

Delete the `:hover .instrument-band__num { opacity: .75 }` rule and replace the
comment with why there is no fade: `--ds-faint` (the unknown numeral) rests at
3.083 and any alpha below 1.0 sinks it under 3:1; the hover hint carries the
affordance instead. Keep the `transition: opacity` on `__num` — the numeral tick
animation (ds-wd2.13) will want it.

**Verify:** numeral test passes; `InstrumentBand.test.ts` stays green.

### Task 4: ds-b42 — the shadow token that does not exist

**Files:** Modify `DemoNoticeBell.svelte:309`, `PausePill.svelte:248`,
`AutonomyPill.svelte:506` — `var(--ds-shadow-md, var(--ds-shadow-sm))` →
`var(--ds-shadow)`.

`--ds-shadow-md` is declared nowhere, so all three silently take the fallback:
`--ds-shadow-sm`, the LIGHTEST tier — while `tokens.css` names popovers as one of
the three things that earn elevation. Add a `styles.test.ts` pin that no
component references an undeclared `--ds-*` token, which catches the whole class.

### Task 5: Verification

- `npm run test:unit -- --run`, `npm run check`, `npm run build`
- `uv run --with ruff ruff check .`
- Screenshot pass: focus each of button / link / input / textarea / pill in both
  locales; hover each instrument-band stat including an unknown one.
- **Defect injection:** revert each of the three fixes in turn, confirm only its
  own test reddens.
