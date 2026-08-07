<script lang="ts">
  /**
   * InstrumentBand — the three big numbers across the top of the desk
   * (docs/plans/2026-07-28-composite-mockup.html "instrument band"): managed
   * / drift / awaiting, plus a thin proportional meter underneath. This is
   * the demo's visible pulse — drift lands and the drift numeral ticks
   * 6→7 while awaiting goes 0→1, the operator approves, the numbers fall
   * back — so it must read like an instrument panel, not a dashboard widget.
   *
   * Deliberately dumb: this component computes NOTHING. `managed`/`drift`/
   * `awaiting` are plain numbers the consumer derives from the overview
   * store via `scopeTotals()` (lib/infra_graph.ts) and passes straight in.
   * `scopeTotals`'s `managed + drift === resources` invariant is load-
   * bearing and documented at length there; a second derivation here would
   * just be a second place for the two to quietly disagree. See Task 3.5
   * (App.svelte wiring) for the actual computation.
   *
   * ds-7ag.2 settled the routing this used to flag as an open oddity. Every
   * stat used to be a `<button>` calling `onNavigate('estate')`, including
   * "awaiting your approval" — whose content, the approval queue, is on the
   * DESK. Clicking the number that says "you have work" walked away from the
   * work, and that is the wayfinding failure the judges hit.
   *
   * So the band no longer decides destinations at all: it emits `onStat(stat)`
   * and the CONSUMER routes it. Which stats are interactive at all, and how
   * their accessible names read, is `STATS` below — one flat table, because
   * the desk+estate merge (2026-07-31) left exactly one page to render on:
   *
   *   managed   → the estate section further down this page
   *   drift     → the same
   *   awaiting  inert figure
   *
   * That table used to have a second column for the standalone estate view,
   * where managed/drift were inert and awaiting led back to the desk. With the
   * estate merged in as a section, neither of those situations exists: you are
   * always on the page that holds all three subjects, so the only question left
   * is whether a stat's subject is on screen or below the fold.
   *
   * ds-s61 is why awaiting is the one that isn't a control. ds-7ag.2 had
   * pointed it at this page's own pending card via scrollIntoView, but that
   * card sits ~270px below the numeral on the same screen — already fully
   * visible. The "jump" had nowhere to go, so all it did was spend whatever
   * scroll happened to exist (38px of it, from a stale viewport calc) and stop,
   * which read as an unexplained twitch. A number whose subject is directly
   * beneath it does not need to be a control.
   *
   * An inert stat renders as a `<span>`, never a disabled `<button>`: a
   * disabled button drops out of keyboard navigation and helps nobody.
   *
   * Each stat's accessible name is a dedicated *Aria catalog key (not the
   * concatenated visible text) so a screen reader always pairs the figure with
   * what it counts — a bare "7" read aloud is meaningless. An INTERACTIVE stat
   * also names its destination there, because the aria-label overrides all
   * descendant text: the visible hover hint below is invisible to a screen
   * reader, so the label has to carry the same promise. The inert figure keeps
   * the plain wording — it promises nothing. Both live in the same `STATS`
   * entry as the `interactive` flag itself, so a stat cannot become a control
   * while keeping a name that names nowhere (that WAS the ds-7ag.2 defect).
   *
   * The meter is two flex segments sized directly off `managed`/`drift`
   * (mockup: `.meter i { flex: <managed> }` / `.meter u { flex: <drift> }`).
   * Proportional flex is scale-free, so it stays correct at any magnitude —
   * no percentage math needed. The one degenerate case is managed=drift=0
   * (a fresh or degraded estate with nothing indexed yet): both segments
   * get flex 0, which — with no content and flex-basis 0% — collapse to
   * zero width, so neither color paints and the track's own background
   * shows through as a bare line. That reads as "no data" rather than a
   * misleading full or half-filled bar, so it needs no special-casing;
   * `Math.max(0, …)` below only guards against a negative/non-finite input
   * ever reaching the CSS `flex` shorthand, which a negative number breaks.
   */
  import { untrack } from 'svelte';
  import { t, type TranslateFn } from '../lib/i18n';

  /** Which numeral was activated. The consumer owns what that means. */
  export type BandStat = 'managed' | 'drift' | 'awaiting';

  let {
    managed,
    drift,
    awaiting,
    onStat,
  }: {
    /** `null` = NOT YET KNOWN, and it renders as a placeholder rather than a
     *  numeral (ds-eh6). Before the first refresh cycle settles the store holds
     *  `graph: null` and an empty approvals list, which used to arrive here as
     *  three zeros — "0 managed, 0 drift, 0 awaiting" is a confident claim
     *  about an estate nothing has looked at yet, and it is the same over-claim
     *  the desk hero made with its all-clear. Zero remains a perfectly good
     *  ANSWER; it just has to be one we actually got. */
    managed: number | null;
    drift: number | null;
    awaiting: number | null;
    onStat: (stat: BandStat) => void;
  } = $props();

  /** The visible stand-in for an unknown figure. An em dash is the typographic
   *  convention for "no value" in a numeric column and needs no translation;
   *  the accessible name still gets real words (see `statAria`). */
  const UNKNOWN_GLYPH = '—';

  function statText(n: number | null): string {
    return n === null ? UNKNOWN_GLYPH : String(n);
  }

  /** Screen readers must never hear the em dash — it reads as nothing, or as
   *  punctuation. An unknown stat announces its label with an explicit
   *  "not yet known" instead of a bare figure. */
  // Keys are typed against the catalog (TranslateFn's own key union), not
  // plain strings — a widened `string` here would silently accept a typo'd or
  // deleted key and only surface it as a missing translation at runtime.
  type CatalogKey = Parameters<TranslateFn>[0];

  type StatSpec = {
    key: BandStat;
    /** The small caption under the numeral. */
    label: CatalogKey;
    /** Whether this stat is a control. Everything a control has to promise —
     *  the destination clause in its `aria` and its visible `hint` — sits in
     *  this same entry, so the flag and the promise cannot drift apart. */
    interactive: boolean;
    /** The accessible name in both ds-eh6 states. `known` is interpolated with
     *  `{n}`; `unknown` carries the state in words, because the visible em dash
     *  a screen reader would otherwise meet announces as nothing at all. Both
     *  are required — the compiler will not let a stat exist without a name. */
    aria: { known: CatalogKey; unknown: CatalogKey };
    /** The VISIBLE hover/focus hint. Sugar only, and only for a control: the
     *  aria-label overrides this text entirely, which is why the destination
     *  clause is duplicated into `aria` above rather than read from here. */
    hint?: CatalogKey;
    extraClass: string;
  };

  // The whole routing table (see the header comment). Vestigial naming: the
  // `*AriaDesk` keys were named for the context that selected them back when
  // there were two. There is one now, so the suffix means nothing — renaming
  // the catalog keys would touch every locale for no operator-visible gain.
  const STATS: readonly StatSpec[] = [
    {
      key: 'managed',
      label: 'desk.band.managedLabel',
      interactive: true,
      aria: {
        known: 'desk.band.managedAriaDesk',
        unknown: 'desk.band.managedUnknownAriaDesk',
      },
      hint: 'desk.band.statHintEstate',
      extraClass: '',
    },
    {
      key: 'drift',
      label: 'desk.band.driftLabel',
      interactive: true,
      aria: {
        known: 'desk.band.driftAriaDesk',
        unknown: 'desk.band.driftUnknownAriaDesk',
      },
      hint: 'desk.band.statHintEstate',
      extraClass: 'instrument-band__stat--drift',
    },
    {
      // Inert regardless of value (ds-s61): the queue it counts is directly
      // below it on this same page. No `hint`, and the plain wording — a
      // figure promises nothing.
      key: 'awaiting',
      label: 'desk.band.awaitingLabel',
      interactive: false,
      aria: {
        known: 'desk.band.awaitingAria',
        unknown: 'desk.band.awaitingUnknownAria',
      },
      extraClass: 'instrument-band__stat--wait',
    },
  ];

  function statAria(spec: StatSpec, n: number | null, tf: TranslateFn): string {
    return n === null ? tf(spec.aria.unknown) : tf(spec.aria.known, { n });
  }

  const value = $derived<Record<BandStat, number | null>>({ managed, drift, awaiting });
  const stats = $derived(STATS.map((spec) => ({ spec, value: value[spec.key] })));

  // ── The numeral tick (ds-wd2.13) ─────────────────────────────────────────
  // Mockup `@keyframes pop` (1.14 → 1 scale, .3s): a numeral pops when its
  // value CHANGES. The mockup produced that by swapping which of three
  // pre-rendered <i> elements was displayed per `data-state`; that machinery
  // exists so one static page can fake three states, and porting it would be
  // mockup-artifact-as-architecture. A live component animates on value change
  // instead, which is a different mechanism entirely.
  //
  // `popN` counts qualifying changes per stat and KEYS the numeral's {#key}
  // block, so each change builds a fresh <span>. A brand-new element with the
  // animation class restarts the animation by construction — no
  // remove/force-reflow/re-add dance, and no Web Animations API (which
  // `motion.ts` would have to gate in JS, and which every unit test would see
  // as disabled, since tests/unit/setup.ts mocks matchMedia to report
  // prefers-reduced-motion: reduce).
  //
  // The guard is `prev !== null && next !== null && prev !== next`. `null` is
  // NOT YET KNOWN (see the props doc above), so:
  //   · null → 9  is the first reading landing, i.e. every page load. Popping
  //     there is noise, not news — which is the failure mode this bead's design
  //     note names explicitly.
  //   · 7 → null  would pop an em dash. It doesn't.
  //   · 6 → 6     is what most of the 45s poll cycles deliver. Only a real
  //     change is news.
  // The accepted consequence: a change SPANNING a degraded cycle does not pop.
  // 6 → null → 7 is reachable — a soft-failed /infra/graph is a well-formed 200
  // carrying degraded:true, so it replaces the good graph, and ApprovalDesk
  // nulls the band off `graphUsable` (and nulls `awaiting` on any degraded
  // cycle). That missed tick is deliberate: it can only happen while the hero
  // directly below is announcing that the snapshot is incomplete, and a
  // celebratory pop under that headline would be the wrong emphasis.
  //
  // $effect.pre, not $effect: pre runs BEFORE the DOM update in the same flush,
  // so the new count and the new numeral text land together. A post-update
  // effect would need a second flush and start the animation a frame late.
  // untrack, because the effect both reads and writes popN; without it the
  // write re-triggers the effect (it would converge, but only by wasting a pass).
  //
  // Derived from STATS, not a second hand-typed literal: STATS is this file's
  // own source of truth for "what stats exist" (see the header comment). A
  // literal list here would be a second, unenforced copy of BandStat's members
  // — the Record<BandStat, …> literals below DO force a compile error if
  // BandStat grows a key, but a stray array literal doesn't, so a new stat
  // would silently never pop instead of failing to build.
  const BAND_KEYS = STATS.map((s) => s.key);

  let popN = $state<Record<BandStat, number>>({ managed: 0, drift: 0, awaiting: 0 });
  // Seeded to null (NOT the current prop values — reading `managed`/`drift`/
  // `awaiting` directly here would only capture their initial value anyway,
  // which is exactly what trips Svelte's state_referenced_locally warning).
  // Either seed produces the same first pass: the guard's `prev !== null`
  // already skips seen→now on mount, so a null seed reaches that same "no pop"
  // outcome without reading a prop outside a derived/effect. Deliberately NOT
  // $state — nothing renders it.
  let seen: Record<BandStat, number | null> = { managed: null, drift: null, awaiting: null };

  $effect.pre(() => {
    const next = value; // registers the dependency on all three props
    untrack(() => {
      for (const k of BAND_KEYS) {
        const prev = seen[k];
        const now = next[k];
        if (prev !== null && now !== null && prev !== now) popN[k] += 1;
      }
      seen = next;
    });
  });

  // Defensive clamp for the meter only: scopeTotals() guarantees non-negative
  // finite sums, but this component doesn't re-derive or trust that upstream
  // invariant blindly — a negative flex value is invalid CSS and a non-finite
  // one renders nothing at all, either of which would silently blank the
  // meter rather than showing the honest bare-track "no data" state above.
  //
  // `null` (not yet known) lands on 0 through the same path a non-finite value
  // does, which is already the right answer: both segments collapse and the
  // bare track shows through as "no data". That is precisely what an unlooked-at
  // estate should render, so the unknown case needs no branch of its own here.
  function meterFlex(n: number | null): number {
    return n !== null && Number.isFinite(n) ? Math.max(0, n) : 0;
  }
  const meterManaged = $derived(meterFlex(managed));
  const meterDrift = $derived(meterFlex(drift));
</script>

<div class="instrument-band" data-testid="instrument-band">
  <div class="instrument-band__stats">
    <!-- `<button>` when the stat leads somewhere, `<span role="img">` when it is
         a figure. role="img" (not "group") because it reproduces the button's
         own behaviour: the aria-label REPLACES the descendant text, so a screen
         reader hears "9 declared in IaC" rather than the numeral and its label
         read twice — and, for an unknown figure, hears the "not yet known"
         wording instead of an em dash that announces as nothing at all. -->
    {#each stats as { spec, value: n } (spec.key)}
      <svelte:element
        this={spec.interactive ? 'button' : 'span'}
        type={spec.interactive ? 'button' : undefined}
        role={spec.interactive ? undefined : 'img'}
        class="instrument-band__stat {spec.extraClass}"
        class:instrument-band__stat--static={!spec.interactive}
        data-testid="instrument-band-{spec.key}"
        aria-label={statAria(spec, n, $t)}
        data-unknown={n === null ? 'true' : null}
        onclick={spec.interactive ? () => onStat(spec.key) : undefined}
      >
        <!-- {#key} rebuilds this span on every qualifying change, which is what
             restarts the pop animation; `data-pop` is the same counter, exposed
             so tests and the visual rig can assert the tick happened without
             depending on layout or timing. The class is absent until the first
             pop, so a freshly mounted band is still. -->
        {#key popN[spec.key]}
          <span
            class="instrument-band__num"
            class:instrument-band__num--pop={popN[spec.key] > 0}
            data-pop={popN[spec.key]}
          >{statText(n)}</span>
        {/key}
        <!-- The label and its hint share one positioned box so the hint lands
             exactly on the label rather than at the stat's padding edge (an
             abspos child is placed against the PADDING box, and stats 2-3 carry
             a 28px padding-left for their divider rule). The wrapper renders
             unconditionally, so an inert figure keeps identical layout. -->
        <span class="instrument-band__meta" class:instrument-band__meta--hinted={spec.hint}>
          <span class="instrument-band__label">{$t(spec.label)}</span>
          {#if spec.hint}
            <!-- Fades in over the label on hover/focus, so the resting band
                 stays the mockup's calm three numerals while still saying what
                 a click does before you make it. aria-hidden because the
                 aria-label already carries the destination — announcing it
                 twice would be worse than not showing it at all. -->
            <span class="instrument-band__hint" aria-hidden="true">{$t(spec.hint)}</span>
          {/if}
        </span>
      </svelte:element>
    {/each}
  </div>
  <div class="instrument-band__meter" aria-hidden="true">
    <span
      class="instrument-band__meter-seg instrument-band__meter-seg--managed"
      data-testid="instrument-band-meter-managed"
      style:flex={meterManaged}
    ></span>
    <span
      class="instrument-band__meter-seg instrument-band__meter-seg--drift"
      data-testid="instrument-band-meter-drift"
      style:flex={meterDrift}
    ></span>
  </div>
</div>

<style>
  .instrument-band__stats {
    display: flex;
    gap: 0;
    padding: 26px 40px 22px;
    border-bottom: 1px solid var(--ds-border);
    margin-top: 14px;
  }

  /* Buttons, not divs (task spec) — reset native chrome back to the mockup's
     plain flex column, matching the .pause-pill__btn reset convention. */
  .instrument-band__stat {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    appearance: none;
    background: none;
    border: none;
    padding: 0;
    margin: 0;
    font-family: inherit;
    text-align: left;
    cursor: pointer;
  }
  /* There is deliberately NO hover fade on the numeral (ds-16e). It used to
     drop to opacity:.75, which is not a lighter ink but the numeral MIXED with
     the paper behind it.
     Only three numerals could ever fade — the selector excluded --static, and
     `awaiting` is interactive:false, so despite what ds-16e's title says the
     AWAITING numeral never faded at all. Of the three that did, managed (navy,
     7.05) and drift (warn, 3.23) held the 3:1 large-text floor. The one that
     did not is the [data-unknown] placeholder: it takes --ds-faint, rests at
     only 3.08:1, and fell to 2.23:1 — and it is reachable on BOTH interactive
     stats, since ApprovalDesk passes null for managed and drift whenever the
     graph is unavailable.
     Raising the fade does not fix it. The often-suggested .91 still leaves the
     placeholder at 2.73:1; it needs alpha >= ~.981, which is not a fade anyone
     can see. Removing it is the only honest option.
     The affordance is unaffected: the hover hint under the label (ds-7ag.2) is
     a separate element with its own fade, which is why the fade was scoped to
     the numeral in the first place. */
  /* An inert figure must not pretend to be a control (ds-7ag.2). It keeps the
     stat's layout and type, and loses only the affordances. */
  .instrument-band__stat--static {
    cursor: default;
  }

  .instrument-band__stat + .instrument-band__stat {
    border-left: 1px solid var(--ds-border);
    padding-left: 28px;
  }

  .instrument-band__num {
    font-size: 44px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: -0.03em;
    /* Fixed-width digits so a ticking numeral (the demo's 6→7 beat) never
       shifts its own width or neighboring layout. */
    font-variant-numeric: tabular-nums;
    color: var(--ds-navy);
    transition: opacity var(--ds-dur-fast) var(--ds-ease);
  }
  .instrument-band__stat--drift .instrument-band__num {
    color: var(--ds-warn);
  }
  /* Unknown reads as muted regardless of which stat it is — the amber "drift"
     and navy "managed" inks are semantic (they mean "this many are drifting"),
     so painting a placeholder in them would carry the very meaning the
     placeholder exists to withhold. Wins over the two per-stat rules below by
     being an attribute-qualified selector on the same element. Placed after
     --drift and before --wait would leave --wait winning on source order, so
     the `[data-unknown]` qualifier is what makes this order-independent. */
  .instrument-band__stat[data-unknown] .instrument-band__num {
    color: var(--ds-faint);
  }
  .instrument-band__stat--wait .instrument-band__num {
    color: var(--ds-stream);
  }

  /* Positioned box shared by the label and its hover hint (see the markup). */
  .instrument-band__meta {
    position: relative;
    display: block;
    margin-top: 7px;
  }

  .instrument-band__label {
    display: block;
    font-size: 11.5px;
    color: var(--ds-faint);
    letter-spacing: 0.04em;
    transition: opacity var(--ds-dur-fast) var(--ds-ease);
  }

  /* The visible destination affordance (ds-7ag.2): the numerals looked like
     figures, so nothing said a click would go anywhere. Hidden at rest —
     inset over the label, revealed on hover/focus-visible, and never taking
     pointer events of its own. `nowrap` because it may run slightly wider than
     the label it covers; the band has room and nothing clips it. */
  .instrument-band__hint {
    position: absolute;
    inset: 0;
    font-size: 11.5px;
    letter-spacing: 0.04em;
    color: var(--ds-fg-soft);
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--ds-dur-fast) var(--ds-ease);
  }
  /* Gated on --hinted, which the markup sets only when a hint actually rendered
     — so hovering an inert figure never blanks its label. */
  .instrument-band__stat:hover .instrument-band__meta--hinted .instrument-band__label,
  .instrument-band__stat:focus-visible .instrument-band__meta--hinted .instrument-band__label {
    opacity: 0;
  }
  .instrument-band__stat:hover .instrument-band__hint,
  .instrument-band__stat:focus-visible .instrument-band__hint {
    opacity: 1;
  }

  .instrument-band__meter {
    height: 3px;
    background: var(--ds-border);
    margin: 0 40px;
    display: flex;
  }
  .instrument-band__meter-seg {
    display: block;
    height: 100%;
  }
  .instrument-band__meter-seg--managed {
    background: var(--ds-navy);
  }
  .instrument-band__meter-seg--drift {
    background: var(--ds-warn);
    transition: flex 0.4s ease;
  }
</style>
