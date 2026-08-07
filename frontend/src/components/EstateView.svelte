<script lang="ts">
  /**
   * EstateView — screen two of the composite mockup (docs/plans/2026-07-28-
   * composite-mockup.html "SCREEN 2 — 推定図"): the estate, one click below the
   * instrument band. Rows are grouped by STATUS (drift first, then managed)
   * and flattened across resource TYPES — the inverse of InfraDiagram's
   * per-type card grid — via the pure lib/estate.ts model. Filled dots are
   * IaC-managed, hollow amber rings are drift; every drift row carries ONE
   * control: an adopt chip, or (when an adoption PR is already open for it) a
   * "PR #N awaiting review" chip linking to that PR's approval page.
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
  import { t, locale } from '../lib/i18n';
  import { iacApprovalHref } from '../lib/approval';
  import Icon from './Icon.svelte';
  import type { InfraGraph, PendingApproval, UnmatchedDeclaration } from '../lib/infra_graph';
  import {
    adoptionTrusted,
    infraTypeLabel,
    investigateUnmatchedPrefill,
    snapshotFreshness,
  } from '../lib/infra_graph';
  import { estateModel, firstAdoptableRow } from '../lib/estate';
  import type { Decision } from '../lib/types';

  let {
    graph,
    decisions,
    pendingApprovals,
    settled = true,
    approvalsStale = false,
    graphStale = false,
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
    /** The LAST `/infra/graph` fetch failed, so `graph` is a RETAINED snapshot
     *  (OverviewState.graphStale). Its numbers are still worth showing, but its
     *  `iac_snapshot_stale` is an assurance and must not survive as one: a
     *  retained `false` would report "checked, current" about a check that did
     *  not run this cycle. Degrades freshness to `unverified`, never to
     *  `stale` — a failed fetch is no evidence of a mismatch either. */
    graphStale?: boolean;
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
  // ds-1vn. The ONE derivation, shared with the guided tour via lib/infra_graph
  // (Codex r3). EstateView had its own copy and the tour went on recommending
  // an adoption from a snapshot this section had just disowned; a shared
  // function is what makes the two unable to disagree again.
  const freshness = $derived(snapshotFreshness(graph, graphStale));
  const canAdopt = $derived(adoptionTrusted(freshness));

  // No adopt target while the approvals lane is unreliable — the target is
  // chosen from rows whose `pendingPr === null`, which is precisely the
  // unsupported absence. Nulling it here also clears the tour's spotlight.
  //
  // Nulled on a STALE snapshot too (ds-1vn, Codex review): the target is chosen
  // from `row.adoptable`, whose Adopt button that state replaces with a mute
  // chip — so without this the tour would spotlight a row and point at a
  // control that is no longer there.
  const adoptTarget = $derived(approvalsStale || !canAdopt ? null : firstAdoptableRow(model));

  // ---- drift cap (ds-3em) ----
  // The estate card is compacted so the whole desk page stays readable, but
  // this group must NOT fold away like its non-actionable siblings below: drift
  // is the product's main actionable signal, and a count alone does not let an
  // operator act. First three rows, then a toggle.
  //
  // TWO-WAY here, deliberately, where the ledger strip one card up is one-way.
  // That asymmetry is not an oversight: re-capping the ledger could hide a row
  // whose decision record is open (LedgerStrip's own header comment). Drift rows
  // carry no such state, so collapsing again costs nothing.
  const DRIFT_VISIBLE = 3;
  let driftExpanded = $state(false);
  // The cap must never hide the tour's spotlight subject — `adoptTarget`, the
  // row carrying data-tour="adopt-target". Same rule and reason as ledgerRows'
  // `keepTraceId`: cap first, then append the row the cap would have dropped,
  // rather than widening the cap. Without this, "Adopt your first resource"
  // points at a row that is not on screen.
  const driftShown = $derived.by(() => {
    if (driftExpanded) return model.drift;
    const head = model.drift.slice(0, DRIFT_VISIBLE);
    if (adoptTarget !== null && !head.includes(adoptTarget)) head.push(adoptTarget);
    return head;
  });

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
    <!-- ds-1vn — how old is the iac/ tree this estate was read from?
         First thing in the branch, deliberately: it qualifies every row below
         it, including the Adopt buttons, and a notice an operator reaches
         AFTER clicking Adopt has not warned anybody.
         Neither `caveat` nor `degraded` could carry this — InfraDiagram hides
         the caveat when degraded, and degraded replaces the whole estate with
         a generic line, hiding the very rows being qualified. -->
    {#if freshness === 'stale'}
      <p class="estate-view__snapshot estate-view__snapshot--stale" data-testid="estate-snapshot-stale">
        {$t('desk.estate.snapshotStale')}
      </p>
    {:else if freshness === 'unverified'}
      <p class="estate-view__snapshot" data-testid="estate-snapshot-unverified">
        {$t('desk.estate.snapshotUnverified')}
      </p>
    {/if}

    {#if model.drift.length > 0}
      <h2 class="estate-view__group" data-testid="estate-group-drift">
        {$t('desk.estate.driftGroup', { n: model.drift.length })}
      </h2>
      <div class="estate-view__rows">
        {#each driftShown as row (row.nodeId)}
          <div
            class="estate-view__row estate-view__row--un"
            data-testid="estate-row"
            data-tour={row === adoptTarget ? 'adopt-target' : undefined}
          >
            <span class="estate-view__dot" aria-hidden="true"></span>
            <span class="estate-view__name">{row.label}</span>
            <span class="estate-view__type">{row.typeLabel}</span>
            {#if row.pendingPr !== null}
              {@const prHref = iacApprovalHref(row.pendingPr, $locale)}
              {@const prLabel = approvalsStale
                ? $t('desk.estate.prPendingUnrefreshed', { pr: row.pendingPr })
                : $t('desk.estate.prPending', { pr: row.pendingPr })}
              <!-- The PR chip stays FIRST and stays on a stale/unverified
                   snapshot: unlike everything below it, this is a positively
                   observed fact from the GitHub listing rather than an absence
                   read off the iac/ tree.
                   But its WORDING follows the approvals lane (ds-1vn r5). On a
                   pending-approvals failure the store retains the previous
                   list, so "awaiting review" — present tense — outlives the
                   fetch that established it, and a PR closed or merged
                   elsewhere would sit here labelled awaiting review forever.
                   Identity survives a retained value; a verdict does not. Same
                   split already made for the graph's freshness assurance.

                   ds-wd2.17 — it LINKS, in both freshness states, and the
                   sentence that used to end the paragraph above ("and it drives
                   no action") had to go with the change rather than be left
                   standing over code that no longer honours it.
                   The desk queue shows ONE pending item at a time while the band
                   counts them all, and an adopt PR gets no ledger row to click:
                   propose_adoption_tool never writes a decision doc, so the row
                   only exists once the PR has been APPROVED. That made this chip
                   the only pixel in the app naming the second open proposal, and
                   it was inert. Linking it is not the action the old comment
                   refused: navigation is not a mutation, and /iac-approvals/{pr}
                   re-reads live state on GET — it will serve a spent-token,
                   paused or autonomy-blocked form if that is the truth by the
                   time the operator arrives. Same reasoning #307 used to collapse
                   the desk's Approve+Reject pair into one Review anchor. So a
                   retained-list PR that closed elsewhere costs a wasted click on
                   a page that says so, not a wrong write.

                   Null href keeps the inert span: findPendingPr returns
                   `a.pr_number` without validating it, so a malformed backend
                   row genuinely reaches here — this is a live gate, not a
                   formality. InfraDiagram's per-row link is gated the same way. -->
              {#if prHref}
                <a
                  class="estate-view__chip estate-view__chip--pr"
                  data-testid="estate-pr-chip"
                  href={prHref}
                  target="_blank"
                  rel="noopener">{prLabel}</a
                >
              {:else}
                <span class="estate-view__chip estate-view__chip--q" data-testid="estate-pr-chip">
                  {prLabel}
                </span>
              {/if}
            {:else if row.adoptable && !canAdopt}
              <!-- ds-1vn. The SECOND absence claim on this row, and the one
                   that caused the incident: "not declared in IaC" is read off
                   the worker's baked `iac/`. adopt-probe-topic was declared and
                   merged on 07-31 and still showed an Adopt button, because the
                   worker was baked on 07-29. Same reasoning as the
                   approvalsStale arm below — an unsupported absence must not
                   drive an ACTION.

                   Suppressed on BOTH non-fresh states (Codex r3). An earlier
                   cut spared `unverified`, arguing it was only absence of
                   evidence and the state every prior build shipped in. The
                   counterexample is the very rollout that produces it: deploy
                   the coordinator ahead of the worker, and the old worker has
                   no hash AND is genuinely missing the new declaration — the
                   incident exactly, wearing "unknown" instead of "mismatch".
                   Prior releases shipping without the check establish
                   compatibility, not safety. `adoptionTrusted` holds the rule
                   so the tour cannot disagree with this row. -->
              <span
                class="estate-view__chip estate-view__chip--mute"
                data-testid={freshness === 'stale'
                  ? 'estate-adopt-stale'
                  : 'estate-adopt-unverified'}
              >
                {freshness === 'stale'
                  ? $t('desk.estate.adoptSnapshotStale')
                  : $t('desk.estate.adoptSnapshotUnverified')}
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
      {#if model.drift.length > DRIFT_VISIBLE}
        <button
          type="button"
          class="estate-view__toggle"
          data-testid="estate-drift-toggle"
          aria-expanded={driftExpanded}
          onclick={() => (driftExpanded = !driftExpanded)}
        >
          {driftExpanded
            ? $t('desk.estate.driftShowLess')
            : $t('desk.estate.driftShowAll', { n: model.drift.length })}
        </button>
      {/if}
      <!-- Untouched by the toggle above, and the two counts must never be
           merged. This one reports rows the BACKEND truncated (Σ card
           hiddenUnmanaged) — their names never reached this client, so no
           client-side control can reveal them. Expanded, the toggle shows the
           rows it has and this still reports the ones it does not. -->
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

    <!-- Folded (ds-3em), joining the two folds below it. Nothing on a managed
         row is actionable — these are resources already declared in IaC, doing
         exactly what they should — so the COUNT is the information and the
         names are detail-on-demand. Uncapped inside: unlike the drift group
         above, there is no reason to ration rows nobody has to act on.
         The `<h2>` this replaces is not moved into the summary. The sibling
         folds use a bare `<summary>`, and the heading walk still runs h1 (brand)
         → h2 (desk) → h2 (drift/unmatched) with no gap. -->
    {#if model.managed.length > 0}
      <details class="estate-view__fold" data-testid="estate-managed-fold">
        <summary>{$t('desk.estate.managedGroup', { n: model.managed.length })}</summary>
        <div class="estate-view__rows">
          {#each model.managed as row (row.nodeId)}
            <div class="estate-view__row" data-testid="estate-row">
              <span class="estate-view__dot" aria-hidden="true"></span>
              <span class="estate-view__name">{row.label}</span>
              <span class="estate-view__type">{row.typeLabel}</span>
            </div>
          {/each}
        </div>
      </details>
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

  /* ds-1vn. Prose, not mono like __status: this is something to READ before
     acting on the rows under it, not a machine state. The unverified variant
     stays at __status's soft grey — it reports an absence of information, and
     dressing that as an alert would spend the operator's attention on the
     quieter of the two facts. The stale variant earns the warn surface. */
  .estate-view__snapshot {
    margin: 0;
    padding: 14px 40px 0;
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--ds-fg-soft);
  }
  .estate-view__snapshot--stale {
    margin: 16px 40px 0;
    padding: 10px 14px;
    border: 1px solid var(--ds-warn-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-warn-surface);
    color: var(--ds-warn-ink);
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
  /* The inert PR chip — kept for the malformed-pr_number fallback only, where
     there is no page to send the operator to. Muted and cursorless, because it
     genuinely promises nothing. */
  .estate-view__chip--q {
    border-color: var(--ds-border);
    color: var(--ds-faint);
    cursor: default;
  }
  /* The linked PR chip (ds-wd2.17). Reads as a control — the base chip's navy
     ink, not `--q`'s faint grey, which at 11.5px was quiet enough to scan past
     even before it was clickable.
     `text-decoration: none` because the border already carries the affordance,
     exactly as the sibling Adopt chip does; the two never appear on one row
     (a row either has an open PR or is offerable for adoption), so matching
     their weight creates no competition for the eye.
     No `display` declaration: the chip is a direct child of the row's grid, so
     it is blockified whatever it asks for — an `inline-block` here computes to
     `block` and states a reason ("otherwise the border would overlap the row
     above") for a situation a grid item cannot be in. It read as load-bearing,
     which is worse than absent.
     No `:focus-visible` rule either: base.css's zero-specificity
     `:where(a, button, …)` already rings it, and a local copy would be a second
     definition to keep in step. */
  .estate-view__chip--pr {
    cursor: pointer;
    text-decoration: none;
  }
  /* The house hover for a bordered control (`.ds-btn:hover`, base.css) — a
     quiet surface fill, not an inversion. Navy ink is unchanged, so the hover
     costs no contrast; an inverted navy fill would make this the loudest thing
     on a card whose whole job is to read calmly. */
  .estate-view__chip--pr:hover {
    background: var(--ds-surface-2);
  }

  .estate-view__more {
    margin: 0;
    padding: 12px 40px 6px;
    font-family: var(--ds-font-mono);
    font-size: 11.5px;
    color: var(--ds-faint);
  }

  /* The drift cap's two-way toggle (ds-3em). Copied from `.ledger-strip__more`
     rather than invented: the page now has two "there is more of this" controls
     one card apart, and a second visual language for the same gesture is the
     kind of thing that reads as a bolted-on frontend.
     One difference is load-bearing, and it has to be spent on MARGIN, not
     padding. The ledger's button inherits its 40px horizontal inset from its
     padded parent (`.ledger-strip__rows`); this one's parent is the card
     itself, so it must declare the inset — and padding would leave the BOX
     spanning the card's full width. `.estate-view` is `overflow: hidden` and
     `--ds-ring` is drawn 4px OUTSIDE the box, so that ring was clipped: caught
     by the ds-2fp suite as "cuts left 4.0px", not by inspection. Margin insets
     the box, so the ring has somewhere to land. */
  .estate-view__toggle {
    appearance: none;
    display: block;
    margin: 12px 40px 0;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
    font-family: inherit;
    font-size: 11.5px;
    color: var(--ds-fg-soft);
    text-decoration: underline;
  }
  .estate-view__toggle:hover {
    color: var(--ds-fg);
  }
  .estate-view__toggle:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
    border-radius: var(--ds-radius-sm);
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
