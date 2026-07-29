<script lang="ts">
  /**
   * EstateView — screen two of the composite mockup (docs/plans/2026-07-28-
   * composite-mockup.html "SCREEN 2 — 推定図"): the estate, one click below the
   * instrument band. Rows are grouped by STATUS (drift first, then managed)
   * and flattened across resource TYPES — the inverse of InfraDiagram's
   * per-type card grid — via the pure lib/estate.ts model. Filled dots are
   * IaC-managed, hollow amber rings are drift; every drift row carries ONE
   * action: an adopt chip, or (when an adoption PR is already open for it) a
   * non-interactive "PR #N reviewing" chip instead.
   *
   * Dumb: performs NO fetches of its own — `graph`/`decisions`/
   * `pendingApprovals` are the overview store's current snapshot, same as
   * ApprovalDesk. NEVER imports Mermaid (that stays InfraDiagram's preview-
   * only concern); this is a plain DOM row list.
   */
  import { t, locale } from '../lib/i18n';
  import { resourceCards, scopeTotals, type InfraGraph, type PendingApproval } from '../lib/infra_graph';
  import { awaitingCount } from '../lib/desk';
  import { estateModel, firstAdoptableRow } from '../lib/estate';
  import type { Decision } from '../lib/types';
  import type { AppView } from '../lib/deeplink';
  import InstrumentBand from './InstrumentBand.svelte';

  let {
    graph,
    decisions,
    pendingApprovals,
    adoptDisabled = false,
    onAdopt,
    onNavigate,
  }: {
    graph: InfraGraph | null;
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    pendingApprovals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined;
    adoptDisabled?: boolean;
    /** Adopt chip click → App prefills the chat with this string (NOT auto-sent). */
    onAdopt?: (prefill: string) => void;
    onNavigate: (view: AppView) => void;
  } = $props();

  // ---- instrument band numbers — SAME derivation as ApprovalDesk, imported
  // never re-derived, so the two views can never disagree about the figures. ----
  const cards = $derived(graph ? resourceCards(graph, $t) : []);
  const scope = $derived(scopeTotals(cards, graph?.totals?.resources ?? 0));
  const awaiting = $derived(awaitingCount({ decisions, pendingApprovals, locale: $locale }));

  // ---- row model ----
  const model = $derived(estateModel(graph, pendingApprovals, $t));
  // The tour's "Adopt your first resource" step spotlights this exact row
  // (data-tour="adopt-target"); App.svelte computes the SAME predicate off the
  // SAME model for its nav-button fallback, so the two markers are always
  // mutually exclusive (see firstAdoptableRow's own doc comment).
  const adoptTarget = $derived(firstAdoptableRow(model));

  function clickAdopt(prefill: string): void {
    if (adoptDisabled) return;
    onAdopt?.(prefill);
  }
</script>

<section
  class="estate-view"
  data-testid="estate-view"
  data-tour="estate"
  aria-label={$t('desk.estate.ariaLabel')}
>
  <InstrumentBand managed={scope.managed} drift={scope.drift} {awaiting} {onNavigate} />

  {#if graph === null}
    <p class="estate-view__status" data-testid="estate-loading">{$t('desk.estate.loading')}</p>
  {:else if graph.degraded}
    <p class="estate-view__status" data-testid="estate-degraded">{$t('desk.estate.degraded')}</p>
  {:else}
    {#if model.drift.length > 0}
      <h2 class="estate-view__group" data-testid="estate-group-drift">
        {$t('desk.estate.driftGroup', { n: model.drift.length })}
      </h2>
      <div class="estate-view__rows">
        {#each model.drift as row (row.nodeId)}
          <div
            class="estate-view__row estate-view__row--un"
            data-testid="estate-row"
            data-tour={row === adoptTarget ? 'adopt-target' : undefined}
          >
            <span class="estate-view__dot" aria-hidden="true"></span>
            <span class="estate-view__name">{row.label}</span>
            <span class="estate-view__type">{row.typeLabel}</span>
            {#if row.pendingPr !== null}
              <span class="estate-view__chip estate-view__chip--q" data-testid="estate-pr-chip">
                {$t('desk.estate.prPending', { pr: row.pendingPr })}
              </span>
            {:else if row.adoptable}
              <button
                type="button"
                class="estate-view__chip"
                data-testid="estate-adopt-btn"
                disabled={adoptDisabled}
                onclick={() => clickAdopt(row.prefill)}>{$t('desk.estate.adoptButton')}</button
              >
            {/if}
          </div>
        {/each}
      </div>
      {#if model.driftHidden > 0}
        <p class="estate-view__more" data-testid="estate-drift-more">
          {$t('desk.estate.driftMore', { n: model.driftHidden })}
        </p>
      {/if}
    {/if}

    {#if model.managed.length > 0}
      <h2 class="estate-view__group" data-testid="estate-group-managed">
        {$t('desk.estate.managedGroup', { n: model.managed.length })}
      </h2>
      <div class="estate-view__rows">
        {#each model.managed as row (row.nodeId)}
          <div class="estate-view__row" data-testid="estate-row">
            <span class="estate-view__dot" aria-hidden="true"></span>
            <span class="estate-view__name">{row.label}</span>
            <span class="estate-view__type">{row.typeLabel}</span>
          </div>
        {/each}
      </div>
    {/if}

    {#if model.untracked.length > 0}
      <h2 class="estate-view__group" data-testid="estate-group-untracked">
        {$t('desk.estate.untrackedGroup', { n: model.untracked.length })}
      </h2>
      <div class="estate-view__rows">
        {#each model.untracked as row (row.nodeId)}
          <div class="estate-view__row estate-view__row--un" data-testid="estate-row">
            <span class="estate-view__dot" aria-hidden="true"></span>
            <span class="estate-view__name">{row.label}</span>
            <span class="estate-view__type">{row.typeLabel}</span>
          </div>
        {/each}
      </div>
    {/if}

    {#if model.systemManagedTotal > 0}
      <details class="estate-view__fold" data-testid="estate-system-fold">
        <summary>{$t('desk.estate.systemManagedFold', { n: model.systemManagedTotal })}</summary>
        <div class="estate-view__rows">
          {#each model.systemManaged as row (row.nodeId)}
            <div class="estate-view__row" data-testid="estate-system-row">
              <span class="estate-view__dot estate-view__dot--sys" aria-hidden="true"></span>
              <span class="estate-view__name">{row.label}</span>
              <span class="estate-view__type">{row.typeLabel}</span>
            </div>
          {/each}
        </div>
      </details>
    {/if}

    {#if model.otherTypes > 0}
      <p class="estate-view__other" data-testid="estate-other">
        {$t(
          model.otherTypes === 1
            ? 'desk.estate.otherResources.one'
            : 'desk.estate.otherResources.other',
          { other: model.otherResources, types: model.otherTypes },
        )}
      </p>
    {/if}
  {/if}

  <!-- Suppressed while loading/degraded: with no rows on screen, a key for a
       dot and a ring explains a coding the operator cannot see, and reads as
       though the view rendered something it didn't. -->
  {#if graph !== null && !graph.degraded}
  <div class="estate-view__legend" data-testid="estate-legend">
    <span class="estate-view__legend-item"
      ><i class="estate-view__legend-dot" aria-hidden="true"></i>{$t('desk.estate.legendManaged')}</span
    >
    <span class="estate-view__legend-item"
      ><i class="estate-view__legend-dot estate-view__legend-dot--un" aria-hidden="true"></i
      >{$t('desk.estate.legendDrift')}</span
    >
  </div>
  {/if}
</section>

<style>
  .estate-view {
    max-width: 780px;
    margin: 0 auto;
    background: var(--ds-paper);
    color: var(--ds-paper-ink);
    border: 1px solid var(--ds-paper-rule);
    border-radius: var(--ds-radius, 6px);
    overflow: hidden;
  }

  .estate-view__status {
    margin: 0;
    padding: 40px;
    font-family: var(--ds-font-mono);
    font-size: 12.5px;
    color: var(--ds-paper-mut);
  }

  .estate-view__group {
    margin: 0;
    padding: 20px 40px 4px;
    font-family: var(--ds-font-mono);
    font-size: 10.5px;
    letter-spacing: 0.18em;
    /* Deliberately NO text-transform: uppercase. The mockup's .egrp doesn't
       have one, and adding it renders our own term "IaC" as "IAC" in every EN
       group header — the estate view's most repeated string. Casing belongs in
       the catalog, where a translator can see it. */
    color: var(--ds-paper-mut);
    font-weight: 400;
  }

  .estate-view__rows {
    padding: 0 40px;
  }
  .estate-view__row {
    display: grid;
    grid-template-columns: 20px 1fr auto auto;
    gap: 14px;
    padding: 11px 0;
    border-bottom: 1px solid var(--ds-paper-rule);
    align-items: center;
    font-size: 13.5px;
  }
  .estate-view__rows > .estate-view__row:last-child {
    border-bottom: 0;
  }

  .estate-view__dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--ds-ok-green);
    justify-self: center;
  }
  .estate-view__row--un .estate-view__dot {
    background: transparent;
    border: 1.5px solid var(--ds-drift-amber);
  }
  .estate-view__dot--sys {
    background: var(--ds-paper-rule);
  }

  .estate-view__name {
    color: var(--ds-paper-ink);
    overflow-wrap: break-word;
  }
  .estate-view__type {
    font-family: var(--ds-font-mono);
    font-size: 11px;
    color: var(--ds-paper-mut);
    white-space: nowrap;
  }

  .estate-view__chip {
    font-family: inherit;
    font-size: 11.5px;
    padding: 5px 14px;
    border: 1px solid var(--ds-navy);
    border-radius: 3px;
    color: var(--ds-navy);
    background: transparent;
    cursor: pointer;
    white-space: nowrap;
  }
  .estate-view__chip:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .estate-view__chip--q {
    border-color: var(--ds-paper-rule);
    color: var(--ds-paper-mut);
    cursor: default;
  }

  .estate-view__more {
    margin: 0;
    padding: 12px 40px 6px;
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-paper-mut);
  }

  .estate-view__fold {
    margin: 10px 40px 24px;
    border: 1px solid var(--ds-paper-rule);
    border-radius: 4px;
    padding: 11px 16px;
    font-size: 12px;
    color: var(--ds-paper-mut);
    font-family: var(--ds-font-mono);
  }
  .estate-view__fold .estate-view__rows {
    padding: 0;
    margin-top: 10px;
  }

  .estate-view__other {
    margin: 0;
    padding: 0 40px 20px;
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-paper-mut);
  }

  .estate-view__legend {
    display: flex;
    gap: 20px;
    padding: 16px 40px 24px;
    font-size: 11.5px;
    color: var(--ds-paper-mut);
    border-top: 1px solid var(--ds-paper-rule);
  }
  .estate-view__legend-item {
    display: inline-flex;
    align-items: center;
  }
  .estate-view__legend-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--ds-ok-green);
    margin-right: 7px;
  }
  .estate-view__legend-dot--un {
    background: transparent;
    border: 1.5px solid var(--ds-drift-amber);
  }
</style>
