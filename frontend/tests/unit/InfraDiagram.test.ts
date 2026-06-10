import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import InfraDiagram from '../../src/components/InfraDiagram.svelte';
import type { InfraGraph } from '../../src/lib/infra_graph';

// Renders InfraDiagram with a stubbed `call` prop (the component's only data
// dependency) and asserts the coverage treatment end-to-end. The panel is
// NEVER opened, so Mermaid is never imported: <details> content is in the DOM
// even while closed, which lets us assert on the body without paying for the
// diagram.

afterEach(cleanup);

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
