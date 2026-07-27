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
  prefillLocation,
  adoptGroupRank,
  resourceCards,
  splitCards,
  scopeTotals,
  startHereAssetType,
  normalizeForPrompt,
  investigateUnmatchedPrefill,
  type InfraGraph,
  type InfraGroup,
  type InfraNode,
  type PlanOverlay,
  type OverlayEntry,
  type UnmatchedDeclaration,
} from '../../src/lib/infra_graph';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// toMermaid/overlayCountsLine resolve verb suffixes + counts-only chrome
// through the infra.graph.* catalog; the suite asserts English (byte-for-byte
// the original inline text), so pin an EN-bound translator.
const t: TranslateFn = (k, p) => translate('en', k, p);

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
    const src = toMermaid(graph({ groups: [] }), undefined, t);
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
      }), undefined, t
    );
    expect(src).toContain('subgraph sg0["Cloud Run service"]');
    expect(src).toContain('end');
  });
});

describe('toMermaid — managed vs drift classes', () => {
  it('colors managed nodes :::managed and adoptable-drift nodes :::drift', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: RUN,
            label: 'Cloud Run service',
            adoptable: true,
            count: 2,
            nodes: [
              node({ id: 'g0n0', label: 'payment-demo', managed: true }),
              node({ id: 'g0n1', label: 'storefront', managed: false }),
            ],
          }),
        ],
      }), undefined, t
    );
    expect(src).toMatch(/n0\["payment-demo"\]:::managed/);
    expect(src).toMatch(/n1\["storefront"\]:::drift/);
  });

  it('colors an unmanaged node in a NON-adoptable group :::hidden (not amber drift)', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: SA,
            label: 'Service account',
            adoptable: false,
            count: 1,
            nodes: [node({ id: 'g0n0', label: 'ci@p', asset_type: SA, managed: false })],
          }),
        ],
      }), undefined, t
    );
    // Amber is reserved for adoptable drift; a non-adoptable node is neutral.
    expect(src).toMatch(/n0\["ci@p"\]:::hidden/);
    expect(src).not.toMatch(/:::drift/);
  });

  it('colors an unmanaged control-plane node :::hidden (system-managed, never amber)', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({
            asset_type: BUCKET,
            label: 'Storage bucket',
            adoptable: true,
            count: 1,
            nodes: [node({ id: 'g0n0', label: 'demo-tofu-state', asset_type: BUCKET, managed: false, control_plane: true })],
          }),
        ],
      }), undefined, t
    );
    expect(src).toMatch(/n0\["demo-tofu-state"\]:::hidden/);
    expect(src).not.toMatch(/:::drift/);
  });
});

describe('toMermaid — secret / counts-only', () => {
  it('renders a single hidden node with the count and NO real name', () => {
    const src = toMermaid(
      graph({
        groups: [
          group({ asset_type: SECRET, label: 'Secret', count: 3, drift: 3, sensitive: true, nodes: [] }),
        ],
      }), undefined, t
    );
    expect(src).toContain('3 secrets · hidden');
    expect(src).toMatch(/:::hidden/);
    // no node-bracket carrying a leaked name — only the counts-only label
    expect(src).not.toContain('payment-api-key');
  });

  it('pluralizes a count of one correctly', () => {
    const src = toMermaid(
      graph({ groups: [group({ asset_type: SECRET, label: 'Secret', count: 1, sensitive: true })] }), undefined, t
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
      }), undefined, t
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
      }), undefined, t
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
    const src = toMermaid(g, undefined, t);
    // svc-a → n0, svc-b → n1
    expect(src).toMatch(/n0 -->\|calls\| n1/);
    // the unresolved edge produced no arrow to a ghost id
    expect(src).not.toContain('ghost');
  });
});

describe('toMermaid — empty / degraded', () => {
  it('returns a placeholder diagram when nothing is drawable', () => {
    const src = toMermaid(graph({ groups: [] }), undefined, t);
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
        adoptable: true,
        count: 1,
        managed: 1,
        nodes: [node({ id: 'g0n0', label: 'drift-events', asset_type: TOPIC, managed: true })],
      }),
      group({
        asset_type: RUN,
        label: 'Cloud Run service',
        adoptable: true,
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
    expect(toMermaid(g, undefined, t)).toBe(toMermaid(g, undefined, t));
  });

  it('emits no ghost classDefs or ghost classes without an overlay', () => {
    const src = toMermaid(liveGraph(), undefined, t);
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
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }), t);
    expect(src).toContain('classDef ghostCreate');
    expect(src).toContain('classDef ghostUpdate');
    expect(src).toContain('classDef ghostDestroy');
    expect(src).toContain('stroke-dasharray:6 4');
  });

  it('pins the ghost classDef colors to the design-token hexes', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }), t);
    expect(src).toContain('classDef ghostCreate fill:#ecf6ef,stroke:#1f8a4c');
    expect(src).toContain('classDef ghostUpdate fill:#fcf3dc,stroke:#9a6b00');
    expect(src).toContain('classDef ghostDestroy fill:#fdeef0,stroke:#c5303f');
  });

  it('appends ghost classDefs when only hidden > 0', () => {
    const src = toMermaid(liveGraph(), overlay({ hidden: 2 }), t);
    expect(src).toContain('classDef ghostCreate');
  });

  it('does NOT append ghost classDefs for an empty available overlay', () => {
    const src = toMermaid(liveGraph(), overlay({}), t);
    expect(src).not.toContain('ghostCreate');
  });
});

describe('toMermaid — create / import always ADD a ghost', () => {
  it('adds a green dashed ghost inside the mapped group subgraph', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'create', name: 'order-events' })], counts: { create: 1, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 } }), t
    );
    expect(src).toMatch(/\["order-events · will be created"\]:::ghostCreate/);
    // It lives inside the Pub/Sub topic subgraph alongside the live node.
    const topicSub = src.slice(src.indexOf('subgraph sg0['));
    expect(topicSub).toContain('order-events · will be created');
    // The live node was not reclassed.
    expect(src).toContain('drift-events"]:::managed');
  });

  it('renders an import ghost with the "will be imported" suffix and ghostCreate', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'import', name: 'imported-topic' })] }), t);
    expect(src).toMatch(/\["imported-topic · will be imported"\]:::ghostCreate/);
  });

  it('never reclasses a same-labeled live node for a create', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'create', name: 'drift-events' })] }), t);
    // The live drift-events node stays managed; a separate ghost is added.
    expect(src).toContain('drift-events"]:::managed');
    expect(src).toMatch(/\["drift-events · will be created"\]:::ghostCreate/);
  });
});

describe('toMermaid — update / change / forget / destroy / replace RECLASS matching nodes', () => {
  it('reclasses a live node matched by exact label (update → ghostUpdate)', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'update', rtype: 'google_cloud_run_v2_service', type_label: 'Cloud Run service', asset_type: RUN, name: 'storefront' })] }), t
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
      }), t
    );
    expect(src).toMatch(/\["sa@p.iam.gserviceaccount.com · will be modified"\]:::ghostUpdate/);
    expect(src).not.toMatch(/\["sa@p.iam.gserviceaccount.com"\]:::managed/);
  });

  it('ADDS a ghost when an update has no matching live node', () => {
    const src = toMermaid(
      liveGraph(),
      overlay({ entries: [entry({ verb: 'update', name: 'no-such-topic' })] }), t
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
      overlay({ entries: [entry({ verb: 'update', rtype: 'google_cloud_run_v2_service', type_label: 'Cloud Run service', asset_type: RUN, name: 'dup' })] }), t
    );
    const matches = src.match(/\["dup · will be modified"\]:::ghostUpdate/g) ?? [];
    expect(matches.length).toBe(2);
    expect(src).not.toMatch(/\["dup"\]:::(managed|drift)/);
  });

  it('destroy → ghostDestroy + "will be destroyed"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'destroy', name: 'drift-events' })] }), t);
    expect(src).toMatch(/\["drift-events · will be destroyed"\]:::ghostDestroy/);
  });

  it('replace → ghostDestroy + "will be replaced"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'replace', name: 'drift-events' })] }), t);
    expect(src).toMatch(/\["drift-events · will be replaced"\]:::ghostDestroy/);
  });

  it('forget → ghostUpdate + "will leave IaC management"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'forget', name: 'drift-events' })] }), t);
    expect(src).toMatch(/\["drift-events · will leave IaC management"\]:::ghostUpdate/);
  });

  it('change → ghostUpdate + "will change"', () => {
    const src = toMermaid(liveGraph(), overlay({ entries: [entry({ verb: 'change', name: 'drift-events' })] }), t);
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
      }), t
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
      }), t
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
      }), t
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
      }), t
    );
    // Parens are entity-escaped (escape-relevant chars), suffix is a trusted literal.
    expect(src).toMatch(/\["Secret #40;name hidden#41; · will be created"\]:::ghostCreate/);
    expect(src).not.toContain('payment-api-key');
  });
});

describe('toMermaid — hidden overflow node', () => {
  it('adds a "+N more planned change(s)" hidden node in the fallback subgraph', () => {
    const src = toMermaid(liveGraph(), overlay({ hidden: 2 }), t);
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
    const src = toMermaid(g, overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }), t);
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
      }), t
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
      }), t
    );
    const occurrences = src.match(/· will be modified/g) ?? [];
    expect(occurrences.length).toBe(1);
  });
});

describe('toMermaid — degraded / empty live graph still previews ghosts', () => {
  it('renders a ghost-only diagram for a groups:[] graph + 1 create (no empty fallback)', () => {
    const src = toMermaid(graph({ groups: [] }), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }), t);
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
      }), t
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
    expect(overlayCountsLine({ create: 0, update: 0, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 }, t)).toBe(
      'No infrastructure changes',
    );
  });
  it('joins non-zero verbs with " · " in canonical order', () => {
    expect(
      overlayCountsLine({ create: 1, update: 2, destroy: 3, replace: 4, import: 5, forget: 6, change: 7 }, t),
    ).toBe(
      '1 will be created · 2 will be modified · 4 will be replaced · 3 will be destroyed · 5 will be imported · 6 will leave management · 7 will change',
    );
  });
  it('omits zero verbs (no inflection of singular/plural)', () => {
    expect(overlayCountsLine({ create: 1, update: 1, destroy: 0, replace: 0, import: 0, forget: 0, change: 0 }, t)).toBe(
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
      }), t
    );
    await expect(mermaid.parse(src)).resolves.toBeTruthy();
  });

  it('parses the degraded ghost-only output', async () => {
    const mermaid = (await import('mermaid')).default;
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral', flowchart: { htmlLabels: false } });
    const src = toMermaid(graph({ degraded: true, groups: [] }), overlay({ entries: [entry({ verb: 'create', name: 'order-events' })] }), t);
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
    expect(normalizeForPrompt('a\r\nb\tc\u0000d\u0085e', 254)).toBe('a b c d e');
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

  it('appends the topic clause for a subscription with a topic', () => {
    expect(
      adoptPrefill('Pub/Sub subscription', 'adopt-probe-sub', null, 'adopt-probe-topic'),
    ).toBe(
      'Adopt the Pub/Sub subscription `adopt-probe-sub` into IaC management. Its topic is `adopt-probe-topic`.',
    );
  });

  it('omits the topic clause when topic is null/empty (legacy string unchanged)', () => {
    const legacy = 'Adopt the Pub/Sub subscription `s` into IaC management.';
    expect(adoptPrefill('Pub/Sub subscription', 's', null)).toBe(legacy);
    expect(adoptPrefill('Pub/Sub subscription', 's', null, null)).toBe(legacy);
    expect(adoptPrefill('Pub/Sub subscription', 's', null, '')).toBe(legacy);
  });

  it('normalizes an untrusted topic (control chars + length) before composing', () => {
    expect(adoptPrefill('Pub/Sub subscription', 's', null, 'a\tb\nc')).toBe(
      'Adopt the Pub/Sub subscription `s` into IaC management. Its topic is `a b c`.',
    );
    const long = 'x'.repeat(300);
    expect(adoptPrefill('Pub/Sub subscription', 's', null, long)).toContain(
      '`' + 'x'.repeat(254) + '`.',
    );
  });

  it('appends the image clause for a run service with an image (location present too)', () => {
    expect(
      adoptPrefill('Cloud Run service', 'adopt-probe-svc', 'asia-northeast1', null, 'gcr.io/cloudrun/hello'),
    ).toBe(
      'Adopt the Cloud Run service `adopt-probe-svc` in asia-northeast1 into IaC management. Its image is `gcr.io/cloudrun/hello`.',
    );
  });

  it('omits the image clause when image is null/empty (legacy string unchanged)', () => {
    const legacy = 'Adopt the Cloud Run service `s` in asia-northeast1 into IaC management.';
    expect(adoptPrefill('Cloud Run service', 's', 'asia-northeast1')).toBe(legacy);
    expect(adoptPrefill('Cloud Run service', 's', 'asia-northeast1', null, null)).toBe(legacy);
    expect(adoptPrefill('Cloud Run service', 's', 'asia-northeast1', null, '')).toBe(legacy);
  });

  it('normalizes an untrusted image and honors the 512 cap WITHOUT truncating a 254+ valid ref', () => {
    // control chars collapse to a space like every other fragment
    expect(adoptPrefill('Cloud Run service', 's', null, null, 'gcr.io/p\t/img')).toBe(
      'Adopt the Cloud Run service `s` into IaC management. Its image is `gcr.io/p /img`.',
    );
    // A valid 300-char ref survives intact — it WOULD be truncated under the 254 topic cap.
    const ref = 'gcr.io/p/' + 'x'.repeat(291); // length 300
    expect(ref.length).toBe(300);
    expect(adoptPrefill('Cloud Run service', 's', null, null, ref)).toContain('`' + ref + '`.');
    // The 512 cap still bites beyond 512.
    expect(adoptPrefill('Cloud Run service', 's', null, null, 'y'.repeat(600))).toContain(
      '`' + 'y'.repeat(512) + '`.',
    );
  });

  it('keeps clause order deterministic if topic AND image are somehow both present', () => {
    expect(adoptPrefill('X', 'n', null, 't', 'i')).toBe(
      'Adopt the X `n` into IaC management. Its topic is `t`. Its image is `i`.',
    );
  });
});

describe('prefillLocation', () => {
  it('returns null for Pub/Sub topics and subscriptions (they are global, location-forbidden)', () => {
    expect(prefillLocation('pubsub.googleapis.com/Topic', 'global')).toBeNull();
    expect(prefillLocation('pubsub.googleapis.com/Subscription', 'global')).toBeNull();
  });

  it('passes every other type location through unchanged (incl. null)', () => {
    expect(prefillLocation('storage.googleapis.com/Bucket', 'asia-northeast1')).toBe('asia-northeast1');
    expect(prefillLocation('run.googleapis.com/Service', null)).toBeNull();
  });

  it('keeps the location for a Cloud Run service (it is REQUIRED by the tool)', () => {
    expect(prefillLocation('run.googleapis.com/Service', 'asia-northeast1')).toBe('asia-northeast1');
  });
});

describe('subscription topic prefill (end-to-end through the row builders)', () => {
  const SUB = 'pubsub.googleapis.com/Subscription';
  const subGraph = () =>
    graph({
      groups: [
        group({
          asset_type: SUB,
          label: 'Pub/Sub subscription',
          adoptable: true,
          count: 1,
          managed: 0,
          drift: 1,
          nodes: [
            node({
              id: 's0',
              label: 'adopt-probe-sub',
              asset_type: SUB,
              managed: false,
              location: 'global',
              topic: 'adopt-probe-topic',
            }),
          ],
        }),
      ],
    });

  it('adoptRows: subscription prefill carries the topic and NO location clause', () => {
    const [row] = adoptRows(subGraph());
    expect(row.prefill).toBe(
      'Adopt the Pub/Sub subscription `adopt-probe-sub` into IaC management. Its topic is `adopt-probe-topic`.',
    );
    expect(row.prefill).not.toContain(' in global');
  });

  it('resourceCards: subscription drift row prefill carries the topic and NO location clause', () => {
    const [card] = resourceCards(subGraph());
    const [row] = card.rows;
    expect(row.prefill).toBe(
      'Adopt the Pub/Sub subscription `adopt-probe-sub` into IaC management. Its topic is `adopt-probe-topic`.',
    );
  });

  it('a subscription without a joined topic falls back to the legacy prefill (no topic clause)', () => {
    const g = subGraph();
    delete g.groups[0].nodes[0].topic;
    const [row] = adoptRows(g);
    expect(row.prefill).toBe('Adopt the Pub/Sub subscription `adopt-probe-sub` into IaC management.');
  });
});

describe('run service image prefill (end-to-end through the row builders)', () => {
  const RUN = 'run.googleapis.com/Service';
  const runGraph = () =>
    graph({
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run service',
          adoptable: true,
          count: 1,
          managed: 0,
          drift: 1,
          nodes: [
            node({
              id: 'r0',
              label: 'adopt-probe-svc',
              asset_type: RUN,
              managed: false,
              location: 'asia-northeast1',
              image: 'gcr.io/cloudrun/hello',
            }),
          ],
        }),
      ],
    });

  it('adoptRows: run service prefill carries the image AND keeps the location clause', () => {
    const [row] = adoptRows(runGraph());
    expect(row.prefill).toBe(
      'Adopt the Cloud Run service `adopt-probe-svc` in asia-northeast1 into IaC management. Its image is `gcr.io/cloudrun/hello`.',
    );
  });

  it('resourceCards: run service drift row prefill carries the image AND the location clause', () => {
    const [card] = resourceCards(runGraph());
    const [row] = card.rows;
    expect(row.prefill).toBe(
      'Adopt the Cloud Run service `adopt-probe-svc` in asia-northeast1 into IaC management. Its image is `gcr.io/cloudrun/hello`.',
    );
  });

  it('a run service without a joined image falls back to the legacy prefill (no image clause)', () => {
    const g = runGraph();
    delete g.groups[0].nodes[0].image;
    const [row] = adoptRows(g);
    expect(row.prefill).toBe(
      'Adopt the Cloud Run service `adopt-probe-svc` in asia-northeast1 into IaC management.',
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
      controlPlane: false,
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

  it('control-plane node in an adoptable group is a non-adoptable row with the flag', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 2,
          drift: 2,
          nodes: [
            node({ id: 'g0n0', label: 'acme-tofu-artifacts', asset_type: BUCKET, managed: false, location: null, control_plane: true }),
            node({ id: 'g0n1', label: 'acme-assets', asset_type: BUCKET, managed: false, location: null }),
          ],
        }),
      ],
    });
    const rows = adoptRows(g);
    expect(rows[0]).toMatchObject({ adoptable: false, controlPlane: true, prefill: '' });
    expect(rows[1]).toMatchObject({ adoptable: true, controlPlane: false });
    expect(rows[1].prefill).toContain('`acme-assets`');
  });

  it('missing control_plane field (stale coordinator) keeps the row adoptable — fail-safe, C2 still blocks', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          adoptable: true,
          count: 1,
          drift: 1,
          nodes: [node({ id: 'g0n0', label: 'x', asset_type: BUCKET, managed: false, location: null })],
        }),
      ],
    });
    expect(adoptRows(g)[0].adoptable).toBe(true);
    expect(adoptRows(g)[0].controlPlane).toBe(false);
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

// ---------------------------------------------------------------------------
// resourceCards / startHereAssetType (card-grid view; design 2026-06-24-infra-
// resource-cards). One card per group: managed + drift rows together, drift rows
// carrying the Adopt affordance. Pure derivation of the same /infra/graph DTO.
// (BUCKET / TOPIC / SA / SECRET / RUN asset-type consts declared at the top.)
// ---------------------------------------------------------------------------

describe('resourceCards — row mapping', () => {
  it('maps a managed node to a non-adoptable managed row', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET,
            label: 'Storage bucket',
            adoptable: true,
            count: 1,
            managed: 1,
            drift: 0,
            nodes: [node({ id: 'b0', label: 'prod-state', managed: true })],
          }),
        ],
      }),
    );
    expect(cards).toHaveLength(1);
    const [row] = cards[0].rows;
    expect(row.status).toBe('managed');
    expect(row.adoptable).toBe(false);
    expect(row.prefill).toBe('');
  });

  it('maps an unmanaged node in an adoptable group to a drift row with a prefill', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET,
            label: 'Storage bucket',
            adoptable: true,
            count: 1,
            managed: 0,
            drift: 1,
            nodes: [node({ id: 'b0', label: 'my-old-uploads', managed: false, location: 'asia-northeast1' })],
          }),
        ],
      }),
    );
    const [row] = cards[0].rows;
    expect(row.status).toBe('drift');
    expect(row.adoptable).toBe(true);
    expect(row.prefill).toBe('Adopt the Storage bucket `my-old-uploads` in asia-northeast1 into IaC management.');
  });

  it('routes a control-plane unmanaged node to systemManaged, not the inline rows', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET,
            label: 'Storage bucket',
            adoptable: true,
            count: 1,
            managed: 0,
            drift: 1,
            nodes: [node({ id: 'b0', label: 'demo-tofu-state', managed: false, control_plane: true })],
          }),
        ],
      }),
    );
    expect(cards[0].rows).toEqual([]); // not inline — collapsed away
    const [sys] = cards[0].systemManaged;
    expect(sys.status).toBe('control_plane');
    expect(sys.adoptable).toBe(false);
    expect(sys.prefill).toBe('');
    expect(cards[0].systemManagedTotal).toBe(1);
  });

  it('maps an unmanaged node in a NON-adoptable group to a neutral untracked row (not drift)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: SA,
            label: 'Service account',
            adoptable: false,
            count: 1,
            managed: 0,
            drift: 1,
            nodes: [node({ id: 's0', label: 'ci-runner@proj.iam', asset_type: SA, managed: false })],
          }),
        ],
      }),
    );
    const [row] = cards[0].rows;
    // A non-adoptable type is not actionable drift: it renders neutral, never amber.
    expect(row.status).toBe('untracked');
    expect(row.adoptable).toBe(false);
    expect(row.prefill).toBe('');
  });

  it('makes a sensitive group a counts-only card (no rows, count preserved)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 2, managed: 0, drift: 2, nodes: [] }),
        ],
      }),
    );
    expect(cards).toHaveLength(1);
    expect(cards[0].sensitive).toBe(true);
    expect(cards[0].rows).toEqual([]);
    expect(cards[0].count).toBe(2);
  });

  it('returns [] for a degraded graph', () => {
    expect(resourceCards(graph({ degraded: true, groups: [] }))).toEqual([]);
  });
});

describe('resourceCards — system-managed collapse', () => {
  it('splits control-plane rows out of inline rows into systemManaged (managed + drift stay)', () => {
    const cards = resourceCards(
      graph({
        totals: { resources: 4, managed: 1, drift: 3 },
        groups: [
          group({
            asset_type: RUN, label: 'Cloud Run service', adoptable: true,
            count: 4, managed: 1, drift: 3, drift_adoptable: 1,
            nodes: [
              node({ id: 'r0', label: 'storefront', managed: true }),
              node({ id: 'r1', label: 'orders-worker', managed: false }),
              node({ id: 'r2', label: 'driftscribe-agent', managed: false, control_plane: true }),
              node({ id: 'r3', label: 'driftscribe-worker', managed: false, control_plane: true }),
            ],
          }),
        ],
      }),
    );
    const card = cards[0];
    expect(card.rows.map((r) => r.status)).toEqual(['managed', 'drift']); // no control_plane inline
    expect(card.systemManaged.map((r) => r.label)).toEqual(['driftscribe-agent', 'driftscribe-worker']);
    expect(card.systemManagedTotal).toBe(2);
    expect(card.managed).toBe(1);
    expect(card.actionableDrift).toBe(1);
  });

  it('infers the true systemManagedTotal from group counts when nodes are sampled (drift − actionableDrift)', () => {
    // 12 services: 1 managed, 1 adoptable drift, 10 control-plane, but only one
    // control-plane node sampled → total must reflect the group figure, not the sample.
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: RUN, label: 'Cloud Run service', adoptable: true,
            count: 12, managed: 1, drift: 11, drift_adoptable: 1,
            nodes: [node({ id: 'r1', label: 'driftscribe-agent', managed: false, control_plane: true })],
          }),
        ],
      }),
    );
    expect(cards[0].systemManaged).toHaveLength(1); // sampled
    expect(cards[0].systemManagedTotal).toBe(10); // 11 raw drift − 1 actionable
  });

  it('has an empty systemManaged (total 0) for a card with no control-plane rows', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET, label: 'Storage bucket', adoptable: true,
            count: 2, managed: 1, drift: 1, drift_adoptable: 1,
            nodes: [
              node({ id: 'b0', label: 'm', asset_type: BUCKET, managed: true }),
              node({ id: 'b1', label: 'd', asset_type: BUCKET, managed: false }),
            ],
          }),
        ],
      }),
    );
    expect(cards[0].systemManaged).toEqual([]);
    expect(cards[0].systemManagedTotal).toBe(0);
  });
});

describe('resourceCards — actionable drift (excludes control-plane / non-adoptable)', () => {
  it('takes actionableDrift from drift_adoptable while keeping raw drift', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: RUN, label: 'Cloud Run service', adoptable: true,
            count: 12, managed: 1, drift: 11, drift_adoptable: 1,
            nodes: [node({ id: 'r0', label: 'adopt-probe-svc', asset_type: RUN, managed: false })],
          }),
        ],
      }),
    );
    expect(cards[0].actionableDrift).toBe(1);
    expect(cards[0].drift).toBe(11); // raw not_in_iac preserved for context
  });

  it('is 0 for a non-adoptable group even with raw drift', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: SA, label: 'Service account', adoptable: false,
            count: 5, managed: 0, drift: 5, drift_adoptable: 0,
            nodes: [node({ id: 's0', label: 'ci@p', asset_type: SA, managed: false })],
          }),
        ],
      }),
    );
    expect(cards[0].actionableDrift).toBe(0);
  });

  it('falls back to raw drift for an adoptable group when drift_adoptable is missing (stale)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET, label: 'Storage bucket', adoptable: true,
            count: 2, managed: 0, drift: 2,
            nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: false })],
          }),
        ],
      }),
    );
    expect(cards[0].actionableDrift).toBe(2);
  });

  it('falls back to 0 for a non-adoptable group when drift_adoptable is missing (stale)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: SA, label: 'Service account',
            count: 3, managed: 0, drift: 3,
            nodes: [node({ id: 's0', label: 'x', asset_type: SA, managed: false })],
          }),
        ],
      }),
    );
    expect(cards[0].actionableDrift).toBe(0);
  });

  it('sorts a card whose drift is entirely control-plane below an actionable card', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: RUN, label: 'Cloud Run service', adoptable: true,
            count: 3, managed: 0, drift: 3, drift_adoptable: 0,
            nodes: [node({ id: 'r0', label: 'driftscribe-agent', asset_type: RUN, managed: false, control_plane: true })],
          }),
          group({
            asset_type: BUCKET, label: 'Storage bucket', adoptable: true,
            count: 1, managed: 0, drift: 1, drift_adoptable: 1,
            nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: false })],
          }),
        ],
      }),
    );
    // BUCKET has actionable drift (tier 0); RUN is all control-plane (tier 1).
    expect(cards.map((c) => c.assetType)).toEqual([BUCKET, RUN]);
  });
});

describe('resourceCards — hidden-unmanaged honesty', () => {
  it('excludes control-plane rows from the "+N more unmanaged" figure (actionable-aware)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: RUN, label: 'Cloud Run service', adoptable: true,
            count: 12, managed: 0, drift: 11, drift_adoptable: 1,
            nodes: [
              node({ id: 'r0', label: 'adopt-probe-svc', asset_type: RUN, managed: false }),
              node({ id: 'r1', label: 'driftscribe-agent', asset_type: RUN, managed: false, control_plane: true }),
            ],
          }),
        ],
      }),
    );
    // actionableDrift 1, exactly one 'drift' row shown → nothing more to adopt.
    expect(cards[0].hiddenUnmanaged).toBe(0);
  });


  it('counts only the unmanaged delta, never the managed rows shown', () => {
    // drift=5, two unmanaged sampled + one managed sampled → +3 hidden (managed
    // row must NOT reduce the hidden-unmanaged figure).
    const cards = resourceCards(
      graph({
        groups: [
          group({
            asset_type: BUCKET,
            label: 'Storage bucket',
            adoptable: true,
            count: 6,
            managed: 1,
            drift: 5,
            nodes: [
              node({ id: 'm0', label: 'prod-state', managed: true }),
              node({ id: 'd0', label: 'b-a', managed: false }),
              node({ id: 'd1', label: 'b-b', managed: false }),
            ],
          }),
        ],
      }),
    );
    expect(cards[0].rows).toHaveLength(3);
    expect(cards[0].hiddenUnmanaged).toBe(3);
  });

  it('keeps a card for every group with resources (count>0), even when all nodes were sampled out', () => {
    // A type with resources must always show a card (matching hasRenderableNodes),
    // so a fully-managed estate whose sample was truncated to zero never collapses
    // to the "No resources indexed yet" note (5-lens review w4jj7t4a5).
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, count: 3, managed: 3, drift: 0, nodes: [] }),
          group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 1, managed: 0, drift: 1, nodes: [] }),
        ],
      }),
    );
    // BUCKET is in-sync (tier 1), SECRET counts-only (tier 2): both kept, in order.
    expect(cards.map((c) => c.assetType)).toEqual([BUCKET, SECRET]);
    const bucket = cards.find((c) => c.assetType === BUCKET);
    expect(bucket?.rows).toEqual([]);
    expect(bucket?.count).toBe(3);
  });

  it('drops a group the backend reported with zero resources (count===0)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: BUCKET, label: 'Storage bucket', count: 0, managed: 0, drift: 0, nodes: [] }),
          group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 0, managed: 0, drift: 0, nodes: [] }),
        ],
      }),
    );
    expect(cards).toEqual([]);
  });
});

describe('resourceCards — ordering (drift-first, rank within, sensitive last)', () => {
  function mixedGraph(): InfraGraph {
    return graph({
      groups: [
        group({ asset_type: TOPIC, label: 'Pub/Sub topic', count: 1, managed: 1, drift: 0, nodes: [node({ id: 't0', label: 'orders', asset_type: TOPIC, managed: true })] }),
        group({ asset_type: RUN, label: 'Cloud Run service', adoptable: true, adopt_rank: 4, count: 1, managed: 0, drift: 1, nodes: [node({ id: 'r0', label: 'svc', asset_type: RUN, managed: false })] }),
        group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 1, managed: 0, drift: 1, nodes: [] }),
        group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, adopt_rank: 1, count: 1, managed: 0, drift: 1, nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: false })] }),
        group({ asset_type: SA, label: 'Service account', adoptable: false, count: 1, managed: 0, drift: 1, nodes: [node({ id: 's0', label: 'sa', asset_type: SA, managed: false })] }),
      ],
    });
  }

  it('orders actionable-drift cards (rank, then stable) before neutral, before counts-only', () => {
    // SA is non-adoptable → 0 actionable drift → it leaves the drift tier and
    // sits with the in-sync Topic in server order (BUCKET/RUN idx before SA idx).
    const cards = resourceCards(mixedGraph());
    expect(cards.map((c) => c.assetType)).toEqual([BUCKET, RUN, TOPIC, SA, SECRET]);
  });

  it('falls back to server order when ranks/adoptable are absent (stale coordinator)', () => {
    const g = mixedGraph();
    for (const grp of g.groups) {
      delete grp.adopt_rank;
      delete grp.adoptable;
    }
    // No adoptable flag → no actionable drift for any type → a single neutral
    // tier in server order, counts-only Secret last.
    const cards = resourceCards(g);
    expect(cards.map((c) => c.assetType)).toEqual([TOPIC, RUN, BUCKET, SA, SECRET]);
  });

  it('keeps in-sync (tier 1) cards in server order even when the server ranked them', () => {
    // The backend can emit adopt_rank on an adoptable type whose drift is 0. Rank
    // orders only the drift tier; in-sync cards must keep their server order.
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: RUN, label: 'Cloud Run service', adoptable: true, adopt_rank: 5, count: 1, managed: 1, drift: 0, nodes: [node({ id: 'r0', label: 'svc', asset_type: RUN, managed: true })] }),
          group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, adopt_rank: 1, count: 1, managed: 1, drift: 0, nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: true })] }),
        ],
      }),
    );
    // Server order [RUN, BUCKET], NOT rank order [BUCKET, RUN].
    expect(cards.map((c) => c.assetType)).toEqual([RUN, BUCKET]);
  });
});

describe('startHereAssetType', () => {
  it('picks the top-ranked card that still has an adoptable row', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, adopt_rank: 1, count: 1, managed: 0, drift: 1, nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: false })] }),
          group({ asset_type: TOPIC, label: 'Pub/Sub topic', adoptable: true, adopt_rank: 2, count: 1, managed: 0, drift: 1, nodes: [node({ id: 't0', label: 'orders', asset_type: TOPIC, managed: false })] }),
        ],
      }),
    );
    expect(startHereAssetType(cards)).toBe(BUCKET);
  });

  it('skips a ranked card whose every row is control-plane', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, adopt_rank: 1, count: 1, managed: 0, drift: 1, nodes: [node({ id: 'b0', label: 'demo-tofu-state', asset_type: BUCKET, managed: false, control_plane: true })] }),
          group({ asset_type: TOPIC, label: 'Pub/Sub topic', adoptable: true, adopt_rank: 2, count: 1, managed: 0, drift: 1, nodes: [node({ id: 't0', label: 'orders', asset_type: TOPIC, managed: false })] }),
        ],
      }),
    );
    expect(startHereAssetType(cards)).toBe(TOPIC);
  });

  it('returns null when no card carries a rank (stale coordinator)', () => {
    const cards = resourceCards(
      graph({
        groups: [
          group({ asset_type: BUCKET, label: 'Storage bucket', count: 1, managed: 0, drift: 1, nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET, managed: false })] }),
        ],
      }),
    );
    expect(startHereAssetType(cards)).toBeNull();
  });
});

describe('resourceCards — adoptable field', () => {
  it('sets adoptable=true on an adoptable group card', () => {
    const cards = resourceCards(
      graph({
        groups: [group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, count: 1, drift: 1, nodes: [node({ id: 'b0', label: 'bkt', asset_type: BUCKET })] })],
      }),
    );
    expect(cards[0].adoptable).toBe(true);
  });

  it('sets adoptable=false on a non-adoptable group card', () => {
    const cards = resourceCards(
      graph({
        groups: [group({ asset_type: SA, label: 'Service account', count: 1, drift: 1, nodes: [node({ id: 's0', label: 'ci@p', asset_type: SA })] })],
      }),
    );
    expect(cards[0].adoptable).toBe(false);
  });

  it('treats a missing adoptable flag (stale coordinator) as false', () => {
    const cards = resourceCards(
      graph({ groups: [group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 2, drift: 2 })] }),
    );
    expect(cards[0].adoptable).toBe(false);
  });
});

describe('splitCards — primary (in DriftScribe scope) vs other', () => {
  // primary := adoptable OR managed > 0; everything else (incl. sensitive
  // secrets) is other → folded behind the disclosure.
  function mkCards(): InfraGraph {
    return graph({
      groups: [
        group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, adopt_rank: 1, count: 2, managed: 1, drift: 1, nodes: [node({ id: 'b0', label: 'mgd', asset_type: BUCKET, managed: true }), node({ id: 'b1', label: 'drift', asset_type: BUCKET, managed: false })] }),
        group({ asset_type: SA, label: 'Service account', count: 1, drift: 1, nodes: [node({ id: 's0', label: 'ci@p', asset_type: SA })] }),
        group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 3, drift: 3 }),
      ],
    });
  }

  it('puts an adoptable card in primary', () => {
    const { primary, other } = splitCards(resourceCards(mkCards()));
    expect(primary.map((c) => c.assetType)).toContain(BUCKET);
    expect(other.map((c) => c.assetType)).not.toContain(BUCKET);
  });

  it('puts a non-adoptable, all-drift card in other', () => {
    const { primary, other } = splitCards(resourceCards(mkCards()));
    expect(other.map((c) => c.assetType)).toContain(SA);
    expect(primary.map((c) => c.assetType)).not.toContain(SA);
  });

  it('puts a sensitive (secret) card in other', () => {
    const { other } = splitCards(resourceCards(mkCards()));
    expect(other.map((c) => c.assetType)).toContain(SECRET);
  });

  it('keeps a managed-but-NON-adoptable card in primary (never hide a managed resource)', () => {
    // Defensive: a future .tf declares a non-adoptable type. managed>0 must
    // keep its card in the default view even though adoptable is false.
    const cards = resourceCards(
      graph({
        groups: [group({ asset_type: SA, label: 'Service account', adoptable: false, count: 2, managed: 1, drift: 1, nodes: [node({ id: 's0', label: 'mgd@p', asset_type: SA, managed: true }), node({ id: 's1', label: 'drift@p', asset_type: SA, managed: false })] })],
      }),
    );
    const { primary, other } = splitCards(cards);
    expect(primary.map((c) => c.assetType)).toContain(SA);
    expect(other).toHaveLength(0);
  });

  it('preserves the resourceCards sort order within each list', () => {
    const cards = resourceCards(mkCards());
    const { primary, other } = splitCards(cards);
    // The split is a stable partition: concatenating primary then other does
    // not reorder relative to the source filtered by membership.
    const primaryFromSource = cards.filter((c) => c.adoptable || c.managed > 0).map((c) => c.assetType);
    const otherFromSource = cards.filter((c) => !(c.adoptable || c.managed > 0)).map((c) => c.assetType);
    expect(primary.map((c) => c.assetType)).toEqual(primaryFromSource);
    expect(other.map((c) => c.assetType)).toEqual(otherFromSource);
  });

  it('returns empty lists for a degraded graph', () => {
    expect(splitCards(resourceCards(graph({ degraded: true, groups: [] })))).toEqual({ primary: [], other: [] });
  });
});

describe('scopeTotals — coverage within the adoptable scope', () => {
  // A live-shaped slice: 1 adoptable type (2 res, 1 managed, 1 drift) + 2
  // non-adoptable noise types (10 + 3 res, all drift). Project total 15.
  function liveSlice(): InfraGraph {
    return graph({
      totals: { resources: 15, managed: 1, drift: 14 },
      groups: [
        group({ asset_type: BUCKET, label: 'Storage bucket', adoptable: true, count: 2, managed: 1, drift: 1, nodes: [node({ id: 'b0', label: 'm', asset_type: BUCKET, managed: true }), node({ id: 'b1', label: 'd', asset_type: BUCKET, managed: false })] }),
        group({ asset_type: 'run.googleapis.com/Revision', label: 'Revision', count: 10, managed: 0, drift: 10, nodes: [node({ id: 'r0', label: 'rev', asset_type: 'run.googleapis.com/Revision', managed: false })] }),
        group({ asset_type: SECRET, label: 'Secret', sensitive: true, count: 3, managed: 0, drift: 3 }),
      ],
    });
  }

  it('computes managed/resources/drift over the PRIMARY (adoptable) cards only', () => {
    const g = liveSlice();
    const s = scopeTotals(resourceCards(g), g.totals.resources);
    expect(s.managed).toBe(1);
    expect(s.resources).toBe(2);
    expect(s.drift).toBe(1);
  });

  it('sums ACTIONABLE drift (excludes control-plane), not raw drift', () => {
    const g = graph({
      groups: [
        group({
          asset_type: RUN, label: 'Cloud Run service', adoptable: true,
          count: 12, managed: 1, drift: 11, drift_adoptable: 1,
          nodes: [node({ id: 'r0', label: 'adopt-probe-svc', asset_type: RUN, managed: false })],
        }),
      ],
    });
    const s = scopeTotals(resourceCards(g), 12);
    expect(s.drift).toBe(1); // the header badge reads actionable, not the raw 11
  });

  it('excludes control-plane rows from the coverage denominator (managed + drift = resources)', () => {
    // Prod shape (#195): an adoptable Cloud Run card holding DriftScribe's own
    // services (control-plane) alongside 1 managed + 1 adoptable-drift service.
    // The denominator must be managed + actionable drift, so the meter can reach
    // 100% and "N managed of M · K not yet in IaC" reconciles (N + K = M).
    const g = graph({
      totals: { resources: 4, managed: 1, drift: 3 },
      groups: [
        group({
          asset_type: RUN, label: 'Cloud Run service', adoptable: true,
          count: 4, managed: 1, drift: 3, drift_adoptable: 1,
          nodes: [
            node({ id: 'r0', label: 'storefront', asset_type: RUN, managed: true }),
            node({ id: 'r1', label: 'orders-worker', asset_type: RUN, managed: false }),
            node({ id: 'r2', label: 'driftscribe-agent', asset_type: RUN, managed: false, control_plane: true }),
            node({ id: 'r3', label: 'driftscribe-worker', asset_type: RUN, managed: false, control_plane: true }),
          ],
        }),
      ],
    });
    const s = scopeTotals(resourceCards(g), g.totals.resources);
    expect(s.managed).toBe(1);
    expect(s.drift).toBe(1); // actionable only
    expect(s.resources).toBe(2); // 1 managed + 1 adoptable drift — NOT 4
    expect(s.managed + s.drift).toBe(s.resources); // the reconcile invariant
  });

  it('reads the project-wide total from graph.totals.resources, not card sums (Codex MF1)', () => {
    const g = liveSlice();
    // Force a divergence: backend total higher than the sum of group counts
    // (e.g. server-side truncation). The honest "indexed total" is the
    // authoritative backend number.
    const s = scopeTotals(resourceCards(g), 99);
    expect(s.totalResources).toBe(99);
  });

  it('derives outOfScope from the authoritative total minus the scope', () => {
    const g = liveSlice();
    const s = scopeTotals(resourceCards(g), g.totals.resources);
    expect(s.outOfScope).toBe(13); // 15 total − 2 in scope
  });

  it('counts otherResources (Σ other-card counts) and otherTypes', () => {
    const g = liveSlice();
    const s = scopeTotals(resourceCards(g), g.totals.resources);
    expect(s.otherResources).toBe(13); // Revision 10 + Secret 3
    expect(s.otherTypes).toBe(2);
  });

  it('never returns a negative outOfScope', () => {
    const g = liveSlice();
    // Pathological: authoritative total below the in-scope sum → clamp at 0.
    const s = scopeTotals(resourceCards(g), 1);
    expect(s.outOfScope).toBe(0);
  });

  it('is all-zero for a degraded graph', () => {
    const g = graph({ degraded: true, groups: [] });
    const s = scopeTotals(resourceCards(g), g.totals.resources);
    expect(s).toEqual({ resources: 0, managed: 0, drift: 0, totalResources: 0, outOfScope: 0, otherResources: 0, otherTypes: 0 });
  });
});

describe('investigateUnmatchedPrefill', () => {
  function decl(p: Partial<UnmatchedDeclaration> = {}): UnmatchedDeclaration {
    return {
      id: 'u0',
      asset_type: BUCKET,
      type_label: 'Storage bucket',
      label: 'bucket-a',
      address: 'google_storage_bucket.bucket_a',
      ...p,
    };
  }

  it('lists a single same-type unmanaged candidate and pins the intent sentences', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: [node({ id: 'g0n0', label: 'bucket-b', asset_type: BUCKET })],
        }),
      ],
    });
    const p = investigateUnmatchedPrefill(decl(), g);
    expect(p).toContain(
      'Investigate why IaC declares the Storage bucket `bucket-a` (`google_storage_bucket.bucket_a`) but it was not found in the latest Cloud Asset Inventory.',
    );
    expect(p).toContain('Visible unmanaged resources of the same type: `bucket-b`.');
    expect(p).toContain('do not assume a rename, change files, or open a PR');
    expect(p).toContain('ask me to confirm the relationship first');
  });

  it('lists multiple candidates in graph order', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: [
            node({ id: 'a', label: 'bucket-b', asset_type: BUCKET }),
            node({ id: 'b', label: 'bucket-c', asset_type: BUCKET }),
          ],
        }),
      ],
    });
    expect(investigateUnmatchedPrefill(decl(), g)).toContain(
      'Visible unmanaged resources of the same type: `bucket-b`, `bucket-c`.',
    );
  });

  it('says so explicitly when there are no visible candidates', () => {
    const p = investigateUnmatchedPrefill(decl(), graph({ groups: [] }));
    expect(p).toContain('No unmanaged resources of the same type are currently visible.');
    expect(p).not.toContain('Visible unmanaged resources of the same type:');
  });

  it('excludes managed, control-plane, and cross-type nodes', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: [
            node({ id: 'm', label: 'managed-bucket', asset_type: BUCKET, managed: true }),
            node({ id: 'c', label: 'cp-bucket', asset_type: BUCKET, control_plane: true }),
            node({ id: 'ok', label: 'plain-bucket', asset_type: BUCKET }),
          ],
        }),
        group({
          asset_type: RUN,
          label: 'Cloud Run service',
          nodes: [node({ id: 'r', label: 'some-service', asset_type: RUN })],
        }),
      ],
    });
    const p = investigateUnmatchedPrefill(decl(), g);
    expect(p).toContain('Visible unmanaged resources of the same type: `plain-bucket`.');
    expect(p).not.toContain('managed-bucket');
    expect(p).not.toContain('cp-bucket');
    expect(p).not.toContain('some-service');
  });

  it('de-duplicates repeated candidate labels', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: [
            node({ id: 'a', label: 'dup', asset_type: BUCKET }),
            node({ id: 'b', label: 'dup', asset_type: BUCKET }),
          ],
        }),
      ],
    });
    expect(investigateUnmatchedPrefill(decl(), g)).toContain(
      'Visible unmanaged resources of the same type: `dup`.',
    );
  });

  it('caps at five candidates and appends a bounded "(and more may exist)"', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: Array.from({ length: 7 }, (_, i) =>
            node({ id: `n${i}`, label: `b${i}`, asset_type: BUCKET }),
          ),
        }),
      ],
    });
    const p = investigateUnmatchedPrefill(decl(), g);
    expect(p).toContain(
      'Visible unmanaged resources of the same type: `b0`, `b1`, `b2`, `b3`, `b4` (and more may exist).',
    );
    expect(p).not.toContain('`b5`');
    expect(p).not.toContain('`b6`');
  });

  it('normalizes control chars and long names in every fragment', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          nodes: [node({ id: 'a', label: 'na\tme\nb', asset_type: BUCKET })],
        }),
      ],
    });
    const p = investigateUnmatchedPrefill(
      decl({ label: 'de\tcl', type_label: 'Storage\tbucket', address: 'addr\nx' }),
      g,
    );
    expect(p).toContain('Investigate why IaC declares the Storage bucket `de cl` (`addr x`)');
    expect(p).toContain('`na me b`');
  });

  it('omits the address clause when the declaration has none', () => {
    const p = investigateUnmatchedPrefill(decl({ address: undefined }), graph({ groups: [] }));
    expect(p).toContain('Investigate why IaC declares the Storage bucket `bucket-a` but it was not found');
    expect(p).not.toContain('()');
  });
});

describe('unmatched_declarations does not affect existing derivations', () => {
  it('resourceCards, scopeTotals, and adoptRows are identical with/without the field', () => {
    const base = graph({
      totals: { resources: 2, managed: 1, drift: 1 },
      groups: [
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          count: 2,
          managed: 1,
          drift: 1,
          drift_adoptable: 1,
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            node({ id: 'g0n0', label: 'managed-b', asset_type: BUCKET, managed: true }),
            node({ id: 'g0n1', label: 'drift-b', asset_type: BUCKET }),
          ],
        }),
      ],
    });
    const withField: InfraGraph = {
      ...base,
      unmatched_declarations: {
        count: 1,
        entries: [{ id: 'u0', asset_type: BUCKET, type_label: 'Storage bucket', label: 'ghost-b' }],
        truncated: 0,
      },
    };
    expect(resourceCards(withField)).toEqual(resourceCards(base));
    expect(scopeTotals(resourceCards(withField), withField.totals.resources)).toEqual(
      scopeTotals(resourceCards(base), base.totals.resources),
    );
    expect(adoptRows(withField)).toEqual(adoptRows(base));
  });
});
