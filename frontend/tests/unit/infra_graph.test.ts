import { describe, it, expect } from 'vitest';
import {
  toMermaid,
  escapeMermaidLabel,
  hasRenderableNodes,
  overlayRenderable,
  overlayCountsLine,
  previewPrFromSearch,
  adoptRows,
  adoptPrefill,
  adoptGroupRank,
  normalizeForPrompt,
  type InfraGraph,
  type InfraGroup,
  type InfraNode,
  type PlanOverlay,
  type OverlayEntry,
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

// ===========================================================================
// Ghost-node overlay composition (ClickOps Wave 2 item 6) — Decision 5
// ===========================================================================

const TOPIC = 'pubsub.googleapis.com/Topic';
const BUCKET = 'storage.googleapis.com/Bucket';
const SA = 'iam.googleapis.com/ServiceAccount';

function entry(p: Partial<OverlayEntry> & { verb: OverlayEntry['verb'] }): OverlayEntry {
  return {
    rtype: 'google_pubsub_topic',
    type_label: 'Pub/Sub topic',
    name: '',
    address: 'google_pubsub_topic.t',
    asset_type: TOPIC,
    sensitive: false,
    location: null,
    ...p,
  };
}

function overlay(p: Partial<PlanOverlay> = {}): PlanOverlay {
  return {
    pr_number: 47,
    available: true,
    reason: null,
    counts: { create: 0, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 },
    hidden: 0,
    entries: [],
    ...p,
  };
}

// A graph with two real groups (Pub/Sub topic + Cloud Run service) for reclass tests.
function liveGraph(): InfraGraph {
  return graph({
    totals: { resources: 3, managed: 2, drift: 1 },
    groups: [
      group({
        asset_type: TOPIC,
        label: 'Pub/Sub topic',
        count: 1,
        managed: 1,
        nodes: [node({ id: 'g0n0', label: 'drift-events', asset_type: TOPIC, managed: true })],
      }),
      group({
        asset_type: RUN,
        label: 'Cloud Run service',
        count: 2,
        managed: 1,
        nodes: [
          node({ id: 'g1n0', label: 'storefront', asset_type: RUN, managed: true }),
          node({ id: 'g1n1', label: 'orders-worker', asset_type: RUN, managed: false }),
        ],
      }),
    ],
  });
}

describe('toMermaid — no overlay is byte-identical (regression pin)', () => {
  it('produces identical output with overlay omitted vs explicitly undefined', () => {
    const g = liveGraph();
    expect(toMermaid(g, undefined)).toBe(toMermaid(g));
  });

  it('emits no ghost classDefs or ghost classes without an overlay', () => {
    const src = toMermaid(liveGraph());
    expect(src).not.toContain('ghostCreate');
    expect(src).not.toContain('ghostUpdate');
    expect(src).not.toContain('ghostDestroy');
    // The classDef block is exactly the three live classDefs.
    expect(src).toContain('classDef managed');
    expect(src).toContain('classDef drift');
    expect(src).toContain('classDef hidden');
    expect(src).not.toContain('stroke-dasharray');
  });
});

describe('toMermaid — ghost classDefs gating', () => {
  it('appends ghost classDefs when the overlay has entries', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }));
    expect(src).toContain('classDef ghostCreate');
    expect(src).toContain('classDef ghostUpdate');
    expect(src).toContain('classDef ghostDestroy');
    expect(src).toContain('stroke-dasharray:6 4');
  });

  it('pins the ghost classDef colors to the design-token hexes', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }));
    expect(src).toContain('classDef ghostCreate fill:#ecf6ef,stroke:#1f8a4c');
    expect(src).toContain('classDef ghostUpdate fill:#fcf3dc,stroke:#9a6b00');
    expect(src).toContain('classDef ghostDestroy fill:#fdeef0,stroke:#c5303f');
  });

  it('appends ghost classDefs when only hidden > 0', () => {
    const src = toMermaid(liveGraph(), overlay({ hidden: 2 }));
    expect(src).toContain('classDef ghostCreate');
  });

  it('does NOT append ghost classDefs for an empty available overlay', () => {
    const src = toMermaid(liveGraph(), overlay({}));
    expect(src).not.toContain('ghostCreate');
  });
});

describe('toMermaid — create / import always ADD a ghost', () => {
  it('adds a green dashed ghost inside the mapped group subgraph', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'create', name: 'order-events' })], counts: { create: 1, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 } }),
    );
    expect(src).toMatch(/\["order-events · will be created"\]:::ghostCreate/);
    // It lives inside the Pub/Sub topic subgraph alongside the live node.
    const topicSub = src.slice(src.indexOf('subgraph sg0['));
    expect(topicSub).toContain('order-events · will be created');
    // The live node was not reclassed.
    expect(src).toContain('drift-events"]:::managed');
  });

  it('renders an import ghost with the "will be imported" suffix and ghostCreate', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'import', name: 'imported-topic' })] }));
    expect(src).toMatch(/\["imported-topic · will be imported"\]:::ghostCreate/);
  });

  it('never reclasses a same-labeled live node for a create', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'drift-events' })] }));
    // The live drift-events node stays managed; a separate ghost is added.
    expect(src).toContain('drift-events"]:::managed');
    expect(src).toMatch(/\["drift-events · will be created"\]:::ghostCreate/);
  });
});

describe('toMermaid — update / change / forget / destroy / replace RECLASS matching nodes', () => {
  it('reclasses a live node matched by exact label (update → ghostUpdate)', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'update', rtype: 'google_cloud_run_v2_service', type_label: 'Cloud Run service', asset_type: RUN, name: 'storefront' })] }),
    );
    expect(src).toMatch(/\["storefront · will be modified"\]:::ghostUpdate/);
    // The original :::managed line for storefront is gone (reclassed, not duplicated).
    expect(src).not.toMatch(/\["storefront"\]:::managed/);
    // orders-worker still present and unchanged.
    expect(src).toContain('orders-worker"]:::drift');
  });

  it('matches a full-path provider name by its short (last-/-segment) label', () => {
    const g = graph({
      groups: [
        group({
          asset_type: SA,
          label: 'Service account',
          count: 1,
          nodes: [node({ id: 'g0n0', label: 'sa@p.iam.gserviceaccount.com', asset_type: SA, managed: true })],
        }),
      ],
    });
    const src = toMermaid(
      g,
      overlay({
        entries: [
          entry({
            verb: 'update',
            rtype: 'google_service_account',
            type_label: 'Service account',
            asset_type: SA,
            name: 'projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com',
          }),
        ],
      }),
    );
    expect(src).toMatch(/\["sa@p.iam.gserviceaccount.com · will be modified"\]:::ghostUpdate/);
    expect(src).not.toMatch(/\["sa@p.iam.gserviceaccount.com"\]:::managed/);
  });

  it('ADDS a ghost when an update has no matching live node', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'update', name: 'no-such-topic' })] }),
    );
    expect(src).toMatch(/\["no-such-topic · will be modified"\]:::ghostUpdate/);
    // existing live topic untouched
    expect(src).toContain('drift-events"]:::managed');
  });

  it('reclasses ALL live nodes sharing the matched label', () => {
    const g = graph({
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run service',
          count: 2,
          nodes: [
            node({ id: 'g0n0', label: 'dup', asset_type: RUN, managed: true }),
            node({ id: 'g0n1', label: 'dup', asset_type: RUN, managed: false }),
          ],
        }),
      ],
    });
    const src = toMermaid(
      g,
      overlay({ entries: [entry({ verb: 'update', rtype: 'google_cloud_run_v2_service', type_label: 'Cloud Run service', asset_type: RUN, name: 'dup' })] }),
    );
    const matches = src.match(/\["dup · will be modified"\]:::ghostUpdate/g) ?? [];
    expect(matches.length).toBe(2);
    expect(src).not.toMatch(/\["dup"\]:::(managed|drift)/);
  });

  it('destroy → ghostDestroy + "will be destroyed"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'destroy', name: 'drift-events' })] }));
    expect(src).toMatch(/\["drift-events · will be destroyed"\]:::ghostDestroy/);
  });

  it('replace → ghostDestroy + "will be replaced"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'replace', name: 'drift-events' })] }));
    expect(src).toMatch(/\["drift-events · will be replaced"\]:::ghostDestroy/);
  });

  it('forget → ghostUpdate + "will leave IaC management"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'forget', name: 'drift-events' })] }));
    expect(src).toMatch(/\["drift-events · will leave IaC management"\]:::ghostUpdate/);
  });

  it('change → ghostUpdate + "will change"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'change', name: 'drift-events' })] }));
    expect(src).toMatch(/\["drift-events · will change"\]:::ghostUpdate/);
  });
});

describe('toMermaid — fallback "Planned changes" subgraph', () => {
  it('places an unmapped (asset_type:null) ghost in sgplan with a type_label prefix', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({
        entries: [
          entry({
            verb: 'create',
            rtype: 'google_project_iam_member',
            type_label: 'IAM member',
            name: 'roles/run.invoker',
            address: 'google_project_iam_member.invoker',
            asset_type: null,
          }),
        ],
      }),
    );
    expect(src).toContain('subgraph sgplan["Planned changes"]');
    const plan = src.slice(src.indexOf('subgraph sgplan['));
    // base = shortName("roles/run.invoker") = "run.invoker"
    expect(plan).toMatch(/\["IAM member: run.invoker · will be created"\]:::ghostCreate/);
  });

  it('falls back to the address when the entry has no name', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({
        entries: [
          entry({
            verb: 'create',
            rtype: 'google_project_iam_member',
            type_label: 'IAM member',
            name: '',
            address: 'google_project_iam_member.invoker',
            asset_type: null,
          }),
        ],
      }),
    );
    expect(src).toContain('subgraph sgplan["Planned changes"]');
    expect(src).toMatch(/google_project_iam_member.invoker · will be created/);
  });

  it('places a mapped ghost into sgplan when no matching live group exists', () => {
    // A bucket create, but the live graph has no bucket group.
    const src = toMermaid(
      liveGraph(),
      overlay({
        entries: [entry({ verb: 'create', rtype: 'google_storage_bucket', type_label: 'Storage bucket', asset_type: BUCKET, name: 'assets-bucket' })],
      }),
    );
    expect(src).toContain('subgraph sgplan["Planned changes"]');
    const plan = src.slice(src.indexOf('subgraph sgplan['));
    expect(plan).toMatch(/\["Storage bucket: assets-bucket · will be created"\]:::ghostCreate/);
  });
});

describe('toMermaid — sensitive entries are name-free', () => {
  it('renders "<type_label> (name hidden) · <suffix>" with no name/address', () => {
    // A live Secret group so the ghost lands inside it (no fallback type prefix).
    const g = graph({
      groups: [group({ asset_type: SECRET, label: 'Secret', count: 2, sensitive: true, nodes: [] })],
    });
    const src = toMermaid(
      g,
      overlay({
        entries: [
          entry({
            verb: 'create',
            rtype: 'google_secret_manager_secret',
            type_label: 'Secret',
            name: '',
            address: '',
            asset_type: SECRET,
            sensitive: true,
            location: '',
          }),
        ],
      }),
    );
    // Parens are entity-escaped (escape-relevant chars), suffix is a trusted literal.
    expect(src).toMatch(/\["Secret #40;name hidden#41; · will be created"\]:::ghostCreate/);
    expect(src).not.toContain('payment-api-key');
  });
});

describe('toMermaid — hidden overflow node', () => {
  it('adds a "+N more planned change(s)" hidden node in the fallback subgraph', () => {
    const src = toMermaid(liveGraph(), overlay({ hidden: 2 }));
    expect(src).toContain('subgraph sgplan["Planned changes"]');
    // "(s)" parens are entity-escaped.
    expect(src).toMatch(/\["\+2 more planned change#40;s#41;"\]:::hidden/);
  });
});

describe('toMermaid — counts-only / empty group arms still receive ghosts', () => {
  it('emits a create ghost into an existing-but-empty group subgraph (drew=true)', () => {
    // A group with a count but no sampled nodes (capped to zero) — the counts-only arm.
    const g = graph({
      groups: [group({ asset_type: TOPIC, label: 'Pub/Sub topic', count: 1, nodes: [] })],
    });
    const src = toMermaid(g, overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }));
    expect(src).toContain('subgraph sg0["Pub/Sub topic"]');
    const sub = src.slice(src.indexOf('subgraph sg0['));
    expect(sub).toContain('order-events · will be created');
    // not the empty-diagram fallback
    expect(src).not.toContain('No resources indexed yet');
  });

  it('emits a sensitive ghost into the counts-only secrets group', () => {
    const g = graph({
      groups: [group({ asset_type: SECRET, label: 'Secret', count: 3, sensitive: true, nodes: [] })],
    });
    const src = toMermaid(
      g,
      overlay({
        entries: [
          entry({
            verb: 'create',
            rtype: 'google_secret_manager_secret',
            type_label: 'Secret',
            name: '',
            address: '',
            asset_type: SECRET,
            sensitive: true,
            location: '',
          }),
        ],
      }),
    );
    // The counts-only placeholder AND the ghost both live in the Secret subgraph.
    const sub = src.slice(src.indexOf('subgraph sg0['));
    expect(sub).toContain('3 secrets · hidden');
    expect(sub).toMatch(/Secret #40;name hidden#41; · will be created/);
  });

  it('emits a full-path reclass fall-through into an empty group EXACTLY ONCE (no dup ghosts)', () => {
    // A reclass-verb entry whose full name and shortName differ is stored under
    // TWO map keys; the empty-group fall-through must dedupe (Set) like the
    // non-empty branch does, or one planned change renders as two ghost nodes.
    const g = graph({
      groups: [group({ asset_type: SA, label: 'Service account', count: 1, nodes: [] })],
    });
    const src = toMermaid(
      g,
      overlay({
        entries: [
          entry({
            verb: 'update',
            rtype: 'google_service_account',
            type_label: 'Service account',
            asset_type: SA,
            name: 'projects/p/serviceAccounts/sa@p.iam.gserviceaccount.com',
          }),
        ],
      }),
    );
    const occurrences = src.match(/· will be modified/g) ?? [];
    expect(occurrences.length).toBe(1);
  });
});

describe('toMermaid — degraded / empty live graph still previews ghosts', () => {
  it('renders a ghost-only diagram for a groups:[] graph + 1 create (no empty fallback)', () => {
    const src = toMermaid(graph({ groups: [] }), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }));
    expect(src).toContain('subgraph sgplan["Planned changes"]');
    expect(src).toMatch(/order-events · will be created/);
    expect(src).not.toContain('No resources indexed yet');
  });
});

describe('toMermaid — ghost labels are escaped', () => {
  it('escapes a hostile name and type_label into entity codes', () => {
    const src = toMermaid(
      graph({ groups: [] }),
      overlay({
        entries: [
          entry({
            verb: 'create',
            rtype: 'google_project_iam_member',
            type_label: 'Type[x]',
            name: 'evil]"x',
            address: 'evil]"x',
            asset_type: null,
          }),
        ],
      }),
    );
    expect(src).not.toContain('evil]"x');
    expect(src).toContain('#93;'); // ]
    expect(src).toContain('#34;'); // "
    // The suffix literal survives intact (no escape-relevant chars).
    expect(src).toContain('· will be created');
  });
});

describe('overlayRenderable', () => {
  it('is false for an unavailable overlay', () => {
    expect(overlayRenderable(overlay({ available: false, reason: 'no_plan' }))).toBe(false);
  });
  it('is false for an available overlay with 0 entries and 0 hidden', () => {
    expect(overlayRenderable(overlay({}))).toBe(false);
  });
  it('is true with entries', () => {
    expect(overlayRenderable(overlay({ entries: [entry({ verb: 'create', name: 'x' })] }))).toBe(true);
  });
  it('is true with only hidden > 0', () => {
    expect(overlayRenderable(overlay({ hidden: 1 }))).toBe(true);
  });
  it('is false for a null overlay', () => {
    expect(overlayRenderable(null)).toBe(false);
  });
});

describe('overlayCountsLine', () => {
  it('returns "No infrastructure changes" for an all-zero count', () => {
    expect(overlayCountsLine({ create: 0, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 })).toBe(
      'No infrastructure changes',
    );
  });
  it('joins non-zero verbs with " · " in canonical order', () => {
    expect(
      overlayCountsLine({ create: 1, update: 2, destroy: 3, replace: 4, import: 5, forget: 6, change: 7 }),
    ).toBe(
      '1 will be created · 2 will be modified · 4 will be replaced · 3 will be destroyed · 5 will be imported · 6 will leave management · 7 will change',
    );
  });
  it('omits zero verbs (no inflection of singular/plural)', () => {
    expect(overlayCountsLine({ create: 1, update: 1, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 })).toBe(
      '1 will be created · 1 will be modified',
    );
  });
});

describe('previewPrFromSearch', () => {
  it('parses a positive integer', () => {
    expect(previewPrFromSearch('?preview_pr=47')).toBe(47);
  });
  it('returns null for an empty search', () => {
    expect(previewPrFromSearch('')).toBe(null);
  });
  it('rejects 0', () => {
    expect(previewPrFromSearch('?preview_pr=0')).toBe(null);
  });
  it('rejects negatives', () => {
    expect(previewPrFromSearch('?preview_pr=-3')).toBe(null);
  });
  it('rejects non-integers', () => {
    expect(previewPrFromSearch('?preview_pr=1.5')).toBe(null);
  });
  it('rejects junk', () => {
    expect(previewPrFromSearch('?preview_pr=abc')).toBe(null);
  });
  it('ignores unrelated params', () => {
    expect(previewPrFromSearch('?other=1')).toBe(null);
  });
  it('accepts a leading-zero digit string as its integer value', () => {
    expect(previewPrFromSearch('?preview_pr=00012')).toBe(12);
  });
});

describe('toMermaid + mermaid.parse — real grammar validation (Codex plan-review)', () => {
  it('parses a ghost-heavy composition (classDefs + reclass + fallback subgraph)', async () => {
    const mermaid = (await import('mermaid')).default;
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral', flowchart: { htmlLabels: false } });
    const src = toMermaid(
      liveGraph(),
      overlay({
        hidden: 2,
        entries: [
          entry({ verb: 'create', name: 'order-events' }),
          entry({ verb: 'update', rtype: 'google_cloud_run_v2_service', type_label: 'Cloud Run service', asset_type: RUN, name: 'storefront' }),
          entry({ verb: 'create', rtype: 'google_project_iam_member', type_label: 'IAM member', name: 'roles/run.invoker', address: 'google_project_iam_member.invoker', asset_type: null }),
        ],
      }),
    );
    await expect(mermaid.parse(src)).resolves.toBeTruthy();
  });

  it('parses the degraded ghost-only output', async () => {
    const mermaid = (await import('mermaid')).default;
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral', flowchart: { htmlLabels: false } });
    const src = toMermaid(graph({ degraded: true, groups: [] }), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }));
    await expect(mermaid.parse(src)).resolves.toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Adopt helpers (Phase 4 — adopt button UI). Pure derivations off the graph DTO
// that the InfraDiagram component renders as the "Unmanaged resources" action
// list. Names/locations are UNTRUSTED (they flow into a chat prefill the
// operator sends to the agent), so normalizeForPrompt is the input-hardening
// boundary (Codex review 019eb572 must-fix 2).
// ---------------------------------------------------------------------------

describe('normalizeForPrompt', () => {
  it('collapses CR/LF/tab/NUL/C1 control chars to single spaces', () => {
    // C0 controls (NUL, tab, CR, LF) + a C1 control (U+0085 NEL) interleaved.
    expect(normalizeForPrompt('a\r\nb\tc de', 254)).toBe('a b c d e');
  });

  it('collapses whitespace runs and trims', () => {
    expect(normalizeForPrompt('   foo    bar   ', 254)).toBe('foo bar');
  });

  it('caps a 300-char label at 254', () => {
    const long = 'x'.repeat(300);
    expect(normalizeForPrompt(long, 254)).toBe('x'.repeat(254));
  });

  it('does NOT cap a label at or under the max', () => {
    expect(normalizeForPrompt('x'.repeat(254), 254)).toBe('x'.repeat(254));
    expect(normalizeForPrompt('short', 254)).toBe('short');
  });

  it('passes backticks and quotes through unchanged (NOT an HTML escape)', () => {
    expect(normalizeForPrompt('a`b"c\'d<e>f&g', 254)).toBe('a`b"c\'d<e>f&g');
  });

  it('returns "" for an empty / all-control input', () => {
    expect(normalizeForPrompt('', 254)).toBe('');
    expect(normalizeForPrompt('\r\n\t', 254)).toBe('');
  });
});

describe('adoptPrefill', () => {
  it('composes the exact canonical string WITH a location', () => {
    expect(adoptPrefill('Storage bucket', 'my-old-uploads', 'asia-northeast1')).toBe(
      'Adopt the Storage bucket `my-old-uploads` in asia-northeast1 into IaC management.',
    );
  });

  it('composes the exact canonical string WITHOUT a location', () => {
    expect(adoptPrefill('Pub/Sub topic', 'order-events', null)).toBe(
      'Adopt the Pub/Sub topic `order-events` into IaC management.',
    );
  });

  it('normalizes untrusted fragments (controls/whitespace) before composing', () => {
    expect(adoptPrefill('Storage  bucket', 'na\tme', '  loc ')).toBe(
      'Adopt the Storage bucket `na me` in loc into IaC management.',
    );
  });
});

describe('adoptRows', () => {
  it('returns drift (unmanaged) nodes across non-sensitive groups, in render order', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 2,
          managed: 1,
          drift: 1,
          nodes: [
            node({ id: 'g0n0', label: 'managed-bucket', asset_type: BUCKET, managed: true }),
            node({ id: 'g0n1', label: 'my-old-uploads', asset_type: BUCKET, managed: false, location: 'asia-northeast1' }),
          ],
        }),
      ],
    });
    const rows = adoptRows(g);
    // The managed node is skipped; only the drift node yields a row.
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      nodeId: 'g0n1',
      groupLabel: 'Storage bucket',
      nodeLabel: 'my-old-uploads',
      adoptable: true,
      prefill: 'Adopt the Storage bucket `my-old-uploads` in asia-northeast1 into IaC management.',
    });
  });

  it('skips managed nodes and sensitive groups entirely', () => {
    const g = graph({
      groups: [
        group({
          asset_type: SECRET,
          label: 'Secret',
          adoptable: false,
          sensitive: true,
          count: 3,
          drift: 3,
          nodes: [],
        }),
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 1,
          managed: 1,
          drift: 0,
          nodes: [node({ id: 'g1n0', label: 'all-managed', asset_type: BUCKET, managed: true })],
        }),
      ],
    });
    expect(adoptRows(g)).toEqual([]);
  });

  it('marks a row from a group missing the adoptable field as adoptable:false (fail-quiet)', () => {
    const g = graph({
      groups: [
        group({
          asset_type: 'iam.googleapis.com/ServiceAccount',
          label: 'Service account',
          // no `adoptable` field (stale coordinator response)
          count: 1,
          drift: 1,
          nodes: [node({ id: 'g0n0', label: 'ci-runner@proj.iam', managed: false })],
        }),
      ],
    });
    const rows = adoptRows(g);
    expect(rows).toHaveLength(1);
    expect(rows[0].adoptable).toBe(false);
    // Non-adoptable rows carry NO prefill.
    expect(rows[0].prefill).toBe('');
  });

  it('preserves group then node render order across multiple groups', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 1,
          drift: 1,
          nodes: [node({ id: 'b0', label: 'bucket-a', asset_type: BUCKET, managed: false })],
        }),
        group({
          asset_type: RUN,
          label: 'Cloud Run service',
          adoptable: true,
          count: 2,
          drift: 2,
          nodes: [
            node({ id: 'r0', label: 'svc-a', asset_type: RUN, managed: false }),
            node({ id: 'r1', label: 'svc-b', asset_type: RUN, managed: false }),
          ],
        }),
      ],
    });
    expect(adoptRows(g).map((r) => r.nodeId)).toEqual(['b0', 'r0', 'r1']);
  });
});

describe('adoptGroupRank', () => {
  const base = { asset_type: 't', label: 'T', count: 1, managed: 0, drift: 1,
    sensitive: false, nodes: [] } as unknown as InfraGroup;
  it('returns the rank for an adoptable ranked group', () => {
    expect(adoptGroupRank({ ...base, adoptable: true, adopt_rank: 2 })).toBe(2);
  });
  it('returns null when not adoptable, even with a rank present', () => {
    expect(adoptGroupRank({ ...base, adoptable: false, adopt_rank: 1 })).toBeNull();
    expect(adoptGroupRank({ ...base, adopt_rank: 1 })).toBeNull();
  });
  it('returns null when the field is missing (stale coordinator)', () => {
    expect(adoptGroupRank({ ...base, adoptable: true })).toBeNull();
  });
  it('rejects junk ranks: non-number, NaN, zero, negative, non-integer', () => {
    for (const junk of ['x' as never, NaN, 0, -1, 1.5]) {
      expect(adoptGroupRank({ ...base, adoptable: true, adopt_rank: junk })).toBeNull();
    }
  });
});
