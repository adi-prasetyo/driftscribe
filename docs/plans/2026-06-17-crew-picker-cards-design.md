# Crew picker cards — design (2026-06-17)

## Why

PR2 shipped the per-agent `CrewGlyph` animation, but it lives only inside the
`CapabilityCard` — two collapsed `<details>` deep, closed by default. The
operator's verdict: *"where is the animation, I can't find it."* The motion is
real and deployed (live rev `00090-x2h`), just invisible.

This change surfaces the crew at the **chat composer** and gives the motion a
*job*: it becomes the selection indicator. Decided with the operator:

- Four small **mini cards** replace the native `<select>` workload picker.
- They sit **above the text field** ("who → what" reading order).
- **The selected card animates; unselected cards rest on the static frame.**
- The **whole strip collapses** to a compact chip of the selected crew member.

## Design decision: motion = selection (a deliberate reversal)

The PR2 design doc §5 said "all four equally lively; autonomy is NOT in the
motion." This change keeps *autonomy out of the motion* but makes **selection**
the thing motion signals. At the composer only one agent is "in use" at a time,
so active = alive reads as meaningful state, not decoration. Autonomy still
lives on a static marker (the "auto" pill on Anchor), never the motion.

## Components

### 1. `CrewGlyph.svelte` — add an `animated` prop (minimal blast radius)

- New prop `animated: boolean = true`. Default `true` ⇒ `CapabilityCard` is
  **completely unchanged** (all four keep looping there).
- Root `<svg>` gains `class:crew-glyph--animated={animated}`.
- One added CSS rule gates motion off when the class is absent:
  ```css
  .crew-glyph:not(.crew-glyph--animated)
    :is(.anchor-node, .patch-crack, .patch-bandage, .patch-check,
        .prov-block, .prov-branch, .prov-commit, .scan-dot, .scan-band) {
    animation: none;
  }
  ```
  Specificity of this override (≥ 0,3,0) beats the bare `.x { animation }`
  rules (0,1,0), so `animated=false` → every element rests on its **base CSS
  state, which IS the healthy frame** (the same frame `prefers-reduced-motion`
  already lands on — proven in PR2). No keyframe edits, no restructuring.
- Reduced-motion path unchanged: the existing local `@media reduce` block + the
  global `base.css` `!important` reset both still neutralise motion when
  `animated=true`. The new `:not()` rule only fires when already un-animated, so
  it never collides with the reduce rules.

### 2. `CrewPicker.svelte` — NEW

Bindable props: `value: Workload` (`$bindable`), `disabled = false`.

State: `collapsed` — persisted in `localStorage` (`driftscribe_crew_collapsed`),
**default expanded** so first-time operators see the cards.

**Expanded** — a native radio group (a11y for free: arrow-key nav, SR labels):
```
<fieldset> (visually-hidden <legend> "Choose a crew member")
  for each WORKLOADS entry:
    <label.crew-card class:--selected>
      <input type="radio" name="crew" bind:group={value} value={wl.value} {disabled} (sr-only)>
      <CrewGlyph verb={wl.value} animated={value===wl.value && !disabled} size=28 />
      <span.crew-card__name>{wl.name}</span>
      if wl.group==='autonomous': <span.crew-card__auto> "auto" (ds-pill ok, title + SR text)
    </label>
  <button.crew-picker__collapse> chevron → collapsed=true
```
- Native `<input type=radio>` keeps the picker keyboard- and screen-reader-
  accessible (what the old `<select>` gave for free). `bind:group={value}`
  drives both selection and the `animated` prop reactively.
- Only the autonomous crew member (Anchor/`drift`) shows the "auto" marker —
  replaces the old optgroup + adjacent badge. Backend remains the source of
  truth (`AUTONOMOUS_TRIGGER_WORKLOADS`); the catalog `group` already pinned to
  it by the cross-surface test.

**Collapsed** — a single chip button:
```
<button.crew-chip aria-expanded=false aria-label="Crew: {name} — expand to change" {disabled}>
  <CrewGlyph verb={value} animated={!disabled} size=24 />
  <span>{selected.name}</span>
  if autonomous: "auto"
  <Icon chevron-down />
</button>  → click expands (collapsed=false)
```
The active agent stays alive even in the chip (it IS the selection); `disabled`
(historical replay) → static.

### 3. `ChatForm.svelte` — swap the control

- Remove the `<select id="workload-select">`, both `<optgroup>`s, and the
  adjacent `workload-camp` badge + their derived helpers
  (`autonomousOptions`, `onDemandOptions`, `selectedIsAutonomous`).
- Render `<CrewPicker bind:value={workload} disabled={disabled} />` as a
  **full-width row above** the input (wrapper `flex: 1 1 100%`), so layout
  becomes: `[ crew cards ]` / `[ input ] [ Send ]`.
- Keep `id="chat-form"`, the `data-tour="composer"` wrapper, `.historical`
  dimming, `:focus-within` lift, and the `onSubmit(prompt, workload)` contract
  all unchanged. Prefill still sets `workload` → the bound radio selects.

## Tests

- **CrewGlyph.test.ts** (+): default renders `crew-glyph--animated`;
  `animated={false}` omits it. Existing 6 unchanged.
- **CrewPicker.test.ts** (NEW): 4 radio cards (drift/upgrade/explore/provision);
  selected card's glyph has `crew-glyph--animated`, others don't; selecting a
  radio moves the animated class + updates bound value; "auto" marker only on
  Anchor; collapse → only the selected chip shows; expand restores cards;
  collapsed chip shows the selected name + animated glyph; `disabled` → radios
  disabled + glyphs static; localStorage persists collapsed. (No jsdom
  arrow-key assertion — jsdom doesn't implement native radio arrow nav; rely on
  native semantics + assert radio roles/attributes instead.)
- **ChatForm.test.ts** (rewrite the broken bits): prefill tests assert the
  checked radio (`input[name=crew]:checked`) instead of `#workload-select`;
  replace the optgroup/camp describe block with: picker present, "auto" marker
  on Anchor only, selecting a card + typing + submit → `onSubmit(text, value)`.

## Non-goals / frozen

- Symbolic values `drift/upgrade/explore/provision` and the `/chat` contract:
  **frozen**. Cards select `wl.value`, display `wl.name`.
- `CapabilityCard` placement of `CrewGlyph`: **unchanged** (still there, still
  all four looping). This adds a second, prominent surface; it doesn't move the
  first.
- No backend changes. No new icons (chevron-down already in the registry).

## Revision (2026-06-17, after operator review of the first build)

The operator reviewed the working build and simplified it. Three changes,
which also resolved both of Codex's completed-work findings:

- **No autonomy marker on the cards.** The "auto" pill read as imbalanced, and
  the honest autonomy signal already lives in the "what this agent can — and
  cannot — do" card. The composer picker is now purely identity + selection.
  (Resolves Codex's "descriptor dropped" framing by re-adding the descriptor
  instead — see next.)
- **Descriptor on hover/focus instead.** Each card stays compact (glyph +
  name); its domain descriptor (`Cloud Run config`, `dependencies`, `read-only`,
  `infra edits`) surfaces as a tooltip on hover/keyboard-focus AND is wired as
  the radio's `aria-describedby`, so assistive tech announces "Anchor, Cloud Run
  config" regardless of hover. (Restores the descriptor Codex flagged as lost.)
- **No collapse.** The cards render small enough that collapsing isn't needed,
  so the whole collapse-to-chip mechanism (+ its localStorage preference) is
  dropped. (Resolves Codex's main must-fix — keyboard focus was lost when the
  focused collapse/expand button unmounted; with no toggle there's no focus
  drop.)

Net: `CrewPicker` is just a `<fieldset>` of four label-wrapped radios with a
descriptor tooltip; no `collapsed` state, no chip, no Icon import.

## Ship

Additive frontend-only PR → svelte-check 0 + vitest green + vite build →
Playwright visual-verify on the local DRY_RUN rig (select each card, confirm
only it animates; collapse/expand; reduced-motion static; mobile reflow) →
Codex completed-work review → squash-merge → coordinator rebake
(`cloudbuild.coordinator-update.yaml`) → traffic shift to 100% → live-verify.
