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
   *
   * Since ds-zld it also carries the unmatched-DECLARATIONS group (IaC entries
   * with no live resource), which used to live in InfraDiagram's chat panel.
   * Those rows are not in the `estateModel` — they are read straight off the
   * graph — because a declaration never becomes a live node.
   *
   * 2026-07-31 — a SECTION of the desk page, no longer a screen of its own (see
   * docs/plans/2026-07-31-desk-estate-merge-design.md). The desk owns the one
   * instrument band above; this section owns only its own loading/degraded
   * truth, deliberately NOT coupled to the hero's state machine (ds-eh6: a
   * pending approval can coexist with a failed graph fetch).
   */
  import { t } from '../lib/i18n';
  import Icon from './Icon.svelte';
  import type { InfraGraph, PendingApproval, UnmatchedDeclaration } from '../lib/infra_graph';
  import { infraTypeLabel, investigateUnmatchedPrefill } from '../lib/infra_graph';
  import { estateModel, firstAdoptableRow } from '../lib/estate';
  import type { Decision } from '../lib/types';

  let {
    graph,
    decisions,
    pendingApprovals,
    settled = true,
    approvalsStale = false,
    adoptDisabled = false,
    onAdopt,
    onInvestigate,
  }: {
    graph: InfraGraph | null;
    decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
    pendingApprovals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined;
    /** Same contract as ApprovalDesk's — see ds-eh6. Defaults keep existing
     *  test mounts meaning what they meant. */
    settled?: boolean;
    /** The pending-approvals lane specifically was unreliable this cycle — see
     *  OverviewState.approvalsStale. Suppresses ABSENCE-derived affordances
     *  only; positively observed PR chips still render. */
    approvalsStale?: boolean;
    adoptDisabled?: boolean;
    /** Adopt chip click → App prefills the chat with this string (NOT auto-sent). */
    onAdopt?: (prefill: string) => void;
    /** Investigate click (unmatched-declarations group) → the same prefill bridge
     *  as `onAdopt`, kept a SEPARATE prop because it is a different errand: adopt
     *  asks Provision to author IaC for a live resource, investigate asks it to
     *  find out what happened to one that is declared and missing. App wires both
     *  to handleAdopt; a future consumer can tell them apart. */
    onInvestigate?: (prefill: string) => void;
  } = $props();

  // ---- row model ----
  // `decisions` is threaded in for ds-0rm's resolved-PR reconciliation, NOT
  // for row content — see estateModel's `decisions` param. This view and the
  // tour reconcile the same list the same way (App passes TourCard a
  // `reconcileApprovals(...)` of the same two inputs), so the two can never
  // disagree about which PRs are still open.
  const model = $derived(estateModel(graph, pendingApprovals, $t, decisions));
  // The tour's "Adopt your first resource" step spotlights this exact row
  // (data-tour="adopt-target"). App used to compute the same predicate to decide
  // whether the インフラ nav button could host the spotlight instead; the ds-cmc
  // merge deleted both the button and that predicate, so the step's fallback is
  // now the estate section itself (TourCard resolves `fallback: 'estate'`), which
  // is unconditionally present on the desk.
  // No adopt target while the approvals lane is unreliable — the target is
  // chosen from rows whose `pendingPr === null`, which is precisely the
  // unsupported absence. Nulling it here also clears the tour's spotlight.
  const adoptTarget = $derived(approvalsStale ? null : firstAdoptableRow(model));

  function clickAdopt(prefill: string): void {
    if (adoptDisabled) return;
    onAdopt?.(prefill);
  }

  // ---- unmatched IaC declarations (moved here from InfraDiagram, ds-zld) ----
  // Declarations that matched no live resource in the latest CAI snapshot (#244,
  // design 2026-07-11). They are NOT part of `model`: estateModel is built from
  // resourceCards(), which only ever sees live nodes, and a declaration never
  // becomes one. Read straight off the graph, exactly as InfraDiagram did.
  //
  // They belong in this list on the merits, not merely because their old home is
  // being deleted: this section groups the estate by how each thing stands
  // relative to IaC, and a declaration with no resource is the mirror image of
  // the drift group above it — live but undeclared, versus declared but not
  // live. The chat's diagram panel only ever had them because that panel was
  // where the infra graph happened to be rendered.
  //
  // A degraded graph's declaration list is exactly as untrustworthy as its
  // resource list, and this must not become the one figure that survives a
  // failed read (ds-eh6). That is owned by the ONE loaded-branch gate the group
  // renders inside — the same gate every other group answers to — and NOT
  // re-asserted here: a `graph.degraded` term in this expression is unreachable
  // today, so it would sit there un-exercised, and an injected defect in it
  // reddens nothing (verified). The null test is the type system's, not a
  // second arm.
  const unmatched = $derived(graph === null ? null : (graph.unmatched_declarations ?? null));
  const unmatchedEntries = $derived(unmatched?.entries ?? []);

  function clickInvestigate(d: UnmatchedDeclaration): void {
    // `graph` is non-null wherever a row can be clicked (the rows only render
    // inside the loaded branch), but the prefill builder needs it as a value,
    // not as an assertion.
    if (adoptDisabled || graph === null) return;
    onInvestigate?.(investigateUnmatchedPrefill(d, graph, $t));
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<section
  id="estate"
  class="estate-view"
  data-testid="estate-view"
  data-tour="estate"
  aria-label={$t('desk.estate.ariaLabel')}
  tabindex="-1"
>
  <!-- A null graph is only "loading" while the first cycle is still out. Once
       it has settled, a null graph means the fetch FINISHED and failed, and
       "Loading the estate…" would be a claim that something is still in
       progress — which can then sit there until the next 45s poll. Same
       over-claim class as the desk's all-clear, just phrased as optimism. -->
  {#if graph === null && !settled}
    <p class="estate-view__status" data-testid="estate-loading">{$t('desk.estate.loading')}</p>
  {:else if graph === null || graph.degraded}
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
            {:else if row.adoptable && approvalsStale}
              <!-- The Adopt button is an ABSENCE claim: it appears exactly when
                   no open adoption PR was found for this row. On a
                   pending-approvals soft failure that emptiness means "we could
                   not ask GitHub", so offering Adopt would assert something we
                   just failed to establish — and unlike a wrong figure it drives
                   an action (a duplicate PR). Suppressed with a reason rather
                   than silently dropped. Note the same outage would break the
                   adopt itself, since opening the PR needs the same GitHub. -->
              <span class="estate-view__chip estate-view__chip--mute" data-testid="estate-adopt-unknown">
                {$t('desk.estate.adoptUnavailable')}
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

    <!-- Declared in IaC, no live resource — the drift group's mirror image, so
         it sits directly under it and above the managed rows that are neither.
         Rows carry NO status dot on purpose: a dot in this list means a live
         resource is either managed or drifted, and these are not live resources
         at all. The lead line is not decoration — without it "not found live"
         reads as a deletion claim, and it is evidence (index lag, an unapplied
         change), not proof. -->
    {#if unmatchedEntries.length > 0}
      <h2 class="estate-view__group" data-testid="estate-group-unmatched">
        {$t('desk.estate.unmatchedGroup', { n: unmatched?.count ?? unmatchedEntries.length })}
      </h2>
      <p class="estate-view__lead" data-testid="estate-unmatched-lead">
        {$t('infra.unmatched.lead')}
      </p>
      <div class="estate-view__rows" aria-label={$t('infra.unmatched.ariaLabel')}>
        {#each unmatchedEntries as d (d.id)}
          <div class="estate-view__row estate-view__row--decl" data-testid="estate-unmatched-row">
            <!-- The dot's CELL, with no dot in it. Keeping the element (rather
                 than dropping to a 3-column row) holds these names on the same
                 left edge as every other group's, and hands the row the mobile
                 restack's `grid-area: 1 / 1` for free. -->
            <span class="estate-view__dot estate-view__dot--none" aria-hidden="true"></span>
            <span class="estate-view__decl">
              <span class="estate-view__name">{d.label}</span>
              {#if d.address}
                <code class="estate-view__addr">{d.address}</code>
              {/if}
            </span>
            <span class="estate-view__type">{infraTypeLabel(d.asset_type, d.type_label, $t)}</span>
            <button
              type="button"
              class="estate-view__chip estate-view__chip--icon"
              data-testid="estate-unmatched-investigate"
              disabled={adoptDisabled}
              title={adoptDisabled ? $t('infra.disabledHint') : $t('infra.unmatched.investigateHint')}
              onclick={() => clickInvestigate(d)}
              ><Icon name="compass" size={12} />{$t('infra.unmatched.investigate')}</button
            >
          </div>
        {/each}
      </div>
      {#if unmatched && unmatched.truncated > 0}
        <p class="estate-view__more" data-testid="estate-unmatched-more">
          {$t('infra.unmatched.trailer', { n: unmatched.truncated })}
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

    <!-- Folded (2026-07-31 merge): nothing here is actionable — these are
         unmanaged resources of a type DriftScribe cannot adopt — so the COUNT
         is the information and the rows are detail-on-demand. Same pattern as
         the system-managed fold below. -->
    {#if model.untracked.length > 0}
      <details class="estate-view__fold" data-testid="estate-untracked-fold">
        <summary>{$t('desk.estate.untrackedGroup', { n: model.untracked.length })}</summary>
        <div class="estate-view__rows">
          {#each model.untracked as row (row.nodeId)}
            <div class="estate-view__row estate-view__row--un" data-testid="estate-row">
              <span class="estate-view__dot" aria-hidden="true"></span>
              <span class="estate-view__name">{row.label}</span>
              <span class="estate-view__type">{row.typeLabel}</span>
            </div>
          {/each}
        </div>
      </details>
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
    /* `width: 100%` is load-bearing since the 2026-07-31 merge, and it is not
       redundant with max-width. This is a GRID ITEM with auto margins, so
       without an explicit width it is sized shrink-to-fit — 384px at 1280,
       against the 780px approval-desk card directly above it. Two centered
       cards of different widths in one column is exactly the "bolted-on
       frontend" reading the merge exists to fix. ApprovalDesk carries the same
       pair; keep them identical. */
    width: 100%;
    max-width: 780px;
    margin: 0 auto;
    background: var(--ds-bg);
    color: var(--ds-fg);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius, 6px);
    overflow: hidden;
  }

  /* Programmatic scroll target (the band's managed/drift stats focus this
     section so keyboard focus follows the scroll). Not keyboard-interactive —
     it must not grow a focus ring. */
  .estate-view:focus {
    outline: none;
  }

  /* Loading / degraded status. This is the only thing on the screen when the
     estate cannot be read, so it carries the entire message — ink-2, not the
     lightest grey (ds-qbo). */
  .estate-view__status {
    margin: 0;
    padding: 40px;
    font-family: var(--ds-font-mono);
    font-size: 12.5px;
    color: var(--ds-fg-soft);
  }

  .estate-view__group {
    margin: 0;
    padding: 20px 40px 4px;
    font-family: var(--ds-font-mono);
    font-size: 10.5px;
    letter-spacing: 0.18em;
    /* Deliberately NO text-transform: uppercase. The mockup's .egrp doesn't
       have one, and adding it renders our own term "IaC" as "IAC" in every EN
       group header — this section's most repeated string. Casing belongs in
       the catalog, where a translator can see it. */
    color: var(--ds-faint);
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
    border-bottom: 1px solid var(--ds-border);
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
    background: var(--ds-ok);
    justify-self: center;
  }
  .estate-view__row--un .estate-view__dot {
    background: transparent;
    border: 1.5px solid var(--ds-warn);
  }
  .estate-view__dot--sys {
    background: var(--ds-border);
  }
  /* An unmatched DECLARATION has no live resource to be managed or drifted, so
     it gets no dot — only the cell, so the name still lines up with the groups
     above and below. */
  .estate-view__dot--none {
    background: transparent;
  }

  .estate-view__name {
    color: var(--ds-fg);
    overflow-wrap: break-word;
  }

  /* Name over HCL address, in ONE grid cell — the address is a second identity
     for the same thing, not a fourth column. A column of its own would have to
     be `auto` (the row's other tracks are), and a long address would then push
     the Investigate chip off the card, which `.estate-view`'s overflow:hidden
     would quietly clip rather than scroll (ds-cmc). */
  .estate-view__decl {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }
  .estate-view__addr {
    font-family: var(--ds-font-mono);
    font-size: 11px;
    color: var(--ds-faint);
    overflow-wrap: anywhere;
  }
  /* The lead under the group heading. These rows are EVIDENCE (index lag, an
     unapplied change), not proof that something was deleted, and the heading
     alone cannot carry that. */
  .estate-view__lead {
    margin: 0;
    padding: 0 40px 8px;
    font-size: 12px;
    line-height: 1.6;
    color: var(--ds-faint);
  }
  .estate-view__type {
    font-family: var(--ds-font-mono);
    font-size: 11px;
    color: var(--ds-faint);
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
  /* Only the icon-bearing chip goes inline-flex: switching the plain adopt chip
     to it would shift its text baseline for no reason. */
  .estate-view__chip--icon {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  /* The "adoption status unknown" stand-in. Muted and NOT a button: it must not
     read as an action, because the whole point is that we cannot say whether
     the action is appropriate. */
  .estate-view__chip--mute {
    color: var(--ds-faint);
    border-style: dashed;
    cursor: default;
  }
  .estate-view__chip--q {
    border-color: var(--ds-border);
    color: var(--ds-faint);
    cursor: default;
  }

  .estate-view__more {
    margin: 0;
    padding: 12px 40px 6px;
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-faint);
  }

  .estate-view__fold {
    margin: 10px 40px 24px;
    border: 1px solid var(--ds-border);
    border-radius: 4px;
    padding: 11px 16px;
    font-size: 12px;
    color: var(--ds-faint);
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
    color: var(--ds-faint);
  }

  .estate-view__legend {
    display: flex;
    gap: 20px;
    padding: 16px 40px 24px;
    font-size: 11.5px;
    color: var(--ds-faint);
    border-top: 1px solid var(--ds-border);
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
    background: var(--ds-ok);
    margin-right: 7px;
  }
  .estate-view__legend-dot--un {
    background: transparent;
    border: 1.5px solid var(--ds-warn);
  }

  /* Phone widths: the row restacks instead of clipping.
     `20px 1fr auto auto` needs the type label AND the adopt chip to fit beside
     the name, and both refuse to shrink (`white-space: nowrap` on each, and the
     chip is a tap target). Below ~410px they don't, and the chip runs past the
     card — measured 2026-07-31: last broken at 390 (ja 4px over, en 16px),
     clean from 420.
     The reason this was never noticed is worth keeping: `.estate-view` is
     `overflow: hidden`, so the button is CLIPPED rather than scrolled to, and a
     document-level `scrollWidth === clientWidth` check — the exact narrow-width
     check this plan prescribes — calls the page clean while a control sits half
     off the card. The pin in transparency.smoke.ts measures the button against
     the card instead, for that reason.
     Restack, don't shrink: the name is the identity, the type says what it is,
     the chip is the action, and each gets its own line under the dot.

     ONE LINE PER ITEM, not two — the first attempt put type and chip together on
     line 2 and that was wrong. They are BOTH `nowrap`, so pairing them just
     moves the collision: at 390 the row offers ~278px while `Pub/Sub
     サブスクリプション` (152px) beside 「取り込み状況を確認できません」 (191px) wants
     343px. The type then overflowed its `minmax(0, 1fr)` track and ran UNDER the
     chip. Measured overlap at 390 on every chip variant in both locales, worst
     ja/unknown at 69px. Stacking is the only arrangement that cannot be broken
     by a longer translation, which matters because the widest chip in each
     locale is a different string.

     460 rather than the measured 410 leaves headroom for a longer resource name
     or a wider translated chip. */
  @media (max-width: 460px) {
    .estate-view__row {
      grid-template-columns: 20px minmax(0, 1fr);
      row-gap: 6px;
    }
    .estate-view__dot {
      grid-area: 1 / 1;
    }
    .estate-view__name {
      grid-area: 1 / 2;
    }
    .estate-view__type {
      grid-area: 2 / 2;
      justify-self: start;
    }
    .estate-view__chip {
      grid-area: 3 / 2;
      justify-self: start;
    }
  }
</style>
