<script lang="ts">
  /**
   * InstrumentBand — the three big numbers across the top of the desk/estate
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
   * So the band no longer decides destinations at all: it emits
   * `onStat(stat)` and the CONSUMER routes it. `context` is the part a
   * callback cannot express — which stats are interactive at all here, and
   * how their accessible names read:
   *
   *   stat      | context 'desk'              | context 'estate'
   *   ----------|-----------------------------|---------------------------
   *   managed   | → estate map                | inert figure
   *   drift     | → estate map                | inert figure
   *   awaiting  | → the desk's pending card   | → the desk
   *
   * with awaiting inert in BOTH when it is 0 or unknown — there is nothing to
   * land on, and a control that goes nowhere is worse than a figure. An inert
   * stat renders as a `<span>`, never a disabled `<button>`: a disabled button
   * drops out of keyboard navigation and helps nobody.
   *
   * Each stat's accessible name is a dedicated *Aria catalog key (not the
   * concatenated visible text) so a screen reader always pairs the figure with
   * what it counts — a bare "7" read aloud is meaningless. An INTERACTIVE stat
   * also names its destination there, because the aria-label overrides all
   * descendant text: the visible hover hint below is invisible to a screen
   * reader, so the label has to carry the same promise. Inert figures and
   * not-yet-known figures keep the plain wording — they promise nothing.
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
  import { t, type TranslateFn } from '../lib/i18n';

  /** Which numeral was activated. The consumer owns what that means. */
  export type BandStat = 'managed' | 'drift' | 'awaiting';
  /** Which view is rendering the band — see the routing table above. */
  export type BandContext = 'desk' | 'estate';

  let {
    managed,
    drift,
    awaiting,
    context,
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
    context: BandContext;
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

  const LABEL: Record<BandStat, CatalogKey> = {
    managed: 'desk.band.managedLabel',
    drift: 'desk.band.driftLabel',
    awaiting: 'desk.band.awaitingLabel',
  };
  /** Plain "{n} <what it counts>" — for an inert figure, which promises nothing. */
  const PLAIN_ARIA: Record<BandStat, CatalogKey> = {
    managed: 'desk.band.managedAria',
    drift: 'desk.band.driftAria',
    awaiting: 'desk.band.awaitingAria',
  };
  /** The same, plus where activating it goes. Keyed by the CONTEXT the band is
   *  rendered in; a missing entry means that stat is never interactive there. */
  const DEST_ARIA: Record<BandStat, Partial<Record<BandContext, CatalogKey>>> = {
    managed: { desk: 'desk.band.managedAriaDesk' },
    drift: { desk: 'desk.band.driftAriaDesk' },
    awaiting: { desk: 'desk.band.awaitingAriaDesk', estate: 'desk.band.awaitingAriaEstate' },
  };
  const UNKNOWN_ARIA: Record<BandStat, CatalogKey> = {
    managed: 'desk.band.managedUnknownAria',
    drift: 'desk.band.driftUnknownAria',
    awaiting: 'desk.band.awaitingUnknownAria',
  };

  function statAria(stat: BandStat, n: number | null, interactive: boolean, tf: TranslateFn): string {
    if (n === null) return tf(UNKNOWN_ARIA[stat]);
    const dest = interactive ? DEST_ARIA[stat][context] : undefined;
    return tf(dest ?? PLAIN_ARIA[stat], { n });
  }

  // Which stats are live, per the routing table in the header comment. awaiting
  // is the one that depends on its own VALUE rather than only on the context:
  // with nothing pending there is no card to land on.
  const stats = $derived([
    {
      key: 'managed' as BandStat,
      value: managed,
      interactive: context === 'desk',
      extraClass: '',
    },
    {
      key: 'drift' as BandStat,
      value: drift,
      interactive: context === 'desk',
      extraClass: 'instrument-band__stat--drift',
    },
    {
      key: 'awaiting' as BandStat,
      value: awaiting,
      interactive: awaiting !== null && awaiting > 0,
      extraClass: 'instrument-band__stat--wait',
    },
  ]);

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
         reader hears "9 managed by IaC" rather than the numeral and its label
         read twice — and, for an unknown figure, hears the "not yet known"
         wording instead of an em dash that announces as nothing at all. -->
    {#each stats as s (s.key)}
      <svelte:element
        this={s.interactive ? 'button' : 'span'}
        type={s.interactive ? 'button' : undefined}
        role={s.interactive ? undefined : 'img'}
        class="instrument-band__stat {s.extraClass}"
        class:instrument-band__stat--static={!s.interactive}
        data-testid="instrument-band-{s.key}"
        aria-label={statAria(s.key, s.value, s.interactive, $t)}
        data-unknown={s.value === null ? 'true' : null}
        onclick={s.interactive ? () => onStat(s.key) : undefined}
      >
        <span class="instrument-band__num">{statText(s.value)}</span>
        <span class="instrument-band__label">{$t(LABEL[s.key])}</span>
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
    border-bottom: 1px solid var(--ds-paper-rule);
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
    transition: opacity var(--ds-dur-fast) var(--ds-ease);
  }
  .instrument-band__stat:not(.instrument-band__stat--static):hover {
    opacity: 0.75;
  }
  /* An inert figure must not pretend to be a control (ds-7ag.2). It keeps the
     stat's layout and type, and loses only the affordances. */
  .instrument-band__stat--static {
    cursor: default;
  }

  .instrument-band__stat + .instrument-band__stat {
    border-left: 1px solid var(--ds-paper-rule);
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
  }
  .instrument-band__stat--drift .instrument-band__num {
    color: var(--ds-drift-amber);
  }
  /* Unknown reads as muted regardless of which stat it is — the amber "drift"
     and navy "managed" inks are semantic (they mean "this many are drifting"),
     so painting a placeholder in them would carry the very meaning the
     placeholder exists to withhold. Wins over the two per-stat rules below by
     being an attribute-qualified selector on the same element. Placed after
     --drift and before --wait would leave --wait winning on source order, so
     the `[data-unknown]` qualifier is what makes this order-independent. */
  .instrument-band__stat[data-unknown] .instrument-band__num {
    color: var(--ds-paper-mut);
  }
  .instrument-band__stat--wait .instrument-band__num {
    color: var(--ds-gblue);
  }

  .instrument-band__label {
    font-size: 11.5px;
    color: var(--ds-paper-mut);
    letter-spacing: 0.04em;
    margin-top: 7px;
  }

  .instrument-band__meter {
    height: 3px;
    background: var(--ds-paper-rule);
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
    background: var(--ds-drift-amber);
    transition: flex 0.4s ease;
  }
</style>
