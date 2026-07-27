<script lang="ts">
  // CoverageMeter — the migration-progress treatment of the infra totals
  // (roadmap Wave 1 item 2). Purely presentational: parent passes the
  // /infra/graph `totals`; percentage shaping lives in lib/coverage.ts.
  import { coveragePercent } from '../lib/coverage';
  import { t, locale, fmtNumber } from '../lib/i18n';

  let {
    totals = null,
    subject,
  }: {
    /** The /infra/graph totals; null until the first fetch resolves. */
    totals: { resources: number; managed: number; drift: number } | null;
    /**
     * What the percentage is "of" in the headline. Defaults to the localized
     * "your infrastructure"; the Infrastructure panel passes a scope-aware
     * subject ("your supported infrastructure") when it feeds scope totals.
     */
    subject?: string;
  } = $props();

  const pct = $derived(totals ? coveragePercent(totals.managed, totals.resources) : null);
  const pctText = $derived(pct === null ? null : fmtNumber(pct, $locale));
  const subjectText = $derived(subject ?? $t('infra.coverage.subjectDefault'));
  // The headline is one whole-sentence catalog key per locale (word order
  // differs), with a literal '{{PCT}}' marker split out here to slot in the
  // separately-styled/tested percentage <strong> — see infra.coverage.headline.
  const headlineParts = $derived($t('infra.coverage.headline', { subject: subjectText }).split('{{PCT}}'));
</script>

{#if totals && pct !== null}
  <div class="coverage" data-testid="coverage-meter">
    <p class="coverage__headline">
      {headlineParts[0]}<strong class="coverage__pct" data-testid="coverage-pct">{pctText}%</strong
      >{headlineParts[1] ?? ''}
    </p>
    <div
      class="coverage__bar"
      role="progressbar"
      aria-label={$t('infra.coverage.ariaLabel')}
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={pct}
      aria-valuetext={$t('infra.coverage.ariaValueText', {
        pct: pctText ?? '',
        subject: subjectText,
        managed: fmtNumber(totals.managed, $locale),
        resources: fmtNumber(totals.resources, $locale),
      })}
    >
      <div class="coverage__fill" data-testid="coverage-fill" style:width="{pct}%"></div>
    </div>
    <p class="coverage__detail" data-testid="coverage-detail">
      {#if totals.drift > 0}
        {$t('infra.coverage.detailWithDrift', {
          managed: fmtNumber(totals.managed, $locale),
          resources: fmtNumber(totals.resources, $locale),
          drift: fmtNumber(totals.drift, $locale),
        })}
      {:else}
        {$t('infra.coverage.detail', {
          managed: fmtNumber(totals.managed, $locale),
          resources: fmtNumber(totals.resources, $locale),
        })}
      {/if}
    </p>
  </div>
{/if}

<style>
  .coverage {
    margin: 0 0 var(--ds-sp-4);
  }
  .coverage__headline {
    margin: 0 0 var(--ds-sp-2);
    font-size: var(--ds-fs-2);
    color: var(--ds-muted);
  }
  .coverage__pct {
    color: var(--ds-fg);
    font-variant-numeric: tabular-nums;
  }
  .coverage__bar {
    height: 0.5rem;
    border-radius: var(--ds-radius-sm);
    background: var(--ds-neutral-surface);
    border: 1px solid var(--ds-border-strong);
    overflow: hidden;
  }
  .coverage__fill {
    height: 100%;
    background: var(--ds-ok-surface);
    /* No border on the fill: at 0% a zero-width div would still paint a 1px
       border sliver — the track's border alone frames the bar. */
    transition: width var(--ds-dur-fast) var(--ds-ease);
  }
  .coverage__detail {
    margin: var(--ds-sp-2) 0 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
