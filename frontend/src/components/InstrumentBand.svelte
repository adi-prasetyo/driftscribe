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
   * Every stat is a `<button>` that calls `onNavigate('estate')` — including
   * "awaiting your approval", which is a known oddity (approvals don't live
   * on the estate view) the plan keeps as specified; Task 3.5 may revisit
   * the routing. Each button's accessible name is a dedicated *Aria catalog
   * key (not the concatenated visible text) so a screen reader always pairs
   * the figure with what it counts — a bare "7" read aloud is meaningless.
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
  import type { AppView } from '../lib/deeplink';

  let {
    managed,
    drift,
    awaiting,
    onNavigate,
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
    onNavigate: (view: AppView) => void;
  } = $props();

  function goEstate(): void {
    onNavigate('estate');
  }

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
  function statAria(
    n: number | null,
    key: CatalogKey,
    unknownKey: CatalogKey,
    tf: TranslateFn,
  ): string {
    return n === null ? tf(unknownKey) : tf(key, { n });
  }

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
    <button
      type="button"
      class="instrument-band__stat"
      data-testid="instrument-band-managed"
      aria-label={statAria(managed, 'desk.band.managedAria', 'desk.band.managedUnknownAria', $t)}
      data-unknown={managed === null ? 'true' : null}
      onclick={goEstate}
    >
      <span class="instrument-band__num">{statText(managed)}</span>
      <span class="instrument-band__label">{$t('desk.band.managedLabel')}</span>
    </button>
    <button
      type="button"
      class="instrument-band__stat instrument-band__stat--drift"
      data-testid="instrument-band-drift"
      aria-label={statAria(drift, 'desk.band.driftAria', 'desk.band.driftUnknownAria', $t)}
      data-unknown={drift === null ? 'true' : null}
      onclick={goEstate}
    >
      <span class="instrument-band__num">{statText(drift)}</span>
      <span class="instrument-band__label">{$t('desk.band.driftLabel')}</span>
    </button>
    <button
      type="button"
      class="instrument-band__stat instrument-band__stat--wait"
      data-testid="instrument-band-awaiting"
      aria-label={statAria(awaiting, 'desk.band.awaitingAria', 'desk.band.awaitingUnknownAria', $t)}
      data-unknown={awaiting === null ? 'true' : null}
      onclick={goEstate}
    >
      <span class="instrument-band__num">{statText(awaiting)}</span>
      <span class="instrument-band__label">{$t('desk.band.awaitingLabel')}</span>
    </button>
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
  .instrument-band__stat:hover {
    opacity: 0.75;
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
