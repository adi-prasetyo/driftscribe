<script lang="ts">
  // CoverageMeter — the migration-progress treatment of the infra totals
  // (roadmap Wave 1 item 2). Purely presentational: parent passes the
  // /infra/graph `totals`; percentage shaping lives in lib/coverage.ts.
  import { coveragePercent } from '../lib/coverage';

  let {
    totals = null,
  }: {
    /** The /infra/graph totals; null until the first fetch resolves. */
    totals: { resources: number; managed: number; drift: number } | null;
  } = $props();

  const pct = $derived(totals ? coveragePercent(totals.managed, totals.resources) : null);
</script>

{#if totals && pct !== null}
  <div class="coverage" data-testid="coverage-meter">
    <p class="coverage__headline">
      <strong class="coverage__pct" data-testid="coverage-pct">{pct}%</strong>
      of your infrastructure is under IaC management
    </p>
    <div
      class="coverage__bar"
      role="progressbar"
      aria-label="IaC coverage"
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={pct}
      aria-valuetext="{pct}%, {totals.managed} of {totals.resources} resources managed"
    >
      <div class="coverage__fill" data-testid="coverage-fill" style:width="{pct}%"></div>
    </div>
    <!-- {' '} renders the separator space explicitly: Svelte trims literal
         leading whitespace at {#if} boundaries, but never expression tags. -->
    <p class="coverage__detail" data-testid="coverage-detail">
      {totals.managed} of {totals.resources} resources managed{#if totals.drift > 0}{' '}· {totals.drift} not yet in IaC{/if}
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
