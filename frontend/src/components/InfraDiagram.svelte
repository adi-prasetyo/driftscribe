<script lang="ts">
  // InfraDiagram — a collapsed "Infrastructure" panel that renders the project's
  // current resources as a Mermaid resource map, colored managed-in-IaC vs drift.
  //
  // Design (docs/plans/2026-06-03-…-design.md §3, §5 Phase 1):
  //  - The CHEAP JSON (GET /infra/graph) is fetched on mount so the collapsed
  //    summary can show a glanceable drift badge without opening the panel.
  //  - The HEAVY part — Mermaid (~500KB) — is `await import()`-ed only on first
  //    expand, so a session that never opens the panel pays 0 KB for it.
  //  - Refresh triggers (the /decisions reload alone is NOT a sufficient signal):
  //    (1) re-fetch on expand, (2) a manual Refresh button, (3) a focus/visibility
  //    refresh while open, (4) light polling while open, and (5) — because CAI is
  //    eventually consistent and lags a just-applied resource — delayed re-fetches
  //    (0/10/30/60s) when the parent signals an `applied` iac_apply (appliedEpoch).
  //
  // Concurrency: fetches and renders have SEPARATE monotonic guards (fetchRun /
  // renderRun) so a fetch never invalidates an in-flight render's own bookkeeping
  // (that bug wedged the Refresh button). Each render owns mermaidLoading and the
  // latest render always clears it.
  //
  // SECURITY: Mermaid is initialized with securityLevel:'strict' + htmlLabels:false,
  // and every label is entity-escaped in lib/infra_graph.toMermaid. Secret types
  // arrive counts-only (no name) from the server.

  import { onMount, untrack } from 'svelte';
  import {
    toMermaid,
    hasRenderableNodes,
    overlayRenderable,
    overlayCountsLine,
    adoptPrefill,
    adoptGroupRank,
    type InfraGraph,
    type InfraGroup,
    type PlanOverlay,
  } from '../lib/infra_graph';
  import { RefreshScheduler } from '../lib/infra_refresh';
  import { coveragePercent } from '../lib/coverage';
  import CoverageMeter from './CoverageMeter.svelte';
  import HelpHint from './HelpHint.svelte';
  import Icon from './Icon.svelte';

  let {
    call,
    appliedEpoch = 0,
    previewPr = null,
    onExitPreview,
    onAdopt,
    adoptDisabled = false,
    onGraph,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
    /** Bumps when the parent observes a freshly-`applied` iac_apply decision. */
    appliedEpoch?: number;
    /** Pending IaC PR to preview (?preview_pr=N), set once at boot. null = no preview. */
    previewPr?: number | null;
    /** Called when the operator clicks "Exit preview" (App removes the URL param). */
    onExitPreview?: () => void;
    /** Adopt click → App prefills the chat with this string (NOT auto-sent). */
    onAdopt?: (prefill: string) => void;
    /**
     * Disable the Adopt buttons — App passes the SAME condition that disables
     * ChatForm (busy / historical replay), so an Adopt click can never silently
     * mutate a disabled input or leave a stale draft behind a historical view
     * (Codex review 019eb572 must-fix 3).
     */
    adoptDisabled?: boolean;
    /**
     * Called with each successfully-applied /infra/graph payload (item 14):
     * App lifts the graph to the onboarding TourCard so the tour reads the
     * SAME data as this panel — no duplicate fetch, no second source of truth.
     */
    onGraph?: (g: InfraGraph) => void;
  } = $props();

  // previewPr is set once at boot (App parses it from the URL) and only ever
  // transitions N → null on exit; capture its boot-time presence non-reactively
  // to arm preview mode and open the panel.
  const previewArmedAtBoot = untrack(() => previewPr != null);
  let previewActive = $state(previewArmedAtBoot);
  let open = $state(previewArmedAtBoot);
  let graph = $state<InfraGraph | null>(null);
  let loading = $state(false);
  let mermaidLoading = $state(false);
  let error = $state<string | null>(null);
  let svgHtml = $state('');

  // Preview overlay state. `overlayError` is the transport/parse failure flag
  // (distinct from an `available:false` overlay, which is a calm "unavailable").
  let overlay = $state<PlanOverlay | null>(null);
  let overlayError = $state(false);

  // Non-reactive locals. The timer/epoch logic lives in a pure RefreshScheduler
  // (lib/infra_refresh) so it is unit-testable independent of this component; the
  // component keeps only the view + the async fetch/render concurrency guards.
  let fetchRun = 0; // guards refresh() — error paths bail when a newer fetch started
  let lastAppliedFetch = 0; // highest run whose RESPONSE was applied (last-applied-wins)
  // Backlog-3 residual (2026-06-12): the describe budget is now 90s and the
  // coordinator runs concurrency=2, so stacked poll/ladder triggers must coalesce
  // — one in-flight /infra/graph request per open panel. Response-application
  // logic (fetchRun/lastAppliedFetch — PR #99 last-applied-wins) unchanged as
  // defense-in-depth.
  let refreshInFlight = false;
  let renderRun = 0; // guards renderDiagram() — independent of fetchRun
  let overlayRun = 0; // guards fetchOverlay() — a THIRD independent guard (grounding fact 5)
  let mermaidIdSeq = 0; // unique mermaid render id
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let mermaidMod: any = null; // cached after the first lazy import
  const scheduler = new RefreshScheduler({ onFetch: () => void refresh() });

  // One inline explanation for the three legend colors (HelpHint ⓘ). Voice
  // matches the de-AI copy pass (PR #144): colons, no em dashes.
  const LEGEND_HELP =
    'Every box is a real resource in your project. Green means managed in IaC: ' +
    'it is defined in OpenTofu, so DriftScribe tracks it and can change it through ' +
    'the approval flow. Yellow means drift: the resource exists but is not in any ' +
    '.tf file, so it is outside management. Grey means counts-only: sensitive types ' +
    'such as secrets, shown as a number with no name.';

  const degraded = $derived(graph?.degraded ?? false);
  const totals = $derived(graph?.totals ?? null);
  const driftCount = $derived(totals?.drift ?? 0);
  const renderable = $derived(graph ? hasRenderableNodes(graph) : false);
  const pct = $derived(totals ? coveragePercent(totals.managed, totals.resources) : null);

  // Adopt list (Phase 4): per-group unmanaged rows + a hidden-unmanaged trailer.
  // Grouped (not the flat adoptRows helper) because the trailer is per-group:
  // `hiddenUnmanaged = max(0, group.drift − unmanaged rows actually shown)`.
  // truncated_in_group counts ALL unsampled resources (managed OR unmanaged), so
  // it must NOT drive the trailer — only this drift-vs-shown delta may, or we'd
  // mislabel hidden MANAGED resources as unmanaged (Codex review 019eb572 round-2).
  type AdoptListRow = {
    nodeId: string;
    label: string;
    adoptable: boolean;
    controlPlane: boolean;
    prefill: string;
  };
  // assetType is the each-key: friendly labels are NOT unique (live CAI carries
  // cloudresourcemanager…/Project AND compute…/Project, both labelled "Project";
  // keying by label crashed the whole panel flush with each_key_duplicate —
  // found by the Phase-4 live e2e).
  type AdoptListGroup = {
    assetType: string;
    label: string;
    rows: AdoptListRow[];
    hiddenUnmanaged: number;
    rank: number | null; // item 10: guided adoption order (null = unranked)
    hint: string | null;
  };
  const adoptGroups = $derived.by((): AdoptListGroup[] => {
    if (!graph || graph.degraded) return [];
    const out: AdoptListGroup[] = [];
    for (const g of graph.groups as InfraGroup[]) {
      if (g.sensitive) continue;
      const adoptable = g.adoptable === true;
      const rows: AdoptListRow[] = [];
      for (const n of g.nodes) {
        if (n.managed) continue;
        const controlPlane = n.control_plane === true;
        const rowAdoptable = adoptable && !controlPlane;
        rows.push({
          nodeId: n.id,
          label: n.label,
          adoptable: rowAdoptable,
          controlPlane,
          prefill: rowAdoptable ? adoptPrefill(g.label, n.label, n.location) : '',
        });
      }
      if (rows.length === 0) continue;
      const rank = adoptGroupRank(g);
      out.push({
        assetType: g.asset_type,
        label: g.label,
        rows,
        hiddenUnmanaged: Math.max(0, g.drift - rows.length),
        rank,
        hint:
          rank !== null && typeof g.adopt_hint === 'string' && g.adopt_hint
            ? g.adopt_hint
            : null,
      });
    }
    // JS sort is stable, so unranked groups keep their server order.
    out.sort(
      (a, b) =>
        (a.rank ?? Number.POSITIVE_INFINITY) - (b.rank ?? Number.POSITIVE_INFINITY),
    );
    return out;
  });
  const hasAdoptRows = $derived(adoptGroups.length > 0);
  // The count shown on the adopt-zone header. Sum of the named rows actually
  // rendered PLUS each group's "+N more" trailer, so it is provably equal to
  // everything the zone accounts for (a reader can add the visible rows and the
  // trailer numbers and land on exactly this badge). It diverges below
  // totals.drift in two cases, in both of which it still equals exactly what the
  // zone renders: (1) totals.drift counts sensitive / counts-only types (e.g.
  // secrets) this section deliberately does not list; (2) a non-sensitive group
  // whose g.drift > 0 but whose sampled nodes are all managed contributes zero
  // rows and no trailer, so it is excluded here. The hero's "not yet in IaC" line
  // carries the global drift number in either case.
  const adoptShownTotal = $derived(
    adoptGroups.reduce((acc, g) => acc + g.rows.length + g.hiddenUnmanaged, 0),
  );
  // First group that is ranked AND still has a clickable Adopt row — a ranked
  // group whose every shown row is control-plane (denylist-refused) must not
  // claim "Start here": the chip would sit on a group with no button. Ranked
  // groups sort first, so the scan walks the guide order.
  const startHereAssetType = $derived(
    adoptGroups.find((g) => g.rank != null && g.rows.some((r) => r.adoptable))
      ?.assetType ?? null,
  );

  function clickAdopt(prefill: string): void {
    if (adoptDisabled) return;
    onAdopt?.(prefill);
  }

  // The overlay actually drawn (only when preview is active AND it has ghosts).
  // A plain function (not a $derived) so renderDiagram can read it across an
  // await boundary without tripping `derived_inert` on a torn-down component.
  function activeOverlay(): PlanOverlay | null {
    return previewActive && overlayRenderable(overlay) ? overlay : null;
  }

  async function refresh(): Promise<void> {
    if (refreshInFlight) return;
    refreshInFlight = true;
    const myRun = ++fetchRun;
    loading = true;
    error = null;
    try {
      let resp: Response;
      try {
        resp = await call('/infra/graph');
      } catch {
        if (myRun !== fetchRun) return;
        error = 'Could not reach the coordinator.';
        return;
      }
      if (myRun !== fetchRun && resp.ok) {
        // A newer fetch has STARTED but a 200 in hand is still fresher than
        // anything applied so far — fall through to the apply check below.
        // (Discarding here livelocked the panel in prod: the boot-time
        // applied-epoch ladder fires fetches every 10-30s while a cold
        // CAI-backed /infra/graph takes 10-30s to answer, so EVERY response
        // arrived "stale", graph never set, and the panel spun on
        // "Refreshing…" forever — found by the Phase-4 live e2e.)
      } else if (myRun !== fetchRun) {
        return;
      } else if (!resp.ok) {
        error = `Request failed (${resp.status}).`;
        return;
      }
      let body: InfraGraph;
      try {
        body = (await resp.json()) as InfraGraph;
      } catch {
        if (myRun !== fetchRun) return;
        error = 'Malformed response.';
        return;
      }
      // Last-APPLIED-wins (not last-started-wins): apply this response unless a
      // NEWER fetch's response already applied. The first completion always
      // lands (no livelock); an out-of-order older completion still can't
      // overwrite a newer applied graph.
      if (myRun <= lastAppliedFetch) return;
      lastAppliedFetch = myRun;
      graph = body;
      error = null;
      onGraph?.(body);
      if (open) await renderDiagram(body);
    } finally {
      refreshInFlight = false;
      if (myRun === fetchRun) loading = false;
    }
  }

  async function renderDiagram(g: InfraGraph): Promise<void> {
    // Own guard, independent of fetchRun, so a concurrent refresh can't strand
    // mermaidLoading=true (the wedge bug). The latest render owns the flag.
    const myRender = ++renderRun;
    // An available overlay with ghosts keeps the diagram alive even when the
    // live graph is degraded/empty (a CAI outage must not blind the preview).
    const ov = activeOverlay();
    if ((g.degraded || !hasRenderableNodes(g)) && !overlayRenderable(ov)) {
      svgHtml = '';
      mermaidLoading = false;
      return;
    }
    mermaidLoading = true;
    try {
      if (!mermaidMod) {
        const mod = await import('mermaid');
        if (myRender !== renderRun) return;
        mermaidMod = mod.default;
        mermaidMod.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'neutral',
          flowchart: { htmlLabels: false },
        });
      }
      const src = toMermaid(g, ov ?? undefined);
      const { svg } = await mermaidMod.render(`infra-mmd-${++mermaidIdSeq}`, src);
      if (myRender !== renderRun) return;
      svgHtml = svg;
    } catch {
      if (myRender === renderRun) {
        svgHtml = '';
        error = 'Could not render the diagram.';
      }
    } finally {
      if (myRender === renderRun) mermaidLoading = false;
    }
  }

  // Fetch the preview overlay for `previewPr`. Its OWN monotonic guard
  // (overlayRun) — never reuse fetchRun/renderRun. Called ONLY from
  // onMount-activation / Refresh / Retry — NEVER from any RefreshScheduler path
  // (focus/poll/applied-ladder) so the expensive route never gets polled.
  async function fetchOverlay(): Promise<void> {
    if (previewPr == null) return;
    const myRun = ++overlayRun;
    overlayError = false;
    try {
      let resp: Response;
      try {
        resp = await call(`/infra/graph/preview?pr=${previewPr}`);
      } catch {
        if (myRun !== overlayRun) return;
        overlay = null;
        overlayError = true;
        return;
      }
      if (myRun !== overlayRun) return;
      if (!resp.ok) {
        overlay = null;
        overlayError = true;
        return;
      }
      let body: PlanOverlay;
      try {
        body = (await resp.json()) as PlanOverlay;
      } catch {
        if (myRun !== overlayRun) return;
        overlay = null;
        overlayError = true;
        return;
      }
      if (myRun !== overlayRun) return;
      overlay = body;
      // An overlay arriving while open re-composes the (possibly ghost-only) map.
      if (open && graph) await renderDiagram(graph);
    } finally {
      /* overlayError/overlay set above; no shared loading flag */
    }
  }

  function exitPreview(): void {
    // Cancel any in-flight fetchOverlay — its write-back must not survive exit
    // (a late `overlay = body` would resurrect the banner counts and re-render).
    ++overlayRun;
    previewActive = false;
    overlay = null;
    overlayError = false;
    // Re-render without ghosts when the panel is open.
    if (open && graph) void renderDiagram(graph);
    onExitPreview?.();
  }

  function onToggle(e: Event): void {
    const d = e.currentTarget as HTMLDetailsElement;
    open = d.open;
    if (open) scheduler.open(appliedEpoch);
    else scheduler.close();
    // NOTE: the overlay is NEVER fetched here — only from onMount/Refresh/Retry
    // (Decision 6) — so a browser-fired toggle on the initial mount can't
    // double-fetch the expensive preview route.
  }

  // The Refresh button refreshes the cheap graph AND, while preview is active,
  // re-fetches the overlay (an explicit operator intent — Decision 6).
  function manualRefresh(): void {
    void refresh();
    if (previewActive) void fetchOverlay();
  }

  // Hand each appliedEpoch change to the scheduler: while OPEN it rides out the
  // 0/10/30/60s ladder; while CLOSED it defers so the next expand rides it out.
  // Tracks ONLY appliedEpoch (the scheduler holds open/lastHandledEpoch state).
  $effect(() => {
    const epoch = appliedEpoch;
    untrack(() => scheduler.onAppliedEpoch(epoch));
  });

  // Refresh when the operator returns to the tab (covers an apply approved in
  // another tab). Registered once; tears down all scheduler timers on destroy.
  $effect(() => {
    function onFocus(): void {
      if (document.visibilityState === 'visible') scheduler.onFocus();
    }
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
      scheduler.destroy();
    };
  });

  // Cheap JSON on mount → powers the glanceable badge while collapsed. The panel
  // is closed at mount (no preview), so this does NOT import mermaid.
  //
  // When previewPr is set the panel renders OPEN (initial `open`), but the
  // browser may not fire a `toggle` for the initial open attribute — so onMount
  // must itself (a) call scheduler.open(appliedEpoch) so the focus/poll/applied
  // refresh machinery runs for an initially-open panel, and (b) fetch the
  // overlay exactly once. Overlay fetches are excluded from onToggle, so a
  // browser-fired toggle on the same mount cannot double-fetch the preview.
  onMount(() => {
    void refresh();
    if (previewPr != null) {
      scheduler.open(appliedEpoch);
      void fetchOverlay();
    }
  });
</script>

<details class="ds-card infra-panel" data-testid="infra-panel" {open} ontoggle={onToggle}>
  <summary class="infra-summary" data-testid="infra-toggle">
    <span class="infra-summary__title ds-label"><Icon name="boxes" size={14} extraClass="infra-eyebrow-icon" />Infrastructure</span>
    <span class="infra-summary__badges">
      {#if loading && !graph}
        <span class="ds-pill ds-pill--muted">loading…</span>
      {:else if degraded}
        <span class="ds-pill ds-pill--muted" data-testid="infra-drift-badge">unavailable</span>
      {:else if totals}
        {#if driftCount > 0}
          <span class="ds-pill ds-pill--warn" data-testid="infra-drift-badge">{driftCount} drift</span>
        {:else}
          <span class="ds-pill ds-pill--ok" data-testid="infra-drift-badge">in sync</span>
        {/if}
        <span class="infra-summary__count" data-testid="infra-coverage-count"
          >{totals.managed}/{totals.resources} managed{pct === null ? '' : ` · ${pct}%`}</span
        >
      {/if}
    </span>
  </summary>

  <div class="infra-body">
    {#if previewActive}
      <div class="infra-preview" data-testid="preview-banner" role="status">
        <div class="infra-preview__text">
          <p class="infra-preview__lead">
            Previewing PR #{previewPr}. Dashed nodes show what approving this change
            would do. The live map does not change until the change is applied.
          </p>
          {#if overlay?.available}
            <p class="ds-subtle infra-preview__counts" data-testid="preview-counts">
              {overlayCountsLine(overlay.counts)}{overlay.hidden > 0
                ? ` · +${overlay.hidden} more not shown`
                : ''}
            </p>
          {/if}
        </div>
        <button
          class="ds-btn ds-btn--ghost infra-preview__exit"
          type="button"
          data-testid="preview-exit"
          onclick={exitPreview}>Exit preview</button
        >
      </div>

      {#if overlayError}
        <p class="ds-note" data-testid="preview-error">
          Could not load the change preview.
          <button
            class="ds-btn ds-btn--ghost infra-preview__retry"
            type="button"
            data-testid="preview-retry"
            onclick={() => void fetchOverlay()}>Retry</button
          >
        </p>
      {:else if overlay && !overlay.available}
        <p class="ds-note" data-testid="preview-unavailable">
          {#if overlay.reason === 'no_plan'}
            No pending plan was found for PR #{previewPr}. Nothing to preview.
          {:else if overlay.reason === 'artifact_error'}
            The plan for PR #{previewPr} could not be verified, so it cannot be previewed.
            Open the approval page for details.
          {:else if overlay.reason === 'resolved'}
            PR #{previewPr} has already reached a final outcome. The map below shows
            what is live now.
          {:else}
            This plan could not be summarized into a preview. Review the approval page
            instead.
          {/if}
        </p>
      {/if}
    {/if}

    <!-- Zone 1 — hero band: the coverage headline (or degraded / loading state)
         is the panel's lead stat; Refresh always lives here so it is reachable
         in every state. The degraded note lives INSIDE the hero (it replaces the
         coverage meter) yet the diagram region below stays independent, so a
         ghost-only preview can still render under a degraded live graph. -->
    <div class="infra-hero" data-testid="infra-hero">
      <div class="infra-hero__main">
        {#if graph && !degraded && pct !== null}
          <CoverageMeter {totals} />
        {:else if graph && !degraded}
          <p class="ds-subtle infra-hero__msg">No resources indexed yet.</p>
        {:else if degraded}
          <!-- A plain muted line (NOT .ds-note): inside the framed hero a
               boxed callout would read as a frame-within-a-frame. -->
          <p class="ds-subtle infra-hero__msg" data-testid="infra-degraded">
            Infrastructure inventory is unavailable right now{graph?.degraded_reason
              ? ` (${graph.degraded_reason})`
              : ''}. Cloud Asset Inventory may still be initializing. Try refreshing in a moment.
          </p>
        {:else if loading}
          <p class="ds-subtle infra-hero__msg">Loading inventory…</p>
        {:else}
          <!-- Fetch failed before any graph arrived: keep the framed hero from
               rendering hollow (the error alert itself shows below). -->
          <p class="ds-subtle infra-hero__msg">Inventory unavailable.</p>
        {/if}
      </div>
      <button
        class="ds-btn ds-btn--ghost infra-refresh"
        type="button"
        data-testid="infra-refresh"
        onclick={manualRefresh}
        disabled={loading || mermaidLoading}
      >{loading || mermaidLoading ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {#if error}
      <p class="ds-blocked" role="alert">{error}</p>
    {/if}

    <!-- Diagram region — independent of the degraded note. -->
    {#if svgHtml}
      <!-- Mermaid output is sanitized (securityLevel:'strict', htmlLabels:false)
           and every label is entity-escaped upstream in toMermaid. -->
      <div class="infra-diagram" data-testid="infra-diagram">{@html svgHtml}</div>
    {:else if mermaidLoading || loading}
      <p class="ds-subtle">Rendering diagram…</p>
    {:else if graph && !degraded && !renderable}
      <p class="ds-note" data-testid="infra-empty">No resources indexed yet.</p>
    {/if}

    {#if (graph && !degraded) || previewActive}
      <!-- Legend is now real a11y content (no aria-hidden): the text labels carry
           the meaning, the ::before swatches are decorative. The single HelpHint
           sits LAST so its inline flex-basis:100% panel drops onto its own row
           below all the keys rather than splitting them. -->
      <p class="infra-legend" data-testid="infra-legend">
        <span class="infra-legend__lead ds-label">Legend</span>
        {#if graph && !degraded}
          <span class="infra-key infra-key--managed">managed in IaC</span>
          <span class="infra-key infra-key--drift">drift (not in IaC)</span>
          <span class="infra-key infra-key--hidden">counts-only</span>
        {/if}
        {#if previewActive}
          <span class="infra-key infra-key--ghost-create">will be created</span>
          <span class="infra-key infra-key--ghost-update">will be modified</span>
          <span class="infra-key infra-key--ghost-destroy">will be destroyed</span>
        {/if}
        {#if graph && !degraded}
          <HelpHint
            text={LEGEND_HELP}
            ariaLabel="Explain the resource map colors"
            testid="legend-help"
          />
        {/if}
      </p>
    {/if}

    <!-- Adopt list (Phase 4 — adopt button UI). The map's Mermaid SVG is strict /
         htmlLabels:false, so the Adopt affordance can't be an in-SVG click target;
         it's this DOM action list, derived from the graph DTO. Names are UNTRUSTED
         but reach only Svelte text interpolation + a text input (no HTML sink); the
         prefill is normalized for the prompt path in lib/infra_graph. Codex 019eb572. -->
    {#if graph && !degraded && hasAdoptRows}
      <div class="infra-adopt" data-testid="adopt-list">
        <div class="infra-adopt__head">
          <span class="ds-label infra-adopt__title">Unmanaged resources</span>
          <span
            class="ds-pill ds-pill--muted infra-adopt__count"
            data-testid="adopt-count"
            aria-label={`${adoptShownTotal} unmanaged ${adoptShownTotal === 1 ? 'resource' : 'resources'}`}
            >{adoptShownTotal}</span
          >
        </div>
        <p class="ds-subtle infra-adopt__heading">
          These exist in your project but are not under IaC management.
        </p>
        {#if startHereAssetType !== null}
          <p class="ds-subtle infra-adopt__order" data-testid="adopt-order-note">
            Suggested order among the unmanaged resources shown: the simplest to
            recognize and review come first. Every adoption is the same zero-change
            import behind the same approval gate. The order is about building
            confidence, not safety.
          </p>
        {/if}
        <ul class="infra-adopt__list">
          {#each adoptGroups as g (g.assetType)}
            {#if g.hint !== null}
              <li class="ds-subtle infra-adopt__hint" data-testid="adopt-hint">
                {#if g.assetType === startHereAssetType}
                  <span class="infra-adopt__start" data-testid="adopt-start-here">Start here</span>
                {/if}
                {g.label}: {g.hint}
              </li>
            {/if}
            {#each g.rows as row (row.nodeId)}
              <li class="infra-adopt__row" data-testid="adopt-row">
                <span class="infra-adopt__type">{g.label}</span>
                <span class="infra-adopt__name">{row.label}</span>
                {#if row.adoptable}
                  <button
                    class="ds-btn ds-btn--ghost infra-adopt__btn"
                    type="button"
                    data-testid="adopt-btn"
                    disabled={adoptDisabled}
                    title={adoptDisabled
                      ? 'Unavailable while the chat is busy or reviewing a past trace.'
                      : undefined}
                    onclick={() => clickAdopt(row.prefill)}>Adopt into IaC</button
                  >
                {:else if row.controlPlane}
                  <span class="ds-subtle infra-adopt__muted" data-testid="adopt-control-plane"
                    >System-managed infrastructure (DriftScribe's own control-plane
                    resources, or a bucket a Google service auto-creates). The
                    always-on denylist blocks changes and adoption for these.</span
                  >
                {:else}
                  <span class="ds-subtle infra-adopt__muted" data-testid="adopt-unavailable"
                    >not an adoptable type</span
                  >
                {/if}
              </li>
            {/each}
            {#if g.hiddenUnmanaged > 0}
              <li class="ds-subtle infra-adopt__trailer" data-testid="adopt-trailer">
                +{g.hiddenUnmanaged} more unmanaged {g.label}(s) not on the map
              </li>
            {/if}
          {/each}
        </ul>
      </div>
    {/if}

    {#if graph && !degraded}
      <p class="ds-subtle infra-freshness">{graph.caveat}</p>
    {/if}
  </div>
</details>

<style>
  .infra-panel {
    padding: 0; /* the summary + body own their own padding */
  }

  .infra-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-4) var(--ds-sp-5);
    cursor: pointer;
    list-style: none;
  }
  .infra-summary::-webkit-details-marker {
    display: none;
  }
  /* a small disclosure caret that rotates when open */
  /* Eyebrow tint: title text bumped to fg-soft, icon stays at muted (§6) */
  .infra-summary__title {
    color: var(--ds-fg-soft);
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .infra-summary__title :global(.infra-eyebrow-icon) {
    color: var(--ds-muted);
  }
  .infra-summary__title::before {
    content: '▸';
    display: inline-block;
    margin-right: var(--ds-sp-2);
    color: var(--ds-faint);
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .infra-panel[open] .infra-summary__title::before {
    transform: rotate(90deg);
  }

  .infra-summary__badges {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }
  .infra-summary__count {
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .infra-body {
    padding: 0 var(--ds-sp-5) var(--ds-sp-5);
    border-top: 1px solid var(--ds-border);
  }

  /* Preview banner — a calm informational block at the top of the body. */
  .infra-preview {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    margin: var(--ds-sp-4) 0 0;
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
  }
  .infra-preview__text {
    min-width: 0;
  }
  .infra-preview__lead {
    margin: 0;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg-soft);
  }
  .infra-preview__counts {
    margin: var(--ds-sp-1) 0 0;
    font-variant-numeric: tabular-nums;
  }
  .infra-preview__exit {
    flex: none;
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
  }
  .infra-preview__retry {
    padding: 0.15em 0.6em;
    font-size: var(--ds-fs-1);
    margin-left: var(--ds-sp-2);
  }

  /* Zone 1 — hero band. A framed lead-stat header: the coverage meter (or the
     degraded / loading state) on the left, Refresh pinned top-right. The frame
     (surface-2 + border) gives the headline its own weight and visually brackets
     the map together with the matching adopt zone below. */
  .infra-hero {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    margin: var(--ds-sp-4) 0 var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
  }
  .infra-hero__main {
    flex: 1 1 auto;
    min-width: 0;
  }
  /* The meter owns the hero's left column; drop its trailing margin so the band
     hugs its content (the band's own padding frames it). */
  .infra-hero__main :global(.coverage) {
    margin-bottom: 0;
  }
  /* All hero text-states (loading / zero-resource / degraded / error) share this
     plain muted line so they read consistently inside the framed band. */
  .infra-hero__msg {
    margin: 0;
  }
  .infra-refresh {
    flex: none;
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
  }

  .infra-diagram {
    overflow-x: auto;
    padding: var(--ds-sp-3) 0;
  }
  .infra-diagram :global(svg) {
    max-width: 100%;
    height: auto;
  }

  .infra-legend {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2) var(--ds-sp-4);
    margin: var(--ds-sp-3) 0 var(--ds-sp-2);
  }
  .infra-legend__lead {
    color: var(--ds-muted);
  }
  .infra-key {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .infra-key::before {
    content: '';
    width: 0.75rem;
    height: 0.75rem;
    border-radius: var(--ds-radius-sm);
    border: 1px solid var(--ds-border-strong);
  }
  .infra-key--managed::before {
    background: var(--ds-ok-surface);
    border-color: var(--ds-ok-border);
  }
  .infra-key--drift::before {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn-border);
  }
  .infra-key--hidden::before {
    background: var(--ds-neutral-surface);
  }
  /* Ghost (preview) keys — dashed swatches, bare design tokens (no fallback hex). */
  .infra-key--ghost-create::before {
    background: var(--ds-ok-surface);
    border: 1px dashed var(--ds-ok-border);
  }
  .infra-key--ghost-update::before {
    background: var(--ds-warn-surface);
    border: 1px dashed var(--ds-warn-border);
  }
  .infra-key--ghost-destroy::before {
    background: var(--ds-danger-surface);
    border: 1px dashed var(--ds-danger-border);
  }

  .infra-freshness {
    margin: 0;
    font-size: var(--ds-fs-1);
    font-style: italic;
  }

  /* Zone 3 — adopt zone. Framed to match the hero (surface-2 + border), so the
     two weighted zones bracket the map and the "what is not managed yet" action
     block reads as a distinct, deliberate section rather than a trailing list. */
  .infra-adopt {
    margin: var(--ds-sp-2) 0 var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
  }
  .infra-adopt__head {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    margin-bottom: var(--ds-sp-1);
  }
  .infra-adopt__title {
    color: var(--ds-fg-soft);
  }
  .infra-adopt__count {
    font-variant-numeric: tabular-nums;
  }
  .infra-adopt__heading {
    margin: 0 0 var(--ds-sp-2);
    color: var(--ds-muted);
  }
  .infra-adopt__list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-1);
  }
  .infra-adopt__row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-1) 0;
  }
  .infra-adopt__type {
    flex: 0 0 auto;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
  }
  .infra-adopt__name {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg-soft);
  }
  .infra-adopt__btn {
    flex: 0 0 auto;
    padding: 0.25em 0.75em;
    font-size: var(--ds-fs-1);
  }
  .infra-adopt__muted {
    /* Shrink + wrap: the control-plane note is long, and the framed adopt zone's
       padding leaves little room on narrow widths (Codex review). */
    flex: 1 1 14rem;
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--ds-fs-1);
  }
  .infra-adopt__trailer {
    font-size: var(--ds-fs-1);
    font-style: italic;
  }
  /* Guided adoption order (item 10): the order note + per-group hint lines + the
     "Start here" chip. The chip mirrors the ds-pill--ok treatment via the shared
     ok design tokens (--ds-ok / --ds-ok-ink / --ds-ok-surface). */
  .infra-adopt__order {
    margin: 0 0 0.4rem;
  }
  .infra-adopt__hint {
    list-style: none;
    margin-top: 0.45rem;
  }
  .infra-adopt__start {
    display: inline-block;
    margin-right: 0.45rem;
    padding: 0.05rem 0.5rem;
    border: 1px solid var(--ds-ok);
    border-radius: 999px;
    color: var(--ds-ok-ink);
    background: var(--ds-ok-surface);
    font-size: 0.72rem;
    font-weight: 600;
  }
</style>
