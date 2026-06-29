<script lang="ts">
  /**
   * CrewGlyph — a small looping "one estate, four verbs" animation, one per
   * crew agent. Shown in the CapabilityCard summary row (all four always
   * looping) AND in the chat-composer CrewPicker, where only the SELECTED
   * card loops — the `animated` prop gates motion off for the rest.
   *
   * Design: docs/plans/2026-06-16-workload-rename-design.md §5 (glyph) +
   * docs/plans/2026-06-17-crew-picker-cards-design.md (selection-driven motion).
   *
   * Every glyph shares the same service-node (a rounded square in
   * `currentColor`); each agent performs its one verb against it. Autonomy is
   * NEVER encoded in the motion — it lives on the badge/grouping.
   *
   * Color is per-crew IDENTITY: each `crew-glyph--{verb}` rule sets the glyph's
   * `color` (which the node + currentColor-filled dots read) AND `--crew-accent`
   * (the verb accents) to the same crew token — `--ds-crew-{verb}` — so the
   * whole glyph is monochromatic in that crew's hue (Anchor=blue, Patch=brick
   * red, Provision=violet, Explore=teal). The unknown verb has no per-crew rule,
   * so it falls back to inherited ink + the `--ds-stream` accent and is never
   * misrepresented as belonging to a real crew.
   *
   * Tech (locked): inline SVG + CSS @keyframes only — no Lottie/GIF, no JS rAF.
   * Animations use transforms/opacity. The un-animated (base) CSS state of
   * every element IS the healthy/resolved frame, so the global
   * `prefers-reduced-motion` reset in base.css (which neutralises animation)
   * leaves each glyph on a meaningful static picture. A local reduced-motion
   * `animation: none` is added belt-and-suspenders, matching the repo pattern
   * (ReplyPending / TraceBadge).
   *
   * `verb` is the frozen symbolic workload value (drift / upgrade / provision /
   * explore — i.e. CapWorkload.name), NOT the display name. Unknown values fall
   * back to a static node.
   *
   * `animated` (default true) gates the loop: when false the `crew-glyph--
   * animated` class is absent and a specificity-winning `:not()` rule sets
   * `animation: none` on every element, so the glyph rests on its base =
   * healthy frame (identical to the reduced-motion picture).
   */
  let {
    verb,
    size = 24,
    animated = true,
  }: { verb: string; size?: number; animated?: boolean } = $props();

  const KNOWN = new Set(['drift', 'upgrade', 'provision', 'explore']);
  const v = $derived(KNOWN.has(verb) ? verb : 'unknown');
</script>

<svg
  viewBox="0 0 64 64"
  width={size}
  height={size}
  fill="none"
  stroke="currentColor"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
  focusable="false"
  class="crew-glyph crew-glyph--{v}"
  class:crew-glyph--animated={animated}
  data-testid="crew-glyph-{v}"
>
  {#if v === 'drift'}
    <!-- Anchor: the live node drifts off its contract, snaps back, settles.
         The dashed accent outline is the contract "home" it realigns to. -->
    <rect class="anchor-home is-accent" x="22" y="22" width="20" height="20" rx="5" />
    <rect class="anchor-node" x="22" y="22" width="20" height="20" rx="5" />
  {:else if v === 'upgrade'}
    <!-- Patch: a crack opens on the node, a patch covers it (shape change), the
         node heals, and the "up to date" tick pops back. -->
    <rect class="patch-node" x="22" y="22" width="20" height="20" rx="5" />
    <polyline class="patch-crack" points="30,23 27,30 31,33 28,41" />
    <rect class="patch-bandage is-accent" x="27" y="29" width="10" height="6" rx="3" />
    <polyline class="patch-check is-accent" points="44,24 47,27 52,20" />
  {:else if v === 'provision'}
    <!-- Provision: blocks assemble into a solid node inside a dashed slot, then
         a branch line draws out to a commit (the PR). -->
    <rect class="prov-slot is-faint" x="22" y="22" width="20" height="20" rx="5" />
    <rect class="prov-block prov-block--a" x="22" y="22" width="20" height="9" rx="3" />
    <rect class="prov-block prov-block--b" x="22" y="32" width="9" height="10" rx="3" />
    <rect class="prov-block prov-block--c" x="33" y="32" width="9" height="10" rx="3" />
    <line class="prov-branch is-accent" x1="42" y1="34" x2="51" y2="34" />
    <circle class="prov-commit" cx="53" cy="34" r="2.6" />
  {:else if v === 'explore'}
    <!-- Explore: a scan band sweeps the node left-to-right; detail dots light up
         as it passes. Read-only — nothing is mutated. -->
    <rect class="scan-node" x="22" y="22" width="20" height="20" rx="5" />
    <circle class="scan-dot scan-dot--1" cx="27" cy="32" r="1.7" />
    <circle class="scan-dot scan-dot--2" cx="32" cy="32" r="1.7" />
    <circle class="scan-dot scan-dot--3" cx="37" cy="32" r="1.7" />
    <line class="scan-band is-accent" x1="20" y1="20" x2="20" y2="44" />
  {:else}
    <!-- Unknown verb: a genuinely static node (no animation) so a future
         workload we don't recognise is never visually misrepresented as Anchor
         drifting. -->
    <rect class="static-node" x="22" y="22" width="20" height="20" rx="5" />
  {/if}
</svg>

<style>
  /* Base/fallback: the unknown verb inherits ink and the stream-blue accent.
     Known crews override both below. ~3s ambient loop with a long rest tail so a
     panel of four reads calm, not busy. */
  .crew-glyph {
    --crew-accent: var(--ds-stream);
    --glyph-dur: 3000ms;
    flex-shrink: 0;
    display: block;
  }

  /* Per-crew identity color. Setting `color` recolors the square (its stroke is
     `currentColor`) and the currentColor-filled detail dots; `--crew-accent`
     recolors the verb accents to the same hue — so each glyph is monochromatic
     in its crew's color. A direct `color` here wins over any `color` inherited
     from the host (e.g. the CrewPicker card), so the glyph stays its crew hue
     regardless of selected/unselected card text color. */
  .crew-glyph--drift {
    color: var(--ds-crew-drift);
    --crew-accent: var(--ds-crew-drift);
  }
  .crew-glyph--upgrade {
    color: var(--ds-crew-upgrade);
    --crew-accent: var(--ds-crew-upgrade);
  }
  .crew-glyph--provision {
    color: var(--ds-crew-provision);
    --crew-accent: var(--ds-crew-provision);
  }
  .crew-glyph--explore {
    color: var(--ds-crew-explore);
    --crew-accent: var(--ds-crew-explore);
  }

  .crew-glyph :is(rect, line, polyline) {
    stroke-width: 4;
  }
  .is-accent {
    stroke: var(--crew-accent);
  }
  .is-faint {
    opacity: 0.32;
  }

  /* Selection-driven motion (CrewPicker): when the host omits
     `crew-glyph--animated`, gate every loop off. This override carries more
     class-specificity than the bare `.anchor-node { animation }` rules below,
     so each element falls back to its base = healthy frame — the same static
     picture prefers-reduced-motion lands on. `.prov-block` / `.scan-dot` match
     the base class, covering their --a/--b/--c / --2/--3 variants too. */
  .crew-glyph:not(.crew-glyph--animated)
    :is(
      .anchor-node,
      .patch-crack,
      .patch-bandage,
      .patch-check,
      .prov-block,
      .prov-branch,
      .prov-commit,
      .scan-dot,
      .scan-band
    ) {
    animation: none;
  }

  /* Each animated element scales/rotates about its own box centre. */
  .anchor-node,
  .patch-crack,
  .patch-bandage,
  .patch-check,
  .prov-block,
  .prov-branch,
  .prov-commit,
  .scan-dot,
  .scan-band {
    transform-box: fill-box;
    transform-origin: center;
  }

  /* ---- Anchor (drift -> hold) ------------------------------------------- */
  .anchor-home {
    stroke-dasharray: 3 4;
    opacity: 0.4;
  }
  .anchor-node {
    animation: crew-glyph-anchor var(--glyph-dur) var(--ds-ease) infinite;
  }
  @keyframes crew-glyph-anchor {
    0%,
    12% {
      transform: translate(0, 0);
    }
    34% {
      transform: translate(7px, -6px);
    }
    46% {
      transform: translate(7px, -6px);
    }
    60% {
      transform: translate(-1.4px, 1.2px);
      animation-timing-function: cubic-bezier(0.2, 0.7, 0.2, 1);
    }
    70% {
      transform: translate(0.7px, -0.5px);
    }
    78%,
    100% {
      transform: translate(0, 0);
    }
  }

  /* ---- Patch (heal + tick) ---------------------------------------------- */
  .patch-crack {
    opacity: 0;
    stroke-width: 3;
    animation: crew-glyph-patch-crack var(--glyph-dur) var(--ds-ease) infinite;
  }
  .patch-bandage {
    opacity: 0;
    transform: scale(0);
    animation: crew-glyph-patch-bandage var(--glyph-dur) var(--ds-ease) infinite;
  }
  .patch-check {
    animation: crew-glyph-patch-check var(--glyph-dur) var(--ds-ease) infinite;
  }
  @keyframes crew-glyph-patch-crack {
    0%,
    14% {
      opacity: 0;
    }
    22%,
    44% {
      opacity: 1;
    }
    56%,
    100% {
      opacity: 0;
    }
  }
  @keyframes crew-glyph-patch-bandage {
    0%,
    40% {
      opacity: 0;
      transform: scale(0);
    }
    52% {
      opacity: 1;
      transform: scale(1.08);
    }
    60%,
    74% {
      opacity: 1;
      transform: scale(1);
    }
    84%,
    100% {
      opacity: 0;
      transform: scale(1);
    }
  }
  @keyframes crew-glyph-patch-check {
    0%,
    16% {
      opacity: 1;
      transform: scale(1);
    }
    24%,
    64% {
      opacity: 0;
      transform: scale(0.6);
    }
    74% {
      opacity: 1;
      transform: scale(1.18);
    }
    82%,
    100% {
      opacity: 1;
      transform: scale(1);
    }
  }

  /* ---- Provision (assemble + PR) ---------------------------------------- */
  .prov-slot {
    stroke-dasharray: 3 4;
  }
  .prov-block--a {
    animation: crew-glyph-prov-a var(--glyph-dur) var(--ds-ease) infinite;
  }
  .prov-block--b {
    animation: crew-glyph-prov-b var(--glyph-dur) var(--ds-ease) infinite;
    animation-delay: 90ms;
  }
  .prov-block--c {
    animation: crew-glyph-prov-c var(--glyph-dur) var(--ds-ease) infinite;
    animation-delay: 180ms;
  }
  .prov-branch {
    transform-origin: left center;
    animation: crew-glyph-prov-branch var(--glyph-dur) var(--ds-ease) infinite;
  }
  .prov-commit {
    /* A solid accent dot reads as a commit; also sidesteps SVG's default
       stroke-width:1 (the shared stroke-width rule covers rect/line/polyline). */
    fill: var(--crew-accent);
    stroke: none;
    animation: crew-glyph-prov-commit var(--glyph-dur) var(--ds-ease) infinite;
  }
  @keyframes crew-glyph-prov-a {
    0%,
    10% {
      transform: translate(0, 0);
      opacity: 1;
    }
    24%,
    30% {
      transform: translate(0, -13px);
      opacity: 0;
    }
    50%,
    100% {
      transform: translate(0, 0);
      opacity: 1;
    }
  }
  @keyframes crew-glyph-prov-b {
    0%,
    10% {
      transform: translate(0, 0);
      opacity: 1;
    }
    24%,
    30% {
      transform: translate(-13px, 5px);
      opacity: 0;
    }
    54%,
    100% {
      transform: translate(0, 0);
      opacity: 1;
    }
  }
  @keyframes crew-glyph-prov-c {
    0%,
    10% {
      transform: translate(0, 0);
      opacity: 1;
    }
    24%,
    30% {
      transform: translate(13px, 5px);
      opacity: 0;
    }
    58%,
    100% {
      transform: translate(0, 0);
      opacity: 1;
    }
  }
  @keyframes crew-glyph-prov-branch {
    0%,
    58% {
      transform: scaleX(0);
    }
    72%,
    100% {
      transform: scaleX(1);
    }
  }
  @keyframes crew-glyph-prov-commit {
    0%,
    66% {
      transform: scale(0);
      opacity: 0;
    }
    78% {
      transform: scale(1.2);
      opacity: 1;
    }
    86%,
    100% {
      transform: scale(1);
      opacity: 1;
    }
  }

  /* ---- Explore (scan) --------------------------------------------------- */
  .scan-dot {
    fill: currentColor;
    stroke: none;
    opacity: 0.55;
    animation: crew-glyph-scan-dot var(--glyph-dur) var(--ds-ease) infinite;
  }
  .scan-dot--2 {
    animation-delay: 360ms;
  }
  .scan-dot--3 {
    animation-delay: 720ms;
  }
  .scan-band {
    opacity: 0;
    stroke-width: 3;
    animation: crew-glyph-scan-band var(--glyph-dur) var(--ds-ease-out) infinite;
  }
  @keyframes crew-glyph-scan-dot {
    0%,
    100% {
      opacity: 0.55;
      transform: scale(1);
    }
    18% {
      opacity: 1;
      transform: scale(1.55);
    }
    34% {
      opacity: 0.55;
      transform: scale(1);
    }
  }
  @keyframes crew-glyph-scan-band {
    0% {
      transform: translateX(0);
      opacity: 0;
    }
    12% {
      opacity: 0.85;
    }
    50% {
      transform: translateX(24px);
      opacity: 0.85;
    }
    62% {
      transform: translateX(24px);
      opacity: 0;
    }
    100% {
      transform: translateX(0);
      opacity: 0;
    }
  }

  /* Belt-and-suspenders alongside the global base.css reduced-motion reset:
     with animation removed, every element rests on its base (healthy) frame. */
  @media (prefers-reduced-motion: reduce) {
    .anchor-node,
    .patch-crack,
    .patch-bandage,
    .patch-check,
    .prov-block,
    .prov-branch,
    .prov-commit,
    .scan-dot,
    .scan-band {
      animation: none;
    }
  }
</style>
