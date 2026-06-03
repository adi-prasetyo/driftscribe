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
    type InfraGraph,
  } from '../lib/infra_graph';
  import { RefreshScheduler } from '../lib/infra_refresh';

  let {
    call,
    appliedEpoch = 0,
  }: {
    /** App's token-aware fetch wrapper. */
    call: (path: string, init?: RequestInit) => Promise<Response>;
    /** Bumps when the parent observes a freshly-`applied` iac_apply decision. */
    appliedEpoch?: number;
  } = $props();

  let open = $state(false);
  let graph = $state<InfraGraph | null>(null);
  let loading = $state(false);
  let mermaidLoading = $state(false);
  let error = $state<string | null>(null);
  let svgHtml = $state('');

  // Non-reactive locals. The timer/epoch logic lives in a pure RefreshScheduler
  // (lib/infra_refresh) so it is unit-testable independent of this component; the
  // component keeps only the view + the async fetch/render concurrency guards.
  let fetchRun = 0; // guards refresh() — a stale fetch callback bails
  let renderRun = 0; // guards renderDiagram() — independent of fetchRun
  let mermaidIdSeq = 0; // unique mermaid render id
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let mermaidMod: any = null; // cached after the first lazy import
  const scheduler = new RefreshScheduler({ onFetch: () => void refresh() });

  const degraded = $derived(graph?.degraded ?? false);
  const totals = $derived(graph?.totals ?? null);
  const driftCount = $derived(totals?.drift ?? 0);
  const renderable = $derived(graph ? hasRenderableNodes(graph) : false);

  async function refresh(): Promise<void> {
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
      if (myRun !== fetchRun) return;
      if (!resp.ok) {
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
      if (myRun !== fetchRun) return;
      graph = body;
      if (open) await renderDiagram(body);
    } finally {
      if (myRun === fetchRun) loading = false;
    }
  }

  async function renderDiagram(g: InfraGraph): Promise<void> {
    // Own guard, independent of fetchRun, so a concurrent refresh can't strand
    // mermaidLoading=true (the wedge bug). The latest render owns the flag.
    const myRender = ++renderRun;
    if (g.degraded || !hasRenderableNodes(g)) {
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
      const src = toMermaid(g);
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

  function onToggle(e: Event): void {
    const d = e.currentTarget as HTMLDetailsElement;
    open = d.open;
    if (open) scheduler.open(appliedEpoch);
    else scheduler.close();
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
  // is closed at mount, so this does NOT import mermaid.
  onMount(() => {
    void refresh();
  });
</script>

<details class="ds-card infra-panel" data-testid="infra-panel" ontoggle={onToggle}>
  <summary class="infra-summary" data-testid="infra-toggle">
    <span class="infra-summary__title ds-label">Infrastructure</span>
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
        <span class="infra-summary__count">{totals.managed}/{totals.resources} managed</span>
      {/if}
    </span>
  </summary>

  <div class="infra-body">
    <div class="infra-toolbar">
      <p class="ds-label infra-caption">Resource map · current project</p>
      <button
        class="ds-btn ds-btn--ghost infra-refresh"
        type="button"
        data-testid="infra-refresh"
        onclick={() => void refresh()}
        disabled={loading || mermaidLoading}
      >{loading || mermaidLoading ? 'Refreshing…' : 'Refresh'}</button>
    </div>

    {#if error}
      <p class="ds-blocked" role="alert">{error}</p>
    {/if}

    {#if degraded}
      <p class="ds-note" data-testid="infra-degraded">
        Infrastructure inventory is unavailable right now{graph?.degraded_reason
          ? ` (${graph.degraded_reason})`
          : ''}. Cloud Asset Inventory may still be initializing — try refreshing in a moment.
      </p>
    {:else if graph && !renderable}
      <p class="ds-note" data-testid="infra-empty">No resources indexed yet.</p>
    {:else if svgHtml}
      <!-- Mermaid output is sanitized (securityLevel:'strict', htmlLabels:false)
           and every label is entity-escaped upstream in toMermaid. -->
      <div class="infra-diagram" data-testid="infra-diagram">{@html svgHtml}</div>
    {:else if mermaidLoading || loading}
      <p class="ds-subtle">Rendering diagram…</p>
    {/if}

    {#if graph && !degraded}
      <p class="infra-legend" aria-hidden="true">
        <span class="infra-key infra-key--managed">managed in IaC</span>
        <span class="infra-key infra-key--drift">drift (not in IaC)</span>
        <span class="infra-key infra-key--hidden">counts-only</span>
      </p>
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

  .infra-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-4) 0 var(--ds-sp-3);
  }
  .infra-caption {
    margin: 0;
  }
  .infra-refresh {
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
    gap: var(--ds-sp-2) var(--ds-sp-4);
    margin: var(--ds-sp-3) 0 var(--ds-sp-2);
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

  .infra-freshness {
    margin: 0;
    font-size: var(--ds-fs-1);
    font-style: italic;
  }
</style>
