import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import mermaid from 'mermaid';
import InfraDiagram from '../../src/components/InfraDiagram.svelte';
import type { InfraGraph, PlanOverlay } from '../../src/lib/infra_graph';

// Renders InfraDiagram with a stubbed `call` prop (the component's only data
// dependency). The COVERAGE tests never open the panel, so Mermaid is never
// imported: <details> content is in the DOM even while closed, which lets us
// assert on the body without paying for the diagram.
//
// The PREVIEW (ghost-node) tests below DO open the panel (previewPr set at
// mount), so the component lazy-imports Mermaid. We stub the REAL module's
// methods with vi.spyOn rather than vi.mock(factory): the component reaches
// mermaid via a runtime dynamic import, and vitest's module-mock registry does
// NOT reliably survive across tests for lazy dynamic imports (observed: the
// 2nd test's import resolved to the real module, whose render never settles
// under jsdom). Spying on the real, stably-cached module instance intercepts
// every import — static or dynamic, any test, any component instance. The
// renderSpy records (id, src) so tests can assert on the composed Mermaid
// source; the lib-level mermaid.parse tests (infra_graph.test.ts) prove that
// source is valid grammar.
const renderSpy = vi
  .spyOn(mermaid, 'render')
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  .mockImplementation(async (id: string) => ({ svg: `<svg data-id="${id}"></svg>` }) as any);
vi.spyOn(mermaid, 'initialize').mockImplementation(() => {});

afterEach(cleanup);
// Clears call HISTORY only (implementations survive) — per-test call counts.
beforeEach(() => vi.clearAllMocks());

function graphWith(totals: InfraGraph['totals'], degraded = false): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded,
    degraded_reason: degraded ? 'cai_unavailable' : null,
    totals,
    groups: [],
    edges: [],
  };
}

function callWith(graph: InfraGraph, paths: string[] = []): (path: string) => Promise<Response> {
  return async (path: string) => {
    paths.push(path);
    return new Response(JSON.stringify(graph), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
}

describe('InfraDiagram — coverage treatment', () => {
  it('shows the percentage in the collapsed summary count and the meter in the body', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 50, managed: 13, drift: 37 }), paths) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-coverage-count').textContent).toBe('13/50 managed · 26%');
    });
    expect(paths).toContain('/infra/graph');
    expect(getByTestId('coverage-meter')).toBeTruthy();
    expect(getByTestId('coverage-pct').textContent).toBe('26%');
  });

  it('keeps the plain count (no percentage, no meter) for a zero-resource estate', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 0, managed: 0, drift: 0 })) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-coverage-count').textContent).toBe('0/0 managed');
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('shows no meter when the graph is degraded', async () => {
    // non-zero totals: suppression must come from the degraded branch, not an empty estate
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 5, managed: 3, drift: 2 }, true)) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-drift-badge').textContent).toBe('unavailable');
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
    expect(queryByTestId('infra-coverage-count')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const RUN = 'run.googleapis.com/Service';
const TOPIC = 'pubsub.googleapis.com/Topic';

function liveGraph(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 2, managed: 1, drift: 1 },
    groups: [
      {
        asset_type: TOPIC,
        label: 'Pub/Sub topic',
        count: 1,
        managed: 1,
        drift: 0,
        sensitive: false,
        nodes: [{ id: 'g0n0', label: 'drift-events', asset_type: TOPIC, managed: true, location: null }],
      },
      {
        asset_type: RUN,
        label: 'Cloud Run service',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 'g1n0', label: 'storefront', asset_type: RUN, managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

function degradedGraph(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: true,
    degraded_reason: 'cai_unavailable',
    totals: { resources: 0, managed: 0, drift: 0 },
    groups: [],
    edges: [],
  };
}

function overlay(p: Partial<PlanOverlay> = {}): PlanOverlay {
  return {
    pr_number: 47,
    available: true,
    reason: null,
    counts: { create: 1, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 },
    hidden: 0,
    entries: [
      {
        verb: 'create',
        rtype: 'google_pubsub_topic',
        type_label: 'Pub/Sub topic',
        name: 'order-events',
        address: 'google_pubsub_topic.order_events',
        asset_type: TOPIC,
        sensitive: false,
        location: 'asia-northeast1',
      },
    ],
    ...p,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/**
 * A call stub that records every path and answers /infra/graph with the live
 * graph and /infra/graph/preview with a supplied overlay (or a transport
 * rejection). `previewBehavior` may be an overlay object or 'reject'.
 */
function makeCall(
  paths: string[],
  graph: InfraGraph,
  previewBehavior: PlanOverlay | 'reject' | (() => PlanOverlay | 'reject'),
): (path: string, init?: RequestInit) => Promise<Response> {
  return async (path: string) => {
    paths.push(path);
    if (path.startsWith('/infra/graph/preview')) {
      const b = typeof previewBehavior === 'function' ? previewBehavior() : previewBehavior;
      if (b === 'reject') throw new Error('network down');
      return jsonResponse(b);
    }
    return jsonResponse(graph);
  };
}

const PREVIEW = '/infra/graph/preview?pr=47';
const LINE1 =
  'Previewing PR #47. Dashed nodes show what approving this change would do. The live map does not change until the change is applied.';

/** Collapse template whitespace (multi-line copy renders with newlines/indent). */
function norm(s: string | null): string {
  return (s ?? '').replace(/\s+/g, ' ').trim();
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('InfraDiagram — preview mode activation', () => {
  it('opens the panel, fetches the overlay exactly once, and shows the banner + counts', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()), previewPr: 47 },
    });
    await waitFor(() => {
      expect(getByTestId('preview-banner')).toBeTruthy();
    });
    // <details open>
    const panel = getByTestId('infra-panel') as HTMLDetailsElement;
    expect(panel.open).toBe(true);
    // exact line-1 copy
    expect(norm(getByTestId('preview-banner').textContent)).toContain(LINE1);
    // counts line from a 1-create overlay
    await waitFor(() => {
      expect(getByTestId('preview-counts').textContent).toContain('1 will be created');
    });
    // exactly ONE preview fetch at mount
    const previewFetches = paths.filter((p) => p.startsWith('/infra/graph/preview'));
    expect(previewFetches).toEqual([PREVIEW]);
    // Drain this component's render chain before the test ends so its pending
    // mermaid renders can't straggle into a later test's call counts.
    await waitFor(() => expect(getByTestId('infra-diagram')).toBeTruthy());
  });

  it('renders +N more not shown when hidden > 0', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay({ hidden: 3 })), previewPr: 47 },
    });
    await waitFor(() => {
      expect(getByTestId('preview-counts').textContent).toContain('+3 more not shown');
    });
    // Drain (see above).
    await waitFor(() => expect(getByTestId('infra-diagram')).toBeTruthy());
  });
});

describe('InfraDiagram — exit preview', () => {
  it('clears the banner + the ghost map, calls onExitPreview once, and shows the card grid', async () => {
    const paths: string[] = [];
    const onExitPreview = vi.fn();
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()), previewPr: 47, onExitPreview },
    });
    // Synchronize on THIS component's completed ghost render before clicking: the
    // ghost map is in the DOM and the LATEST composed Mermaid src carries a ghost
    // class (guards against a vacuous pass; the banner renders before any fetch).
    await waitFor(() => expect(queryByTestId('infra-diagram')).toBeTruthy());
    await waitFor(() => {
      const calls = renderSpy.mock.calls;
      expect(calls.length).toBeGreaterThan(0);
      expect(calls[calls.length - 1][1]).toContain('ghost');
    });

    await fireEvent.click(getByTestId('preview-exit'));
    await waitFor(() => {
      expect(queryByTestId('preview-banner')).toBeNull();
    });
    expect(onExitPreview).toHaveBeenCalledTimes(1);
    // Exiting preview clears the Mermaid map (no re-render) and the normal-path
    // card grid takes over for the live graph.
    await waitFor(() => expect(queryByTestId('infra-diagram')).toBeNull());
    expect(getByTestId('infra-cards')).toBeTruthy();
  });

  // Pins exitPreview's `++overlayRun;`: an overlay fetch still in flight at
  // exit must not write back (a late `overlay = body` would resurrect the
  // banner counts and re-render ghosts). Deferred-resolver idiom mirrors
  // PauseControl's stale-clobber test.
  it('an in-flight overlay fetch resolving AFTER exit never lands', async () => {
    let resolveOverlay!: (r: Response) => void;
    const gatedOverlay = new Promise<Response>((res) => {
      resolveOverlay = res;
    });
    const paths: string[] = [];
    const call = async (path: string): Promise<Response> => {
      paths.push(path);
      if (path.startsWith('/infra/graph/preview')) return gatedOverlay;
      return jsonResponse(liveGraph());
    };
    const onExitPreview = vi.fn();
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call, previewPr: 47, onExitPreview },
    });
    // The live graph renders while the overlay fetch is still pending.
    await waitFor(() => expect(queryByTestId('infra-diagram')).toBeTruthy());
    expect(paths).toContain(PREVIEW); // overlay fetch IS in flight
    expect(queryByTestId('preview-counts')).toBeNull(); // ...but not landed

    // Exit while the overlay fetch is in flight, THEN let it resolve.
    await fireEvent.click(getByTestId('preview-exit'));
    await waitFor(() => expect(queryByTestId('preview-banner')).toBeNull());
    expect(onExitPreview).toHaveBeenCalledTimes(1);

    // Let every in-flight refresh/render chain settle before releasing the
    // overlay (jsdom fires a details `toggle` macrotask at mount whose
    // scheduler.open → refresh → render can land late; the scheduler's own
    // timers are all ≥10s, so once the count is stable the window is quiet
    // and the count pin below is deterministic).
    let prev = -1;
    while (renderSpy.mock.calls.length !== prev) {
      prev = renderSpy.mock.calls.length;
      await new Promise((r) => setTimeout(r, 40));
    }
    const postExitRenders = renderSpy.mock.calls.length;

    resolveOverlay(jsonResponse(overlay()));
    // Drain the (cancelled) fetchOverlay continuation, then assert nothing
    // landed: no banner/counts resurrected, NO additional render (the bailed
    // continuation must not reach renderDiagram). The ghost sweep is scoped to
    // post-exit calls only — earlier slots can hold legitimate ghost renders
    // straggling in from a previous test's component.
    await new Promise((r) => setTimeout(r, 80));
    expect(queryByTestId('preview-banner')).toBeNull();
    expect(queryByTestId('preview-counts')).toBeNull();
    expect(renderSpy.mock.calls.length).toBe(postExitRenders);
    for (const [, src] of renderSpy.mock.calls.slice(postExitRenders)) {
      expect(src).not.toContain('ghost');
    }
  });
});

describe('InfraDiagram — unavailable reasons (exact copies)', () => {
  const cases: Array<[string, string]> = [
    ['no_plan', 'No pending plan was found for PR #47. Nothing to preview.'],
    [
      'artifact_error',
      'The plan for PR #47 could not be verified, so it cannot be previewed. Open the approval page for details.',
    ],
    ['resolved', 'PR #47 has already reached a final outcome. The map below shows what is live now.'],
    ['summary_unavailable', 'This plan could not be summarized into a preview. Review the approval page instead.'],
    // unknown token → summary_unavailable copy
    ['something_new', 'This plan could not be summarized into a preview. Review the approval page instead.'],
  ];

  for (const [reason, copy] of cases) {
    it(`renders the ${reason} copy`, async () => {
      const paths: string[] = [];
      const unavailable = overlay({
        available: false,
        reason,
        counts: { create: 0, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 },
        entries: [],
      });
      const { getByTestId } = render(InfraDiagram, {
        props: { call: makeCall(paths, liveGraph(), unavailable), previewPr: 47 },
      });
      await waitFor(() => {
        expect(norm(getByTestId('preview-unavailable').textContent)).toContain(copy);
      });
    });
  }
});

describe('InfraDiagram — transport error + retry', () => {
  it('shows preview-error + retry, and Retry refetches the overlay', async () => {
    const paths: string[] = [];
    let attempt = 0;
    const behavior = (): PlanOverlay | 'reject' => {
      attempt += 1;
      return attempt === 1 ? 'reject' : overlay();
    };
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), behavior), previewPr: 47 },
    });
    await waitFor(() => {
      expect(getByTestId('preview-error').textContent).toContain('Could not load the change preview.');
    });
    expect(getByTestId('preview-retry')).toBeTruthy();
    await fireEvent.click(getByTestId('preview-retry'));
    await waitFor(() => {
      expect(getByTestId('preview-counts').textContent).toContain('1 will be created');
    });
    const previewFetches = paths.filter((p) => p.startsWith('/infra/graph/preview'));
    expect(previewFetches.length).toBe(2);
  });
});

describe('InfraDiagram — refresh fetches BOTH; focus fetches ONLY the graph', () => {
  it('Refresh while preview active fetches /infra/graph AND the preview', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()), previewPr: 47 },
    });
    await waitFor(() => expect(getByTestId('preview-banner')).toBeTruthy());
    const before = paths.length;
    await fireEvent.click(getByTestId('infra-refresh'));
    await waitFor(() => {
      const after = paths.slice(before);
      expect(after).toContain('/infra/graph');
      expect(after).toContain(PREVIEW);
    });
  });

  it('a window focus event while preview active fetches ONLY /infra/graph (no preview poll)', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()), previewPr: 47 },
    });
    await waitFor(() => expect(getByTestId('preview-banner')).toBeTruthy());
    const before = paths.length;
    await fireEvent(window, new Event('focus'));
    await waitFor(() => {
      expect(paths.slice(before)).toContain('/infra/graph');
    });
    // no NEW preview fetch was triggered by focus
    const previewFetchesAfter = paths.slice(before).filter((p) => p.startsWith('/infra/graph/preview'));
    expect(previewFetchesAfter).toEqual([]);
  });
});

describe('InfraDiagram — no previewPr (regression)', () => {
  it('makes ZERO preview fetches, shows no banner, and no ghost legend keys', async () => {
    const paths: string[] = [];
    const { queryByTestId, container } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()) },
    });
    await waitFor(() => {
      expect(paths).toContain('/infra/graph');
    });
    expect(paths.filter((p) => p.startsWith('/infra/graph/preview'))).toEqual([]);
    expect(queryByTestId('preview-banner')).toBeNull();
    // The panel is closed (no previewPr); no ghost legend keys anywhere.
    expect(container.querySelector('.infra-key--ghost-create')).toBeNull();
    expect(container.querySelector('.infra-key--ghost-update')).toBeNull();
    expect(container.querySelector('.infra-key--ghost-destroy')).toBeNull();
  });
});

describe('InfraDiagram — degraded + available overlay', () => {
  it('shows the degraded note AND a rendered diagram region together', async () => {
    const paths: string[] = [];
    const { getByTestId, queryByTestId, container } = render(InfraDiagram, {
      props: { call: makeCall(paths, degradedGraph(), overlay()), previewPr: 47 },
    });
    // The degraded note is present...
    await waitFor(() => {
      expect(getByTestId('infra-degraded')).toBeTruthy();
    });
    // ...AND the ghost-only diagram renders (mermaid mock returns an <svg>).
    await waitFor(() => {
      expect(getByTestId('infra-diagram')).toBeTruthy();
    });
    // Legend gating in the degraded + ghost-preview state: ghost keys render
    // (previewActive) but the live-color legend help ⓘ does NOT (it is gated on
    // graph && !degraded — there are no live colors to explain).
    expect(container.querySelector('.infra-key--ghost-create')).toBeTruthy();
    expect(queryByTestId('legend-help')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Adopt list (Phase 4 — adopt button UI). The "Unmanaged resources" action list
// rendered below the legend; derived from the graph DTO by lib/infra_graph
// helpers. Design §1 + §2.5; Codex review 019eb572.
// ---------------------------------------------------------------------------

const BUCKET = 'storage.googleapis.com/Bucket';
const SA = 'iam.googleapis.com/ServiceAccount';

function adoptGraph(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 3, managed: 1, drift: 2 },
    groups: [
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        adoptable: true,
        count: 2,
        managed: 1,
        drift: 1,
        sensitive: false,
        nodes: [
          { id: 'g0n0', label: 'prod-state', asset_type: BUCKET, managed: true, location: 'asia-northeast1' },
          { id: 'g0n1', label: 'my-old-uploads', asset_type: BUCKET, managed: false, location: 'asia-northeast1' },
        ],
      },
      {
        asset_type: SA,
        label: 'Service account',
        adoptable: false,
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 'g1n0', label: 'ci-runner@proj.iam', asset_type: SA, managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

function allManagedGraph(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 1, managed: 1, drift: 0 },
    groups: [
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        adoptable: true,
        count: 1,
        managed: 1,
        drift: 0,
        sensitive: false,
        nodes: [{ id: 'g0n0', label: 'prod-state', asset_type: BUCKET, managed: true, location: null }],
      },
    ],
    edges: [],
  };
}

describe('InfraDiagram — resource cards', () => {
  it('renders one card per group with a row per node (managed shown too); adoptable→button, non-adoptable→muted', async () => {
    const { getByTestId, getAllByTestId, getByText } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // Two cards (Storage bucket, Service account).
    expect(getAllByTestId('infra-card')).toHaveLength(2);
    // THREE rows: the managed bucket node is now SHOWN (cards list managed too).
    expect(getAllByTestId('infra-card-row')).toHaveLength(3);
    expect(getByText('prod-state')).toBeTruthy();
    // The adoptable drift row gets a button; the SA drift row gets the muted note.
    expect(getAllByTestId('card-adopt-btn')).toHaveLength(1);
    expect(getByTestId('card-not-adoptable').textContent).toContain('not an adoptable type');
  });

  it('clicking Adopt fires onAdopt with the exact prefill string', async () => {
    const onAdopt = vi.fn();
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt },
    });
    await waitFor(() => expect(getByTestId('card-adopt-btn')).toBeTruthy());
    await fireEvent.click(getByTestId('card-adopt-btn'));
    expect(onAdopt).toHaveBeenCalledTimes(1);
    expect(onAdopt).toHaveBeenCalledWith(
      'Adopt the Storage bucket `my-old-uploads` in asia-northeast1 into IaC management.',
    );
  });

  it('shows the managed resource in a card with no Adopt button when every node is managed', async () => {
    const { getByTestId, getByText, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(allManagedGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    expect(getByText('prod-state')).toBeTruthy();
    expect(getByTestId('card-managed-tag')).toBeTruthy();
    expect(queryByTestId('card-adopt-btn')).toBeNull();
  });

  it('disables the Adopt buttons and swallows clicks when adoptDisabled', async () => {
    const onAdopt = vi.fn();
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt, adoptDisabled: true },
    });
    await waitFor(() => expect(getByTestId('card-adopt-btn')).toBeTruthy());
    const btn = getByTestId('card-adopt-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe('Unavailable while the chat is busy or reviewing a past trace.');
    await fireEvent.click(btn);
    expect(onAdopt).not.toHaveBeenCalled();
  });

  it('renders a counts-only card for a sensitive group (no rows, just the hidden count)', async () => {
    const graph: InfraGraph = {
      generated_at: null, project: 'demo', caveat: 'test caveat',
      degraded: false, degraded_reason: null,
      totals: { resources: 2, managed: 0, drift: 2 },
      groups: [
        {
          asset_type: 'secretmanager.googleapis.com/Secret', label: 'Secret',
          count: 2, managed: 0, drift: 2, sensitive: true, nodes: [],
        },
      ],
      edges: [],
    };
    const { getByTestId, queryAllByTestId } = render(InfraDiagram, { props: { call: callWith(graph) } });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    expect(norm(getByTestId('card-counts-only').textContent)).toContain('2 secrets · hidden');
    // A counts-only card has no per-resource rows and no Adopt button.
    expect(queryAllByTestId('infra-card-row')).toHaveLength(0);
    expect(queryAllByTestId('card-adopt-btn')).toHaveLength(0);
  });

  it('shows "+N more unmanaged" only when drift exceeds the shown unmanaged rows', async () => {
    // Group A: drift=5 but only 2 unmanaged nodes sampled → "+3 more".
    // Group B: all 2 drift nodes shown, but a MANAGED node was truncated
    //          (truncated_in_group=1, drift=2, shown-unmanaged=2) → NO trailer
    //          (managed rows never enter the hidden-unmanaged subtraction).
    const graph: InfraGraph = {
      generated_at: null,
      project: 'demo',
      caveat: 'test caveat',
      degraded: false,
      degraded_reason: null,
      totals: { resources: 12, managed: 5, drift: 7 },
      groups: [
        {
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 5,
          managed: 0,
          drift: 5,
          sensitive: false,
          truncated_in_group: 3,
          nodes: [
            { id: 'a0', label: 'bucket-a', asset_type: BUCKET, managed: false, location: null },
            { id: 'a1', label: 'bucket-b', asset_type: BUCKET, managed: false, location: null },
          ],
        },
        {
          asset_type: 'run.googleapis.com/Service',
          label: 'Cloud Run service',
          adoptable: true,
          count: 3,
          managed: 1,
          drift: 2,
          sensitive: false,
          truncated_in_group: 1, // a MANAGED node was truncated, not an unmanaged one
          nodes: [
            { id: 'b0', label: 'svc-a', asset_type: 'run.googleapis.com/Service', managed: false, location: null },
            { id: 'b1', label: 'svc-b', asset_type: 'run.googleapis.com/Service', managed: false, location: null },
          ],
        },
      ],
      edges: [],
    };
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(graph), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    const trailers = getAllByTestId('card-trailer');
    // Exactly ONE trailer (group A), and it reads "+3 more".
    expect(trailers).toHaveLength(1);
    expect(trailers[0].textContent).toContain('+3 more unmanaged Storage bucket');
    expect(trailers[0].textContent).toContain('not shown');
  });
});

// ---------------------------------------------------------------------------
// Item 10 (guided adoption order): rank-sorted adopt list + Start-here chip +
// per-group hint lines. The fixture puts the groups in SERVER order (asset_type-
// sorted by the builder) where rank order DIFFERS from server order, plus an
// unranked drift group, so the client sort is actually exercised.
//   pubsub Topic       rank 2 hint 'topic hint'  (1 unmanaged)
//   run Service        rank 4 hint 'run hint'    (1 unmanaged)
//   iam ServiceAccount not adoptable             (1 unmanaged)
//   storage Bucket     rank 1 hint 'bucket hint' (1 unmanaged)
// After the rank sort: bucket (1) → topic (2) → run (4) → SA (unranked last).
// ---------------------------------------------------------------------------

function rankedAdoptGraph(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 4, managed: 0, drift: 4 },
    groups: [
      {
        asset_type: TOPIC,
        label: 'Pub/Sub topic',
        adoptable: true,
        adopt_rank: 2,
        adopt_hint: 'topic hint',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 't0', label: 'topic-name', asset_type: TOPIC, managed: false, location: null }],
      },
      {
        asset_type: RUN,
        label: 'Cloud Run service',
        adoptable: true,
        adopt_rank: 4,
        adopt_hint: 'run hint',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 'r0', label: 'run-name', asset_type: RUN, managed: false, location: null }],
      },
      {
        asset_type: SA,
        label: 'Service account',
        adoptable: false,
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 's0', label: 'sa-name', asset_type: SA, managed: false, location: null }],
      },
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        adoptable: true,
        adopt_rank: 1,
        adopt_hint: 'bucket hint',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        nodes: [{ id: 'b0', label: 'bucket-name', asset_type: BUCKET, managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

// Same fixture stripped of the rank/hint fields — a stale coordinator response
// that must render exactly today's UI (server order, no chip, no order note).
function staleAdoptGraph(): InfraGraph {
  const g = rankedAdoptGraph();
  for (const grp of g.groups) {
    delete grp.adopt_rank;
    delete grp.adopt_hint;
  }
  return g;
}

describe('InfraDiagram — card order (light-touch guided order)', () => {
  it('orders cards by adopt_rank within the drift tier, unranked last', async () => {
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(rankedAdoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // bucket (rank1) → topic (rank2) → run (rank4) → SA (unranked, last).
    const types = getAllByTestId('infra-card-type').map((t) => norm(t.textContent));
    expect(types).toEqual(['Storage bucket', 'Pub/Sub topic', 'Cloud Run service', 'Service account']);
  });

  it('shows the Start-here chip exactly once, on the top-ranked card', async () => {
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(rankedAdoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    const chips = getAllByTestId('card-start-here');
    expect(chips).toHaveLength(1);
    // The chip sits in the first card (Storage bucket, rank 1).
    const firstCard = getAllByTestId('infra-card')[0];
    expect(firstCard.contains(chips[0])).toBe(true);
    expect(norm(firstCard.textContent)).toContain('Storage bucket');
  });

  it('renders no per-type hint lines and no order-note paragraph (light-touch)', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(rankedAdoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // The verbose guided-order prose was dropped in the card rework.
    expect(queryByTestId('adopt-hint')).toBeNull();
    expect(queryByTestId('adopt-order-note')).toBeNull();
  });

  it('renders server order with no chip when rank fields are absent (stale coordinator)', async () => {
    const { getByTestId, getAllByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(staleAdoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    expect(queryByTestId('card-start-here')).toBeNull();
    // Server order within the drift tier: topic → run → SA → bucket.
    const types = getAllByTestId('infra-card-type').map((t) => norm(t.textContent));
    expect(types).toEqual(['Pub/Sub topic', 'Cloud Run service', 'Service account', 'Storage bucket']);
  });
});

describe('InfraDiagram — refresh coalescing + last-applied-wins (livelock regression)', () => {
  // Prod incident (Phase-4 live e2e, 2026-06-11): the boot-time applied-epoch
  // ladder fires fetches every 10-30s while a cold CAI-backed /infra/graph takes
  // 10-30s to answer — under last-STARTED-wins every response arrived "stale",
  // graph never set, and the panel spun on "Refreshing…" forever. The policy is
  // now last-APPLIED-wins: a completed 200 applies unless a NEWER fetch's
  // response already applied.
  //
  // Backlog-3 residual (2026-06-12): the infra-reader describe budget is now 90s
  // (up from 30s), so a stacked poll or ladder rung could hold both coordinator
  // concurrency-2 slots. refresh() now coalesces: if a fetch is already in flight
  // a new trigger returns immediately — one in-flight request per open panel.
  // Last-applied-wins remains the response-application policy as defense-in-depth.
  function deferredCall() {
    const pending: Array<(g: InfraGraph) => void> = [];
    const call = (_path: string): Promise<Response> =>
      new Promise<Response>((resolve) => {
        pending.push((g) =>
          resolve(
            new Response(JSON.stringify(g), {
              status: 200,
              headers: { 'content-type': 'application/json' },
            }),
          ),
        );
      });
    return { call, pending };
  }

  it('coalesces a trigger while a fetch is in flight — and the pending fetch still applies', async () => {
    // Mount: fetch #1 starts, pending.length === 1.
    const { call, pending } = deferredCall();
    const utils = render(InfraDiagram, { props: { call } });
    await waitFor(() => expect(pending.length).toBe(1)); // fetch #1 in flight
    // Fire a second trigger (expand toggle) while #1 is still pending.
    const details = utils.container.querySelector('details')!;
    details.open = true;
    await fireEvent(details, new Event('toggle'));
    // Coalescing: the in-flight count must stay at 1, not grow to 2.
    await new Promise((r) => setTimeout(r, 25));
    expect(pending.length).toBe(1); // this assertion FAILS pre-implementation (observes 2)
    // The pending fetch still applies — no livelock.
    pending[0](graphWith({ resources: 5, managed: 2, drift: 3 }));
    await waitFor(() => {
      expect(utils.getByTestId('infra-coverage-count').textContent).toBe('2/5 managed · 40%');
    });
  });

  it('a trigger after completion fetches fresh and supersedes', async () => {
    // After the first fetch completes a subsequent trigger starts a new one.
    const { call, pending } = deferredCall();
    const utils = render(InfraDiagram, { props: { call } });
    await waitFor(() => expect(pending.length).toBe(1));
    // Let fetch #1 complete first.
    pending[0](graphWith({ resources: 5, managed: 2, drift: 3 }));
    await waitFor(() => {
      expect(utils.getByTestId('infra-coverage-count').textContent).toBe('2/5 managed · 40%');
    });
    // Now fire a new trigger — with no fetch in flight it should start a new one.
    const details = utils.container.querySelector('details')!;
    details.open = true;
    await fireEvent(details, new Event('toggle'));
    await waitFor(() => expect(pending.length).toBe(2));
    // The second response supersedes (last-applied-wins).
    pending[1](graphWith({ resources: 6, managed: 3, drift: 3 }));
    await waitFor(() => {
      expect(utils.getByTestId('infra-coverage-count').textContent).toBe('3/6 managed · 50%');
    });
  });
});

describe('InfraDiagram — card duplicate group labels (prod crash regression)', () => {
  // Prod incident (Phase-4 live e2e, 2026-06-11): cloudresourcemanager…/Project
  // and compute…/Project BOTH carry the fallback friendly label "Project"; keying
  // the each by g.label → each_key_duplicate crashed the render flush, killing the
  // panel body. Cards must key by the unique asset_type.
  it('renders a card for two groups sharing a label without crashing', async () => {
    const graph: InfraGraph = {
      ...graphWith({ resources: 2, managed: 0, drift: 2 }),
      groups: [
        {
          asset_type: 'cloudresourcemanager.googleapis.com/Project',
          label: 'Project',
          count: 1, managed: 0, drift: 1, sensitive: false,
          nodes: [{ id: 'g0n0', label: 'proj-a', asset_type: 'cloudresourcemanager.googleapis.com/Project', managed: false, location: null }],
        },
        {
          asset_type: 'compute.googleapis.com/Project',
          label: 'Project',
          count: 1, managed: 0, drift: 1, sensitive: false,
          nodes: [{ id: 'g1n0', label: 'proj-b', asset_type: 'compute.googleapis.com/Project', managed: false, location: null }],
        },
      ],
    };
    const { getByTestId, getAllByTestId } = render(InfraDiagram, { props: { call: callWith(graph) } });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    expect(getAllByTestId('infra-card')).toHaveLength(2);
    // The caveat below the grid must also survive (the crash killed it).
    expect(getByTestId('infra-panel').textContent).toContain('test caveat');
  });
});

describe('InfraDiagram — normal path skips Mermaid', () => {
  it('never calls mermaid.render when there is no previewPr (cards only)', async () => {
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(liveGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // Give any stray render chain a chance to fire, then assert none did: the
    // ~500KB Mermaid bundle is never imported on the normal (non-preview) path.
    await new Promise((r) => setTimeout(r, 40));
    expect(renderSpy).not.toHaveBeenCalled();
  });
});

describe('InfraDiagram — card edge cases (5-lens review w4jj7t4a5)', () => {
  it('renders a summary line (not a hollow card or empty note) for a count>0 type whose nodes were all sampled out', async () => {
    const graph: InfraGraph = {
      generated_at: null, project: 'demo', caveat: 'test caveat',
      degraded: false, degraded_reason: null,
      totals: { resources: 3, managed: 3, drift: 0 },
      groups: [
        { asset_type: BUCKET, label: 'Storage bucket', count: 3, managed: 3, drift: 0, sensitive: false, nodes: [] },
      ],
      edges: [],
    };
    const { getByTestId, queryByTestId } = render(InfraDiagram, { props: { call: callWith(graph) } });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    expect(queryByTestId('infra-empty')).toBeNull();
    expect(norm(getByTestId('card-summary').textContent)).toContain('3 storage buckets · not individually listed');
  });

  it('gives each card body list semantics (ul/li) for assistive tech', async () => {
    const { getByTestId, getAllByRole } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // adoptGraph: bucket card (2 rows) + SA card (1 row) = 3 listitems, 2 lists.
    expect(getAllByRole('listitem')).toHaveLength(3);
    expect(getAllByRole('list')).toHaveLength(2);
  });

  it('shows the empty note (not a blank gap) under a resolved-overlay preview over an empty live estate', async () => {
    const emptyGraph: InfraGraph = {
      generated_at: null, project: 'demo', caveat: 'test caveat',
      degraded: false, degraded_reason: null,
      totals: { resources: 0, managed: 0, drift: 0 },
      groups: [], edges: [],
    };
    const resolved = overlay({
      available: false,
      reason: 'resolved',
      counts: { create: 0, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 },
      entries: [],
    });
    const { getByTestId } = render(InfraDiagram, {
      props: { call: makeCall([], emptyGraph, resolved), previewPr: 47 },
    });
    await waitFor(() => expect(getByTestId('preview-unavailable')).toBeTruthy());
    await waitFor(() => expect(getByTestId('infra-empty')).toBeTruthy());
  });
});

describe('InfraDiagram — onGraph lift (tour, item 14)', () => {
  it('reports each applied graph to onGraph', async () => {
    const onGraph = vi.fn();
    const graph = liveGraph();
    const call = vi.fn(async () => jsonResponse(graph));
    render(InfraDiagram, { props: { call, onGraph } });
    await waitFor(() => expect(onGraph).toHaveBeenCalledTimes(1));
    expect(onGraph.mock.calls[0][0].totals).toEqual(graph.totals);
  });
});

// ---------------------------------------------------------------------------
// Control-plane adopt suppression (2026-06-12 ranking-filter follow-up).
// ---------------------------------------------------------------------------

function adoptGraphWithControlPlane(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 3, managed: 0, drift: 3 },
    groups: [
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        count: 2, managed: 0, drift: 2, sensitive: false,
        adoptable: true, adopt_rank: 1, adopt_hint: 'a simple leaf resource',
        nodes: [
          { id: 'g0n0', label: 'demo-tofu-artifacts', asset_type: BUCKET, managed: false, location: null, control_plane: true },
          { id: 'g0n1', label: 'demo-assets', asset_type: BUCKET, managed: false, location: null },
        ],
      },
      {
        asset_type: TOPIC,
        label: 'Pub/Sub topic',
        count: 1, managed: 0, drift: 1, sensitive: false,
        adoptable: true, adopt_rank: 2, adopt_hint: 'small and quick to review',
        nodes: [{ id: 'g1n0', label: 'orders', asset_type: TOPIC, managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

function adoptGraphAllControlPlane(): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 2, managed: 0, drift: 2 },
    groups: [
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        count: 1, managed: 0, drift: 1, sensitive: false,
        adoptable: true, adopt_rank: 1, adopt_hint: 'a simple leaf resource',
        nodes: [
          { id: 'g0n0', label: 'demo-tofu-state', asset_type: BUCKET, managed: false, location: null, control_plane: true },
        ],
      },
      {
        asset_type: TOPIC,
        label: 'Pub/Sub topic',
        count: 1, managed: 0, drift: 1, sensitive: false,
        adoptable: true, adopt_rank: 2, adopt_hint: 'small and quick to review',
        nodes: [{ id: 'g1n0', label: 'orders', asset_type: TOPIC, managed: false, location: null }],
      },
    ],
    edges: [],
  };
}

function adoptGraphWithServiceManagedBucket(): InfraGraph {
  // Server marks a Google-service-managed bucket (Cloud Build staging) with the
  // SAME control_plane flag — the frontend needs no second signal.
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 2, managed: 0, drift: 2 },
    groups: [
      {
        asset_type: BUCKET,
        label: 'Storage bucket',
        count: 2, managed: 0, drift: 2, sensitive: false,
        adoptable: true, adopt_rank: 1, adopt_hint: 'a simple leaf resource',
        nodes: [
          { id: 'g0n0', label: 'demo_cloudbuild', asset_type: BUCKET, managed: false, location: null, control_plane: true },
          { id: 'g0n1', label: 'demo-assets', asset_type: BUCKET, managed: false, location: null },
        ],
      },
    ],
    edges: [],
  };
}

describe('InfraDiagram — service-managed bucket adopt suppression', () => {
  it('a Google-service-managed bucket row is suppressed with the denylist note', async () => {
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraphWithServiceManagedBucket()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    const note = getByTestId('card-control-plane');
    // The single unified note honestly covers BOTH kinds — assert the
    // service-managed clause landed alongside the denylist protection.
    expect(note.textContent).toContain('Google service');
    expect(note.textContent).toContain('denylist');
    // only the genuinely adoptable demo-assets row keeps its button
    expect(getAllByTestId('card-adopt-btn')).toHaveLength(1);
  });
});

describe('InfraDiagram — control-plane adopt suppression', () => {
  it('a control-plane row shows the denylist note instead of an Adopt button', async () => {
    const { getByTestId, getAllByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraphWithControlPlane()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    const note = getByTestId('card-control-plane');
    // The note names the protection, not ownership (an unrelated `acme-tofu-state`
    // is refused too), and keeps the "denylist" word.
    expect(note.textContent).toContain('control-plane');
    expect(note.textContent).toContain('denylist');
    // exactly the two non-control-plane drift rows (demo-assets, orders) get buttons
    expect(getAllByTestId('card-adopt-btn')).toHaveLength(2);
    // it is NOT the generic "not an adoptable type" note
    expect(queryByTestId('card-not-adoptable')).toBeNull();
  });

  it('Start here stays on the rank-1 card while it still has an adoptable row', async () => {
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraphWithControlPlane()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // bucket group (rank1) has demo-assets adoptable → chip on the buckets card (first).
    const chip = getByTestId('card-start-here');
    const firstCard = getAllByTestId('infra-card')[0];
    expect(firstCard.contains(chip)).toBe(true);
    expect(norm(firstCard.textContent)).toContain('Storage bucket');
  });

  it('a ranked card whose every row is control-plane cannot claim Start here', async () => {
    const { getByTestId, getAllByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraphAllControlPlane()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-cards')).toBeTruthy());
    // bucket group has ONLY the control-plane node → chip moves to the rank-2 topic card.
    const chip = getByTestId('card-start-here');
    const topicCard = getAllByTestId('infra-card').find((c) => norm(c.textContent).includes('Pub/Sub topic'))!;
    expect(topicCard.contains(chip)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Hero band + legend (kept from the 2026-06-24 hierarchy rework; the Mermaid map
// and the standalone adopt zone were replaced by the resource card grid in the
// 2026-06-24-infra-resource-cards design).
//   Hero   — coverage meter / degraded note + Refresh, framed.
//   Legend — explains the card colors with a single HelpHint.
// ---------------------------------------------------------------------------

describe('InfraDiagram — hero band (zone 1)', () => {
  it('nests the coverage meter and the Refresh button inside the hero band (healthy)', async () => {
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 9, managed: 7, drift: 2 })) },
    });
    await waitFor(() => expect(getByTestId('coverage-meter')).toBeTruthy());
    const hero = getByTestId('infra-hero');
    expect(hero.contains(getByTestId('coverage-meter'))).toBe(true);
    expect(hero.contains(getByTestId('infra-refresh'))).toBe(true);
  });

  it('moves the degraded note into the hero band, with no coverage meter', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 5, managed: 3, drift: 2 }, true)) },
    });
    await waitFor(() => expect(getByTestId('infra-degraded')).toBeTruthy());
    expect(getByTestId('infra-hero').contains(getByTestId('infra-degraded'))).toBe(true);
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('renders the hero with a reachable Refresh while the first fetch is still loading', async () => {
    // A call that never resolves: graph stays null and loading stays true.
    const call = (): Promise<Response> => new Promise<Response>(() => {});
    const { getByTestId } = render(InfraDiagram, { props: { call } });
    await waitFor(() => expect(getByTestId('infra-hero')).toBeTruthy());
    const hero = getByTestId('infra-hero');
    expect(hero.contains(getByTestId('infra-refresh'))).toBe(true);
    expect(hero.textContent).toContain('Loading inventory');
  });
});

describe('InfraDiagram — legend help (zone 2)', () => {
  it('reveals the managed/drift/counts-only explanation when the legend help is clicked', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 9, managed: 7, drift: 2 })) },
    });
    await waitFor(() => expect(getByTestId('legend-help')).toBeTruthy());
    // The panel is hidden until the help button is clicked.
    expect(queryByTestId('legend-help-panel')).toBeNull();
    await fireEvent.click(getByTestId('legend-help'));
    const panel = getByTestId('legend-help-panel');
    expect(panel.textContent).toContain('managed in IaC');
    expect(panel.textContent).toContain('drift');
    expect(panel.textContent).toContain('counts-only');
  });

  it('exposes the legend to assistive tech (no aria-hidden)', async () => {
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 9, managed: 7, drift: 2 })) },
    });
    await waitFor(() => expect(getByTestId('infra-legend')).toBeTruthy());
    expect(getByTestId('infra-legend').getAttribute('aria-hidden')).toBeNull();
  });
});
