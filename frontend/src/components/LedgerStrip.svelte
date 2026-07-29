<script lang="ts">
  /**
   * LedgerStrip — the desk's "Recent record" ledger (Task 3.4, mockup
   * ".striph"/".strip"/".srow" — docs/plans/2026-07-28-composite-mockup.html
   * lines ~145-162 for the CSS, ~327-348 for the markup). Reduces the same
   * `decisions` list the rail already holds to a handful of rows via the pure
   * `ledgerRows()` (lib/ledger.ts) — this component only renders what that
   * function already decided; it computes no classification itself (mirrors
   * InstrumentBand's "deliberately dumb" convention).
   *
   * The mockup's quiet 定期点検 (periodic scan, "no drift found") row is
   * DEFERRED, not implemented here — see lib/ledger.ts's header comment for
   * why (scan runs aren't persisted anywhere the SPA can read).
   *
   * The mini SealStamp renders ONLY on `applied` rows, and always with
   * `animate` left at its default `false`: this strip re-renders on every
   * overview refresh, and only the desk hero's single freshly-approved stamp
   * should ever fire the stamp-in animation (SealStamp's own header comment).
   */
  import { t, locale, type TranslateFn } from '../lib/i18n';
  import { ledgerRows, type LedgerRow, type LedgerState } from '../lib/ledger';
  import { fmtClock, decisionActionLabel } from '../lib/format';
  import type { Decision } from '../lib/types';
  import SealStamp from './SealStamp.svelte';

  let {
    decisions,
    max,
  }: {
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    max?: number;
  } = $props();

  const rows = $derived(ledgerRows(decisions, max));

  // Decorative next to a text title — aria-hidden on the glyph span itself
  // (below) keeps a screen reader from reading raw punctuation aloud.
  // `failed` and `unconfirmed` get DIFFERENT glyphs on purpose. An outcome we
  // could not confirm is not a failure — the operation may still be running —
  // so it reads as a question, not a cross (ds-2mc).
  const GLYPH: Record<LedgerState, string> = {
    applied: '✓', open: '◍', noted: '⬤', failed: '✕', unconfirmed: '?',
  };

  function titleFor(row: LedgerRow, tf: TranslateFn): string {
    if (row.state === 'applied') return tf('desk.ledger.appliedTitle');
    if (row.state === 'open') return tf('desk.ledger.openTitle');
    if (row.state === 'failed') return tf('desk.ledger.failedTitle');
    if (row.state === 'unconfirmed') return tf('desk.ledger.unconfirmedTitle');
    return decisionActionLabel(row.decision.action, tf);
  }

  // Best-effort identity only, never a placeholder: a "—" or "unknown" string
  // would claim a fact we don't have. Omitting the <small> entirely (undefined,
  // not '') is the honest rendering when neither field is present.
  function subtitleFor(row: LedgerRow): string | undefined {
    const d = row.decision;
    if (typeof d.pr_title === 'string' && d.pr_title !== '') return d.pr_title;
    if (typeof d.pr_number === 'number') return `#${d.pr_number}`;
    return undefined;
  }
</script>

{#if rows.length > 0}
  <div class="ledger-strip" data-testid="ledger-strip">
    <div class="ledger-strip__heading">{$t('desk.ledger.heading')}</div>
    <div class="ledger-strip__rows">
      {#each rows as row (row.decision.decision_id)}
        <div
          class="ledger-strip__row"
          data-testid="ledger-strip-row"
          data-state={row.state}
        >
          <span class="ledger-strip__time">{fmtClock(row.decision.created_at ?? '', $locale)}</span>
          <span class="ledger-strip__glyph ledger-strip__glyph--{row.state}" aria-hidden="true"
            >{GLYPH[row.state]}</span
          >
          <span class="ledger-strip__title">
            {titleFor(row, $t)}
            {#if subtitleFor(row) !== undefined}
              <small>{subtitleFor(row)}</small>
            {/if}
          </span>
          {#if row.state === 'applied'}
            <SealStamp size="sm" />
          {:else}
            <span></span>
          {/if}
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .ledger-strip__heading {
    padding: 10px 40px;
    font-family: var(--ds-font-mono);
    font-size: 10.5px;
    letter-spacing: 0.2em;
    color: var(--ds-paper-mut);
    border-top: 1px solid var(--ds-paper-rule);
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .ledger-strip__heading::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--ds-paper-rule);
  }

  .ledger-strip__rows {
    padding: 0 40px 26px;
  }

  .ledger-strip__row {
    display: grid;
    grid-template-columns: 58px 18px 1fr auto;
    gap: 14px;
    padding: 10px 0;
    border-bottom: 1px solid var(--ds-paper-rule);
    align-items: baseline;
    font-size: 12.5px;
  }
  .ledger-strip__row:last-child {
    border-bottom: 0;
  }

  .ledger-strip__time {
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-paper-mut);
    font-variant-numeric: tabular-nums;
  }

  .ledger-strip__glyph {
    font-size: 12px;
    line-height: 1.3;
  }
  .ledger-strip__glyph--applied {
    color: var(--ds-ok-green);
  }
  .ledger-strip__glyph--open {
    color: var(--ds-drift-amber);
  }
  .ledger-strip__glyph--noted {
    color: var(--ds-paper-mut);
  }

  .ledger-strip__title {
    color: var(--ds-paper-ink);
  }
  .ledger-strip__title small {
    display: block;
    font-size: 11.5px;
    color: var(--ds-paper-ink-2);
    margin-top: 1px;
  }
</style>
