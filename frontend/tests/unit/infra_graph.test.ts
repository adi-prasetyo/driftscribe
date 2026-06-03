import { describe, it, expect } from 'vitest';
import {
  toMermaid,
  escapeMermaidLabel,
  hasRenderableNodes,
  type InfraGraph,
  type InfraGroup,
  type InfraNode,
} from '../../src/lib/infra_graph';

const RUN = 'run.googleapis.com/Service';
const SECRET = 'secretmanager.googleapis.com/Secret';

function node(p: Partial<InfraNode> & { id: string; label: string }): InfraNode {
  return { asset_type: RUN, managed: false, location: null, ...p };
}

function group(p: Partial<InfraGroup> & { asset_type: string; label: string }): InfraGroup {
  return { count: 0, managed: 0, drift: 0, sensitive: false, nodes: [], ...p };
}

function graph(p: Partial<InfraGraph>): InfraGraph {
  return {
    generated_at: '2026-06-03T00:00:00Z',
    project: 'p',
    caveat: 'cav',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 0, managed: 0, drift: 0 },
    groups: [],
    edges: [],
    ...p,
  };
}

describe('toMermaid — structure', () => {
  it('emits a flowchart header and the three classDefs', () => {
    const src = toMermaid(graph({ groups: [] }));
    expect(src.startsWith('flowchart LR')).toBe(true);
    expect(src).toContain('classDef managed');
    expect(src).toContain('classDef drift');
    expect(src).toContain('classDef hidden');
  });

  it('wraps each group in a subgraph titled by its friendly label', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: RUN,
            label: 'Cloud Run service',
            count: 1,
            nodes: [node({ id: 'g0n0', label: 'payment-demo', managed: true })],
          }),
        ],
      }),
    );
    expect(src).toContain('subgraph sg0["Cloud Run service"]');
    expect(src).toContain('end');
  });
});

describe('toMermaid — managed vs drift classes', () => {
  it('colors managed nodes :::managed and drift nodes :::drift', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: RUN,
            label: 'Cloud Run service',
            count: 2,
            nodes: [
              node({ id: 'g0n0', label: 'payment-demo', managed: true }),
              node({ id: 'g0n1', label: 'storefront', managed: false }),
            ],
          }),
        ],
      }),
    );
    expect(src).toMatch(/n0\["payment-demo"\]:::managed/);
    expect(src).toMatch(/n1\["storefront"\]:::drift/);
  });
});

describe('toMermaid — secret / counts-only', () => {
  it('renders a single hidden node with the count and NO real name', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({ asset_type: SECRET, label: 'Secret', count: 3, drift: 3, sensitive: true, nodes: [] }),
        ],
      }),
    );
    expect(src).toContain('3 secrets · hidden');
    expect(src).toMatch(/:::hidden/);
    // no node-bracket carrying a leaked name — only the counts-only label
    expect(src).not.toContain('payment-api-key');
  });

  it('pluralizes a count of one correctly', () => {
    const src = toMermaid(
      graph({ groups: [group({ asset_type: SECRET, label: 'Secret', count: 1, sensitive: true })] }),
    );
    expect(src).toContain('1 secret · hidden');
    expect(src).not.toContain('1 secrets');
  });
});

describe('toMermaid — sample-cap truncation', () => {
  it('adds a "+N more" hidden node when a group was truncated', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: RUN,
            label: 'Cloud Run service',
            count: 12,
            nodes: [node({ id: 'g0n0', label: 'a' }), node({ id: 'g0n1', label: 'b' })],
            truncated_in_group: 10,
          }),
        ],
      }),
    );
    expect(src).toContain('+10 more');
  });
});

describe('toMermaid — label escaping (untrusted names, design §4.3)', () => {
  it('escapes quotes, brackets and angle brackets to numeric entities', () => {
    const nasty = 'a"b]c<script>d{e}|`';
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: RUN,
            label: 'Cloud Run service',
            count: 1,
            nodes: [node({ id: 'g0n0', label: nasty })],
          }),
        ],
      }),
    );
    // none of the raw structural/HTML chars survive inside the node label
    expect(src).not.toContain('a"b');
    expect(src).not.toContain(']c');
    expect(src).not.toContain('<script>');
    expect(src).not.toContain('{e}');
    // entity codes are present instead
    expect(src).toContain('#34;'); // "
    expect(src).toContain('#60;'); // <
    expect(src).toContain('#93;'); // ]
  });

  it('does not double-encode the # in its own entity output', () => {
    // A lone '#' becomes '#35;' — and that '#' must not itself become '#35;35;'.
    expect(escapeMermaidLabel('a#b')).toBe('a#35;b');
  });

  it('clamps an over-long label', () => {
    const long = 'x'.repeat(200);
    const out = escapeMermaidLabel(long);
    expect(out.length).toBeLessThanOrEqual(62); // 60 + ellipsis + tolerance
    expect(out.endsWith('…')).toBe(true);
  });

  it('collapses newlines/tabs to a space', () => {
    expect(escapeMermaidLabel('a\nb\tc')).toBe('a b c');
  });
});

describe('toMermaid — edges (Phase-4 forward-compat)', () => {
  it('draws an edge only when both endpoints resolve to emitted nodes', () => {
    const g = graph({
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run service',
          count: 2,
          nodes: [
            node({ id: 'svc-a', label: 'storefront' }),
            node({ id: 'svc-b', label: 'orders-worker' }),
          ],
        }),
      ],
      edges: [
        { from: 'svc-a', to: 'svc-b', kind: 'calls' },
        { from: 'svc-a', to: 'ghost' }, // unresolved → skipped
      ],
    });
    const src = toMermaid(g);
    // svc-a → n0, svc-b → n1
    expect(src).toMatch(/n0 -->\|calls\| n1/);
    // the unresolved edge produced no arrow to a ghost id
    expect(src).not.toContain('ghost');
  });
});

describe('toMermaid — empty / degraded', () => {
  it('returns a placeholder diagram when nothing is drawable', () => {
    const src = toMermaid(graph({ groups: [] }));
    expect(src).toContain('No resources indexed yet');
  });
});

describe('hasRenderableNodes', () => {
  it('is false for a degraded graph', () => {
    expect(hasRenderableNodes(graph({ degraded: true }))).toBe(false);
  });
  it('is false when no group has nodes or a count', () => {
    expect(
      hasRenderableNodes(graph({ groups: [group({ asset_type: RUN, label: 'x', count: 0 })] })),
    ).toBe(false);
  });
  it('is true when a group has nodes', () => {
    expect(
      hasRenderableNodes(
        graph({ groups: [group({ asset_type: RUN, label: 'x', count: 1, nodes: [node({ id: 'a', label: 'a' })] })] }),
      ),
    ).toBe(true);
  });
  it('is true for a counts-only secret group', () => {
    expect(
      hasRenderableNodes(graph({ groups: [group({ asset_type: SECRET, label: 'Secret', count: 2, sensitive: true })] })),
    ).toBe(true);
  });
});
