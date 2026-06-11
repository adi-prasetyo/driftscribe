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
  'Previewing PR #47 — dashed nodes show what approving this change would do. The live map does not change until the change is applied.';

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
  it('clears the banner, calls onExitPreview once, and re-renders WITHOUT ghosts', async () => {
    const paths: string[] = [];
    const onExitPreview = vi.fn();
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: makeCall(paths, liveGraph(), overlay()), previewPr: 47, onExitPreview },
    });
    // Synchronize on THIS component's completed ghost render before clicking:
    // the diagram is in the DOM and the LATEST composed Mermaid src carries a
    // ghost class (guards the post-exit assertion against a vacuous pass; the
    // banner alone renders before any fetch resolves, so waiting only for it
    // would let the click race the component's own render chain).
    await waitFor(() => expect(queryByTestId('infra-diagram')).toBeTruthy());
    await waitFor(() => {
      const calls = renderSpy.mock.calls;
      expect(calls.length).toBeGreaterThan(0);
      expect(calls[calls.length - 1][1]).toContain('ghost');
    });
    const preExitRenders = renderSpy.mock.calls.length;

    await fireEvent.click(getByTestId('preview-exit'));
    await waitFor(() => {
      expect(queryByTestId('preview-banner')).toBeNull();
    });
    expect(onExitPreview).toHaveBeenCalledTimes(1);
    // Exit triggered a FRESH render (the open panel re-composes the map)...
    await waitFor(() => {
      expect(renderSpy.mock.calls.length).toBeGreaterThan(preExitRenders);
    });
    // ...and the last composed src carries NO ghost class tokens.
    const calls = renderSpy.mock.calls;
    const [, lastSrc] = calls[calls.length - 1];
    expect(lastSrc).not.toContain('ghost');
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
    ['no_plan', 'No pending plan was found for PR #47 — nothing to preview.'],
    [
      'artifact_error',
      'The plan for PR #47 could not be verified, so it cannot be previewed. Open the approval page for details.',
    ],
    ['resolved', 'PR #47 has already reached a final outcome — the map below shows what is live now.'],
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
    const { getByTestId } = render(InfraDiagram, {
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

describe('InfraDiagram — adopt list', () => {
  it('lists one row per unmanaged node (button for adoptable, muted for not), managed absent', async () => {
    const { getByTestId, getAllByTestId, queryByText } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('adopt-list')).toBeTruthy());
    // Heading copy is honest about exhaustiveness (Codex finding 4). norm()
    // collapses the multi-line template whitespace before the exact-copy check.
    expect(norm(getByTestId('adopt-list').textContent)).toContain(
      'Unmanaged resources shown on the map — they exist in your project but are not under IaC management',
    );
    // TWO rows (the managed bucket node is absent).
    expect(getAllByTestId('adopt-row')).toHaveLength(2);
    expect(queryByText('prod-state')).toBeNull();
    // The adoptable row gets a button; the SA row gets the muted span.
    const buttons = getAllByTestId('adopt-btn');
    expect(buttons).toHaveLength(1);
    expect(getByTestId('adopt-unavailable').textContent).toContain('not yet adoptable');
  });

  it('clicking Adopt fires onAdopt with the exact prefill string', async () => {
    const onAdopt = vi.fn();
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt },
    });
    await waitFor(() => expect(getByTestId('adopt-btn')).toBeTruthy());
    await fireEvent.click(getByTestId('adopt-btn'));
    expect(onAdopt).toHaveBeenCalledTimes(1);
    expect(onAdopt).toHaveBeenCalledWith(
      'Adopt the Storage bucket `my-old-uploads` in asia-northeast1 into IaC management.',
    );
  });

  it('renders NO adopt-list when every node is managed', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(allManagedGraph()), onAdopt: () => {} },
    });
    await waitFor(() => expect(getByTestId('infra-coverage-count')).toBeTruthy());
    expect(queryByTestId('adopt-list')).toBeNull();
  });

  it('disables the buttons and swallows clicks when adoptDisabled', async () => {
    const onAdopt = vi.fn();
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(adoptGraph()), onAdopt, adoptDisabled: true },
    });
    await waitFor(() => expect(getByTestId('adopt-btn')).toBeTruthy());
    const btn = getByTestId('adopt-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe('Unavailable while the chat is busy or reviewing a past trace.');
    await fireEvent.click(btn);
    expect(onAdopt).not.toHaveBeenCalled();
  });

  it('shows "+N more unmanaged" only when drift exceeds the shown unmanaged rows', async () => {
    // Group A: drift=5 but only 2 unmanaged nodes sampled → "+3 more".
    // Group B: all 2 drift nodes shown, but a MANAGED node was truncated
    //          (truncated_in_group=1, drift=2, shown-unmanaged=2) → NO trailer
    //          (truncated_in_group counts unsampled resources, not unmanaged ones).
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
    await waitFor(() => expect(getByTestId('adopt-list')).toBeTruthy());
    const trailers = getAllByTestId('adopt-trailer');
    // Exactly ONE trailer (group A), and it reads "+3 more".
    expect(trailers).toHaveLength(1);
    expect(trailers[0].textContent).toContain('+3 more unmanaged Storage bucket');
    expect(trailers[0].textContent).toContain('not on the map');
  });
});
