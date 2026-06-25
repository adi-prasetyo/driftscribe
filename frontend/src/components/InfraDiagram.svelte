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
    resourceCards,
    splitCards,
    scopeTotals,
    startHereAssetType,
    type InfraGraph,
    type ResourceCard,
    type ResourceRowStatus,
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

  // Resource cards (card-grid view; design 2026-06-24-infra-resource-cards): one
  // card per group, managed AND drift rows together, drift rows carrying the
  // inline Adopt button. Pure derivation in lib/infra_graph keeps this component
  // thin and the model (row mapping, hidden-unmanaged honesty, drift-first + rank
  // ordering, the each-key by unique assetType) unit-tested in isolation.
  const cards = $derived<ResourceCard[]>(graph ? resourceCards(graph) : []);
  // The top adoptable card gets the "Start here" chip (light-touch guided order:
  // the chip + drift-first ordering replace the dropped hint/order-note prose).
  const startHere = $derived(startHereAssetType(cards));

  // Scope split (design 2026-06-25): PRIMARY cards (adoptable types + anything
  // managed) show by default; the rest fold into a collapsed disclosure. The
  // headline coverage + drift describe the SCOPE, with the project-wide total as
  // muted context — totalResources comes from the authoritative backend total,
  // not the card sums (Codex plan-review MF1).
  const split = $derived(splitCards(cards));
  const scope = $derived(scopeTotals(cards, totals?.resources ?? 0));
  const scopePct = $derived(
    scope.resources > 0 ? coveragePercent(scope.managed, scope.resources) : null,
  );

  // Header pill per card: drift count (warn) / in sync / counts-only (neutral).
  function cardBadge(card: ResourceCard): { text: string; warn: boolean } {
    if (card.sensitive) return { text: 'counts-only', warn: false };
    if (card.drift > 0) return { text: `${card.drift} drift`, warn: true };
    return { text: 'in sync', warn: false };
  }
  // Status-dot tint: managed → ok, drift → warn, control-plane → neutral.
  function dotClass(status: ResourceRowStatus): string {
    return status === 'managed' ? 'ok' : status === 'drift' ? 'drift' : 'hidden';
  }
  // Counts-only line for a sensitive card: "N secrets · hidden" (no name, ever).
  function countsLine(card: ResourceCard): string {
    const word = card.label.toLowerCase();
    return `${card.count} ${card.count === 1 ? word : `${word}s`} · hidden`;
  }

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
      // Mermaid is only used for the preview ghost map. On the normal path the
      // card grid renders reactively from `cards`, so the ~500KB Mermaid bundle
      // is never imported (Codex review 019ef9e9).
      if (open && previewActive) await renderDiagram(body);
    } finally {
      refreshInFlight = false;
      if (myRun === fetchRun) loading = false;
    }
  }

  async function renderDiagram(g: InfraGraph): Promise<void> {
    // Mermaid is preview-only now. Bail (and clear any prior svg) if we are not
    // in preview — this centralizes the invariant so a late call from fetchOverlay
    // racing an exitPreview can't import Mermaid or strand stale svgHtml on the
    // normal path (5-lens adversarial review w4jj7t4a5).
    if (!previewActive) {
      svgHtml = '';
      mermaidLoading = false;
      return;
    }
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
    // Cancel any in-flight fetchOverlay AND any in-flight Mermaid render. A late
    // `overlay = body` would resurrect the banner counts; a straggling
    // renderDiagram continuation could set `error`, restore stale svgHtml, or
    // leave mermaidLoading wedging the Refresh button (Codex review 019ef9e9).
    ++overlayRun;
    ++renderRun;
    previewActive = false;
    overlay = null;
    overlayError = false;
    // Leaving preview drops the Mermaid map; the normal-path card grid takes over.
    svgHtml = '';
    mermaidLoading = false;
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
        {#if scope.drift > 0}
          <span
            class="ds-pill ds-pill--warn"
            data-testid="infra-drift-badge"
            title="Drift in supported resource types">{scope.drift} drift</span
          >
        {:else if scope.resources > 0}
          <span
            class="ds-pill ds-pill--ok"
            data-testid="infra-drift-badge"
            title="In supported resource types">in sync</span
          >
        {:else if totals.resources > 0}
          <!-- Resources exist, but none in a type DriftScribe manages: a green
               "in sync" here would falsely read as "all managed" (Workflow). -->
          <span
            class="ds-pill ds-pill--muted"
            data-testid="infra-drift-badge"
            title="These resources are in types DriftScribe doesn't manage">out of scope</span
          >
        {:else}
          <span class="ds-pill ds-pill--ok" data-testid="infra-drift-badge">in sync</span>
        {/if}
        <span class="infra-summary__count" data-testid="infra-coverage-count"
          >{scope.managed}/{scope.resources} managed{scopePct === null ? '' : ` · ${scopePct}%`}</span
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
        {#if graph && !degraded && scopePct !== null}
          <!-- Coverage is scoped to the resource types DriftScribe manages (the
               primary cards), so the headline isn't dragged to ~2% by Cloud Run
               revisions, container images, and other types nobody puts in IaC. -->
          <!-- "supported" only when there's an out-of-scope set to contrast with
               (the muted note below explains it); otherwise scope == the whole
               estate, so the plain default subject is clearer (Workflow). -->
          <CoverageMeter
            totals={{ resources: scope.resources, managed: scope.managed, drift: scope.drift }}
            subject={scope.otherResources > 0 ? 'your supported infrastructure' : 'your infrastructure'}
          />
          {#if scope.otherResources > 0}
            <!-- otherResources (the disclosure's own sum), NOT outOfScope: the
                 two reconcile only when the backend total equals Σ card counts,
                 so attributing a number to "types DriftScribe doesn't manage"
                 must use what those cards actually total (Codex MF). -->
            <p class="ds-subtle infra-hero__scope-note" data-testid="infra-scope-note">
              {scope.totalResources} total resources indexed · {scope.otherResources} in types
              DriftScribe doesn't manage
            </p>
          {/if}
        {:else if graph && !degraded && scope.totalResources > 0}
          <p class="ds-subtle infra-hero__msg">No resources in supported types yet.</p>
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

    <!-- Zone 2 — legend: the key for the card colors (and, in preview, the ghost
         overlay colors). Placed above the grid/map so it reads as the key for what
         follows. Real a11y content (no aria-hidden); the dot swatches are
         decorative. The single HelpHint sits LAST so its flex-basis:100% panel
         drops onto its own row below the keys. -->
    {#if (graph && !degraded) || previewActive}
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
            ariaLabel="Explain the resource colors"
            testid="legend-help"
          />
        {/if}
      </p>
    {/if}

    <!-- Preview keeps the lazy Mermaid ghost map (its one genuine use — a card
         grid can't draw dashed will-be-created/destroyed nodes); the normal path
         renders the resource card grid, importing no Mermaid. Resource names are
         UNTRUSTED but reach only Svelte text interpolation + the chat input (no
         HTML sink); the Adopt prefill is normalized in lib/infra_graph. -->
    {#if previewActive}
      {#if svgHtml}
        <!-- Mermaid output is sanitized (securityLevel:'strict', htmlLabels:false)
             and every label is entity-escaped upstream in toMermaid. -->
        <div class="infra-diagram" data-testid="infra-diagram">{@html svgHtml}</div>
      {:else if mermaidLoading || loading}
        <p class="ds-subtle">Rendering diagram…</p>
      {:else if graph && !degraded}
        <!-- Preview with no renderable live map AND no ghosts (e.g. a resolved
             overlay over an empty live estate): the banner says "the map below
             shows what is live now", so the honest empty note belongs below it
             rather than a blank gap (5-lens review w4jj7t4a5). -->
        <p class="ds-note" data-testid="infra-empty">No resources indexed yet.</p>
      {/if}
    {:else if graph && !degraded}
      <!-- One card renderer, shared by the in-scope grid and the "Other
           resources" disclosure (a Svelte snippet keeps the two grids identical). -->
      {#snippet cardView(card: ResourceCard)}
        {@const badge = cardBadge(card)}
        <div class="infra-card" data-testid="infra-card">
          <div class="infra-card__head">
            <span class="infra-card__type" data-testid="infra-card-type">{card.label}</span>
            <span class="infra-card__head-meta">
              {#if card.assetType === startHere}
                <span class="infra-card__start" data-testid="card-start-here">Start here</span>
              {/if}
              <span
                class="ds-pill infra-card__badge {badge.warn ? 'ds-pill--warn' : 'ds-pill--muted'}"
                data-testid="infra-card-badge">{badge.text}</span
              >
            </span>
          </div>
          <ul class="infra-card__body">
            {#if card.sensitive}
              <li class="infra-card__counts" data-testid="card-counts-only">
                <span class="infra-dot infra-dot--hidden"></span>{countsLine(card)}
              </li>
            {:else if card.rows.length === 0}
              <!-- Defensive: a non-sensitive type with resources but no sampled
                   nodes (every node truncated). Summarize rather than render a
                   hollow card or collapse the whole grid to "nothing here". -->
              <li class="infra-card__counts" data-testid="card-summary">
                <span class="infra-dot infra-dot--hidden"></span>{card.count}
                {card.label.toLowerCase()}{card.count === 1 ? '' : 's'} · not individually listed
              </li>
            {:else}
              {#each card.rows as row (row.nodeId)}
                <li class="infra-card__row infra-card__row--{row.status}" data-testid="infra-card-row">
                  <span class="infra-dot infra-dot--{dotClass(row.status)}"></span>
                  <span class="infra-card__name">{row.label}</span>
                  {#if row.status === 'managed'}
                    <span class="infra-card__tag infra-card__tag--ok" data-testid="card-managed-tag"
                      >managed</span
                    >
                  {:else if row.adoptable}
                    <button
                      class="ds-btn infra-card__btn"
                      type="button"
                      data-testid="card-adopt-btn"
                      disabled={adoptDisabled}
                      title={adoptDisabled
                        ? 'Unavailable while the chat is busy or reviewing a past trace.'
                        : undefined}
                      onclick={() => clickAdopt(row.prefill)}>Adopt into IaC</button
                    >
                  {:else if row.status === 'control_plane'}
                    <span class="ds-subtle infra-card__muted" data-testid="card-control-plane"
                      >System-managed. The always-on denylist blocks changes and adoption for
                      control-plane resources and for buckets a Google service auto-creates.</span
                    >
                  {:else}
                    <span class="ds-subtle infra-card__muted" data-testid="card-not-adoptable"
                      >not an adoptable type</span
                    >
                  {/if}
                </li>
              {/each}
              {#if card.hiddenUnmanaged > 0}
                <li class="ds-subtle infra-card__trailer" data-testid="card-trailer">
                  +{card.hiddenUnmanaged} more unmanaged {card.label}(s) not shown
                </li>
              {/if}
            {/if}
          </ul>
        </div>
      {/snippet}

      {#if split.primary.length > 0}
        <div class="infra-cards" data-testid="infra-cards">
          {#each split.primary as card (card.assetType)}
            {@render cardView(card)}
          {/each}
        </div>
      {/if}

      {#if split.other.length > 0}
        <!-- Non-adoptable types DriftScribe can't manage (Cloud Run revisions,
             container images, secrets, …): real, but not actionable here. Folded
             into a collapsed disclosure so the default view stays on-scope. -->
        <details class="infra-other" data-testid="infra-other">
          <summary class="infra-other__summary" data-testid="infra-other-summary">
            <span class="infra-other__lead ds-label">Other resources DriftScribe doesn't manage</span>
            <span class="infra-other__meta"
              >{scope.otherTypes} {scope.otherTypes === 1 ? 'type' : 'types'} · {scope.otherResources}
              {scope.otherResources === 1 ? 'resource' : 'resources'}</span
            >
          </summary>
          <div class="infra-cards" data-testid="infra-other-cards">
            {#each split.other as card (card.assetType)}
              {@render cardView(card)}
            {/each}
          </div>
        </details>
      {/if}
      <!-- The empty-estate note lives in the hero band (which always renders a
           state line), so no duplicate is emitted here (Workflow finding). -->
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
  /* Muted project-wide context under the scoped coverage meter: the meter speaks
     to the adoptable scope, this line keeps the full estate honest. */
  .infra-hero__scope-note {
    margin: var(--ds-sp-2) 0 0;
    font-size: var(--ds-fs-1);
    font-variant-numeric: tabular-nums;
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
  /* Round swatches matching the card row dots (visual consistency). */
  .infra-key::before {
    content: '';
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    border: 1px solid var(--ds-border-strong);
  }
  .infra-key--managed::before {
    background: var(--ds-ok-surface);
    border-color: var(--ds-ok);
  }
  .infra-key--drift::before {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn);
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

  /* Status dots — round swatches shared by the card rows and the legend keys.
     managed=ok, drift=warn, control-plane/counts-only=neutral. */
  .infra-dot {
    flex: none;
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    border: 1px solid var(--ds-border-strong);
  }
  .infra-dot--ok {
    background: var(--ds-ok-surface);
    border-color: var(--ds-ok);
  }
  .infra-dot--drift {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn);
  }
  .infra-dot--hidden {
    background: var(--ds-neutral-surface);
  }

  /* Resource card grid (design 2026-06-24-infra-resource-cards): one card per
     resource type, uniform width, 2-col → 1-col responsive. Replaces the Mermaid
     map on the normal path; each card lists its resources with drift rows tinted
     and the inline Adopt affordance. */
  .infra-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(208px, 1fr));
    gap: var(--ds-sp-3);
    margin: var(--ds-sp-3) 0;
  }
  .infra-card {
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    background: var(--ds-surface);
    overflow: hidden;
  }
  .infra-card__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-2) var(--ds-sp-3);
    background: var(--ds-surface-2);
    border-bottom: 1px solid var(--ds-border);
  }
  .infra-card__type {
    /* Grow to push the chip/badge meta to the right edge, and wrap at spaces when
       the title can't share the line with the meta (e.g. "Storage / bucket" next
       to the Start-here chip). NO min-width:0 — that let the title shrink to a
       mid-word break ("Stora ge bucke t"); keeping the longest word as the min
       size wraps cleanly instead. break-word is a safety net for a pathological
       single long word (local visual verify w4jj7t4a5). */
    flex: 1 1 auto;
    overflow-wrap: break-word;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-fg-soft);
  }
  /* Chip + badge grouped on the right of the title; the title grows to push this
     unit right, and it wraps below the title as ONE unit only when crowded. */
  .infra-card__head-meta {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
  }
  .infra-card__badge {
    flex: none;
    font-variant-numeric: tabular-nums;
  }
  /* "Start here" chip — mirrors the ds-pill--ok treatment via the ok tokens. */
  .infra-card__start {
    flex: none;
    padding: 0.05rem 0.5rem;
    border: 1px solid var(--ds-ok);
    border-radius: var(--ds-radius-pill);
    color: var(--ds-ok-ink);
    background: var(--ds-ok-surface);
    font-size: 0.72rem;
    font-weight: var(--ds-fw-semibold);
  }
  /* <ul> — list semantics for AT; reset the default list chrome. */
  .infra-card__body {
    list-style: none;
    margin: 0;
    padding: var(--ds-sp-1) 0;
  }
  .infra-card__row {
    display: flex;
    /* Wrap so a long control-plane note drops to its own line at the 208px
       minimum card width instead of vertically centring the dot + name against a
       tall multi-line note (5-lens review w4jj7t4a5). */
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-2) var(--ds-sp-3);
  }
  /* Drift rows are tinted so the unmanaged resources read at a glance. */
  .infra-card__row--drift {
    background: var(--ds-warn-surface);
  }
  .infra-card__name {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
  }
  .infra-card__tag {
    flex: none;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    white-space: nowrap;
  }
  .infra-card__tag--ok {
    color: var(--ds-ok-ink);
  }
  .infra-card__btn {
    flex: none;
    padding: 0.2em 0.7em;
    font-size: var(--ds-fs-1);
    /* The opaque base ds-btn fixes label legibility, but its --ds-border-strong
       (#d8d7d1) hairline is only ~1.3:1 against the beige drift row (#fcf3dc) —
       below WCAG 1.4.11's 3:1 for the button's own boundary. The --ds-warn border
       (#9a6b00, ~4.2:1 on beige) makes the button shape perceptible AND ties it
       to the drift category (Workflow finding, a11y lens). */
    border-color: var(--ds-warn);
    color: var(--ds-warn-ink);
  }
  .infra-card__btn:hover {
    background: var(--ds-warn-surface);
    border-color: var(--ds-warn);
  }
  .infra-card__muted {
    /* The note takes its own line below the dot + name (flex-basis 100% forces the
       wrap), so dot + name read as the row and the long denylist note sits under
       them instead of vertically centring them against a tall block. */
    flex: 1 1 100%;
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: var(--ds-fs-1);
  }
  .infra-card__counts {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    margin: 0;
    padding: var(--ds-sp-3);
    font-size: var(--ds-fs-2);
    color: var(--ds-muted);
  }
  .infra-card__trailer {
    margin: 0;
    padding: var(--ds-sp-1) var(--ds-sp-3) var(--ds-sp-2);
    font-size: var(--ds-fs-1);
    font-style: italic;
  }

  /* "Other resources" disclosure (design 2026-06-25 scope-split): the
     non-adoptable types DriftScribe can't manage (Cloud Run revisions, container
     images, secrets, …), folded out of the default view. A quiet full-width
     summary row; the revealed grid reuses .infra-cards. */
  .infra-other {
    margin: var(--ds-sp-3) 0 0;
    border-top: 1px dashed var(--ds-border);
  }
  .infra-other__summary {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--ds-sp-1) var(--ds-sp-3);
    padding: var(--ds-sp-3) 0 var(--ds-sp-1);
    cursor: pointer;
    list-style: none;
  }
  .infra-other__summary::-webkit-details-marker {
    display: none;
  }
  .infra-other__lead {
    color: var(--ds-fg-soft);
    flex: 1 1 auto;
  }
  .infra-other__lead::before {
    content: '▸';
    display: inline-block;
    margin-right: var(--ds-sp-2);
    color: var(--ds-faint);
    transition: transform var(--ds-dur-fast) var(--ds-ease);
  }
  .infra-other[open] .infra-other__lead::before {
    transform: rotate(90deg);
  }
  .infra-other__meta {
    flex: none;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }
  .infra-other .infra-cards {
    margin-top: var(--ds-sp-1);
  }
</style>
