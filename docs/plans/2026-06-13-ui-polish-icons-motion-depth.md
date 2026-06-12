# UI polish — icons, motion, gentle depth (2026-06-13)

## Why

Operator feedback on the live SPA (judge-facing for the hackathon):

1. **The autonomy dial feels broken.** Clicking a segment gives zero feedback on the
   segment itself — only a confirm row appears below ("Switch to Propose?"), and the
   active highlight stays on the old segment until the POST round-trips. The
   confirm-first design is right (it gates agent autonomy); the *communication* is
   wrong: no armed state, no motion, the highlight teleports.
2. **The whole UI reads "too minimalist".** Zero icons anywhere (survey confirmed: not
   a single authored `<svg>` in any component), text-only buttons, flat cards.

Direction chosen by the operator (option A of three): **icons + motion + gentle
depth — palette stays light/editorial, no layout changes, no dashboard redesign.**

## Scope guard

- Frontend-only (`frontend/src/**`). No backend, no API contract, no copy changes to
  the pinned blurb strings (AutonomyControl.test.ts pins them by substring).
- `styles/base.css` is shared with the Jinja approval pages — changes there must be
  **additive only** (new classes); do not alter existing `.ds-*` semantics.
- All motion gated by reduced-motion: CSS transitions are already neutralized
  globally (base.css `@media (prefers-reduced-motion)`); Svelte JS transitions use
  the existing `motionMs()` helper (`lib/motion.ts`), same as Timeline.

## 1. Icon system (new)

**`frontend/src/lib/icons.ts`** — vendored Lucide path data (ISC license, attribution
comment with upstream version). `export const ICON_PATHS: Record<IconName, string>`
where each value is the inner markup of a 24×24 stroke icon (SVG child elements
from the allowed tag set only — `path|circle|rect|line|polyline|polygon` — no
`<svg>` wrapper). `export type IconName = keyof typeof …`.
No new npm dependency — hand-vendoring ~21 icons keeps the bundle tight and avoids
supply-chain surface.

**`frontend/src/components/Icon.svelte`** — props `name: IconName`, `size = 16`,
optional `extraClass`. Renders:

```svelte
<svg viewBox="0 0 24 24" width={size} height={size} fill="none"
     stroke="currentColor" stroke-width="2" stroke-linecap="round"
     stroke-linejoin="round" aria-hidden="true" focusable="false"
     class="ds-icon {extraClass}">{@html ICON_PATHS[name]}</svg>
```

- `{@html}` is safe here by construction: the registry is a compile-time constant of
  vendored markup; `name` is typed `IconName` so no caller can inject. A unit test
  drift-pins every `ICON_PATHS` value with an **allowlist parser, not just a
  charset** (Codex should-fix — a charset alone still admits `onload=`): only tags
  `path|circle|rect|line|polyline|polygon`, only attrs
  `d|cx|cy|r|x|y|x1|y1|x2|y2|width|height|rx|ry|points`, and explicit negative
  asserts for `<script`, `on[a-z]+=`, `href=`, `xlink`.
- `Icon.svelte` builds its class list without leaking `undefined` when the optional
  extra class is absent (derived string or Svelte class directive — Codex
  should-fix).
- Always `aria-hidden` — icons are decorative; the text labels they sit next to stay.
- `stroke="currentColor"` so icons inherit text color everywhere (muted in eyebrows,
  white in filled buttons).
- `.ds-icon { flex-shrink: 0; vertical-align: -0.125em; }` added to base.css
  (additive; the Jinja pages simply won't use it).

**Icon mapping** (final; implementer vendors exactly these Lucide names):

| Where | Icon |
|---|---|
| Dial: Observe / Propose / Propose+Apply | `eye` / `git-pull-request` / `zap` |
| PauseControl toggle (pause / resume) | `pause` / `play` (replaces the `⏸` emoji) |
| Header logo mark | `radar` |
| Tour button | `compass` |
| TokenStatus pill | `key-round` |
| ChatForm send button | `send` |
| Confirm / Cancel buttons (dial + pause) | `check` / `x` |
| Eyebrow: "Past decisions" (rail) | `history` |
| Eyebrow: "Infrastructure" panel summary | `boxes` |
| Eyebrow: CapabilityCard summary | `shield` |
| Timeline groups: Coordinator / Tools / MCP | `brain` / `wrench` / `cable` |
| TraceBadge copy pill | `copy` |
| Rail row, by action (see §4) | `rotate-ccw` / `git-merge` / `git-pull-request` / `alert-triangle` / `file-text` |

## 2. AutonomyControl rework (the centerpiece)

Keep ALL existing contracts: confirm-first flow, no optimistic update, monotonic
`seq` guard, `data-testid`s, `aria-pressed` as the state source of truth, blurb
copy byte-identical.

**a. Sliding active pill.** The segment container becomes `position: relative` with
an absolutely-positioned `.autonomy-segments__pill` (aria-hidden, `z-index` under
the buttons). Geometry: `bind:this` refs per segment + a measurement `$effect`
sets `transform: translateX()` + `width` from `offsetLeft`/`offsetWidth`;
transition `var(--ds-dur) var(--ds-ease)`.

- **Effect dependency (Codex must-fix 1)**: keying the effect on `currentMode`
  alone is broken — `currentMode` *defaults* to `propose_apply`, so when the GET
  returns `propose_apply` the value never changes and the effect would fire once
  against the loading branch (no segment DOM) and never again when the loaded
  branch mounts. The effect must read **both** `stateKind` and `currentMode`,
  measure only when `stateKind === 'loaded'`, and run after `tick()` so the
  segment refs exist. The `$effect` callback itself stays **synchronous** (Codex
  round-2): it reads the reactive deps, kicks an inner `tick().then(...)` guarded
  by a `cancelled` flag, and returns a synchronous cleanup that disconnects the
  ResizeObserver, cancels the pending rAF, and sets the flag — an async effect
  callback would lose Svelte's teardown semantics.
- **Fallback is opt-in, not opt-out (Codex should-fix)**: the active segment KEEPS
  its current background-swap styling by default; only when the pill has valid
  nonzero measured geometry does the component add a class that makes the active
  segment background transparent and shows the pill. Measurement failure (jsdom,
  or any real browser edge) therefore degrades to exactly today's rendering —
  never to no-highlight-at-all.
- **No first-paint slide**: pill gets its transition class only after the first
  measurement (rAF flag), so mount renders the pill in place.
- **Re-measure** on container resize via `ResizeObserver` — **guarded**
  (`typeof ResizeObserver !== 'undefined'`): jsdom doesn't define it and the
  existing 462-line test suite mounts this component.
- **Cleanup (Codex should-fix)**: the effect/onMount teardown must disconnect the
  ResizeObserver and cancel the pending rAF; no stale callbacks after unmount.
- **jsdom has no layout** (`offsetWidth` = 0): the pill is purely decorative;
  tests assert state via `aria-pressed`, never pill geometry.

**b. Armed state.** While `confirming && pendingMode === mode`, the clicked segment
gets `.autonomy-segment--armed`: inset 1px ring + light tint in the stream hue
(`box-shadow: inset 0 0 0 1px var(--ds-stream); background: var(--ds-stream-surface)`
at partial strength), distinct from the solid active pill. New test: clicking
Propose adds the armed class to that segment and `aria-pressed` stays on the old one.

**c. Animated confirm row.** `transition:slide={{ duration: motionMs(200) }}` from
`svelte/transition` (pattern already used by Timeline's `fly`). If a custom easing
is wanted, it must be a named import from `svelte/easing` (e.g. `cubicOut`) —
otherwise omit and take slide's default (Codex should-fix: no dangling `easing`
identifier). Cancel/confirm removal animates out symmetrically.

**d. Segment icons.** `Icon` (size 14) before each label, inheriting segment color.

**e. Card depth.** `.autonomy-card`: radius `--ds-radius-sm` → `--ds-radius`, add
`box-shadow: var(--ds-shadow-sm)`.

## 3. PauseControl — consistency pass

- Replace the literal `⏸` with `Icon name="pause"`; toggle button gets pause/play icon.
- Same confirm-row `slide` transition as the dial.
- `.pause-card`: radius → `--ds-radius`, `box-shadow: var(--ds-shadow-sm)`.
- **Intentional contract update (Codex must-fix 2)**: the `⏸` is a TEXT node today
  and PauseControl.test.ts:113 exact-pins `'⏸ DriftScribe is paused — …'` in
  `pause-state` textContent (the component comment at PauseControl.svelte:326
  documents this). Swapping the glyph for an SVG icon changes that textContent —
  update the pinned string (drop the glyph + its trailing space) and the component
  comment in the same commit, as a deliberate presentation-contract change. All
  other testids/text pins stay untouched.

## 4. Decisions rail

- Eyebrow: `history` icon before "Past decisions" (`.ds-label` h2 → wrap in a flex
  span; text content unchanged).
- Per-row leading icon in `.row-summary`, from a new fail-safe mapper in
  `lib/rail.ts` (or local): action contains `rollback` → `rotate-ccw`; `iac` →
  `git-merge`; `upgrade`/`pr` → `git-pull-request`; `issue`/`drift`/`report` →
  `alert-triangle`; else `file-text`. SVG contributes no textContent, so the
  existing `.row-meta`/`.row-subtitle`/CTA text pins in DecisionsRail.test.ts
  (404 lines) stay green.
- Hover lift on rows: existing hover shadow + `transform: translateY(-1px)` with a
  transform transition (reduced-motion neutralizes globally).

## 5. Header, sections, chrome

- **Header**: `radar` logo mark in a small rounded square (`--ds-stream-surface` bg,
  `--ds-stream-ink` stroke) before the title; `box-shadow: var(--ds-shadow-sm)`
  under the header bar. Title/tagline text unchanged.
- **Tour button**: `compass` icon. **TokenStatus**: `key-round` inside the pill.
- **InfraDiagram / CapabilityCard summaries**: icon before the existing `.ds-label`
  title; the `▸` CSS caret stays (it's the open/close affordance).
- **Timeline groups**: icon before each `group__title` (brain/wrench/cable) — passed
  through `Group.svelte` as an optional prop so Group stays generic.
- **ChatForm**: `send` icon in the submit button (label "Send" stays — testid pin).
- **TraceBadge**: `copy` icon in the trace pill before the id.
- **FinalResponse / CTAs / banners**: no icon (they have their own affordances:
  green rail, amber dots). Restraint is part of the direction.

## 6. Eyebrow tint (the "gentle depth" of headers)

Component-scoped (NOT base.css `.ds-label` itself): the three big section eyebrows
(rail, infra, capability) shift `color: var(--ds-muted)` → `var(--ds-fg-soft)` with
the icon at `--ds-muted`. Subtle presence bump, no chips, no color blocking.

## 7. Tests

- New `Icon.test.ts`: renders svg, aria-hidden, size prop; registry safety drift-pin
  via the allowlist parser from §1 (allowed tags/attrs only + negative asserts; set
  of names is exactly the documented mapping).
- AutonomyControl: armed-state class test; confirm row still appears/disappears;
  pill element aria-hidden; **existing suite green unchanged**.
- Rail icon mapper unit tests (fail-safe default included).
- Full run: `cd frontend && npx vitest run` — 543 existing + new, zero regressions.
- Traps for the implementer: jsdom lacks `ResizeObserver` (guard, do not polyfill in
  prod code); Svelte transitions in jsdom need `await tick()`/fake timers — copy the
  Timeline test patterns; `vi.useFakeTimers` interactions with transition timers.

## 8. Ship loop

Branch → PR → CI on exact head SHA → Codex completed-work review (advisory) → merge
→ coordinator rebake (`infra/cloudbuild.coordinator-update.yaml`, `_TAG=<short sha>`)
→ **traffic pin** `update-traffic --to-revisions=<new>=100` → live probe (new
`transparency-*.js` bundle hash, dial renders, /healthz) → memory rev pointer.

Rollback: previous revision stays the pinned-traffic rollback target (currently
00079-6jb @:ef98229).

## Non-goals

- No SvelteKit / separate frontend deploy (explicitly deferred post-hackathon).
- No copy changes to mode blurbs or pinned CTA strings.
- No dark mode, no layout/grid changes, no new npm dependencies.
