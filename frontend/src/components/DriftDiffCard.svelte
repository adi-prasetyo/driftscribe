<script lang="ts">
  // DriftDiffCard — a structured "what drifted" table for a historical drift
  // decision. Renders ONLY the safe rows produced by diffRows() (lib/diff.ts):
  // values are redacted with the same rule the backend uses for the GitHub
  // PR/issue body, and every cell is auto-escaped text ({value}, never {@html}).
  // Self-suppresses when there are no diffs, so App.svelte can mount it
  // unconditionally for any open historical decision.

  import type { Decision } from '../lib/types';
  import { diffRows } from '../lib/diff';
  import { t } from '../lib/i18n';

  let { decision }: { decision: Decision | null } = $props();

  const rows = $derived(diffRows(decision));
</script>

{#if rows.length > 0}
  <section class="ds-card drift-diff-card" data-testid="drift-diff-card" aria-label={$t('infra.driftDiff.ariaLabel')}>
    <p class="ds-label drift-diff-card__label">{$t('infra.driftDiff.label')}</p>
    <table class="drift-diff-card__table">
      <thead>
        <tr>
          <th scope="col">{$t('infra.driftDiff.colVar')}</th>
          <th scope="col">{$t('infra.driftDiff.colExpected')}</th>
          <th scope="col">{$t('infra.driftDiff.colLive')}</th>
          <th scope="col">{$t('infra.driftDiff.colStatus')}</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as r, i (r.name + i)}
          <tr>
            <td><code class="ds-code">{r.name}</code></td>
            <td><code class="ds-code">{r.expected}</code></td>
            <td><code class="ds-code">{r.live}</code></td>
            <td>
              {#if r.status}
                <span class="ds-pill ds-pill--{r.badge}">{r.status}</span>
              {:else}
                <span class="ds-subtle">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

<style>
  .drift-diff-card {
    /* A neutral left accent — distinct from FinalResponse's hero green and from
       DecisionSummary's strong border. */
    border-left: 3px solid var(--ds-border-strong);
    padding: var(--ds-sp-5) var(--ds-sp-6);
  }

  .drift-diff-card__label {
    display: block;
    margin: 0 0 var(--ds-sp-4);
    color: var(--ds-muted);
  }

  .drift-diff-card__table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--ds-fs-2);
  }

  .drift-diff-card__table th {
    text-align: left;
    padding: 0 var(--ds-sp-4) var(--ds-sp-2) 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    border-bottom: 1px solid var(--ds-border);
  }

  .drift-diff-card__table td {
    padding: var(--ds-sp-3) var(--ds-sp-4) var(--ds-sp-3) 0;
    border-bottom: 1px solid var(--ds-border);
    vertical-align: top;
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .drift-diff-card__table tr:last-child td {
    border-bottom: none;
  }

  @media (max-width: 540px) {
    .drift-diff-card__table th:first-child,
    .drift-diff-card__table td:first-child {
      max-width: 8rem;
    }
  }
</style>
