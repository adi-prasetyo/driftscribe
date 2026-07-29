import { describe, it, expect } from 'vitest';
import { estateModel, firstAdoptableRow } from '../../src/lib/estate';
import type { InfraGraph, InfraGroup, InfraNode, PendingApproval } from '../../src/lib/infra_graph';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// estateModel() re-groups resourceCards()'s own output (single source of
// truth: lib/infra_graph.ts) — thread an en-bound translator so assertions
// stay pinned English, matching tour.test.ts's own convention.
const t: TranslateFn = (key, params) => translate('en', key, params);

const BUCKET = 'storage.googleapis.com/Bucket';
const RUN = 'run.googleapis.com/Service';
const REVISION = 'run.googleapis.com/Revision';
const SA = 'iam.googleapis.com/ServiceAccount';

function node(over: Partial<InfraNode> = {}): InfraNode {
  return {
    id: 'n0',
    label: 'demo-node',
    asset_type: BUCKET,
    managed: false,
    location: 'asia-northeast1',
    ...over,
  };
}

function group(over: Partial<InfraGroup> = {}): InfraGroup {
  return {
    asset_type: BUCKET,
    label: 'Storage bucket',
    count: 1,
    managed: 0,
    drift: 1,
    sensitive: false,
    adoptable: true,
    nodes: [node()],
    ...over,
  };
}

function graph(over: Partial<InfraGraph> = {}): InfraGraph {
  return {
    generated_at: '2026-07-28T06:00:00Z',
    project: 'demo-proj',
    caveat: '',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 10, managed: 5, drift: 5 },
    groups: [group()],
    edges: [],
    ...over,
  };
}

function pending(over: Partial<PendingApproval> = {}): PendingApproval {
  return {
    pr_number: 168,
    title: 'Adopt demo-node',
    url: 'https://github.com/x/y/pull/168',
    asset_type: BUCKET,
    resource_name: 'demo-node',
    ...over,
  };
}

const EMPTY = {
  drift: [],
  driftHidden: 0,
  managed: [],
  untracked: [],
  systemManaged: [],
  systemManagedTotal: 0,
  otherTypes: 0,
  otherResources: 0,
};

describe('estateModel — loading/degraded honesty (never a fake-empty "all clear")', () => {
  it('graph === null yields the empty model', () => {
    expect(estateModel(null, [], t)).toEqual(EMPTY);
  });

  it('a degraded graph yields the empty model', () => {
    const m = estateModel(graph({ degraded: true }), [], t);
    expect(m).toEqual(EMPTY);
  });
});

describe('estateModel — status partitioning, flattened across types', () => {
  it('splits managed / drift / untracked / systemManaged rows by status, preserving resourceCards()\'s drift-first + adopt-rank order within `drift`', () => {
    const g = graph({
      totals: { resources: 40, managed: 3, drift: 2 },
      groups: [
        group({
          asset_type: RUN,
          label: 'Cloud Run',
          count: 3,
          managed: 1,
          drift: 2,
          adoptable: true,
          adopt_rank: 2, // ranked AFTER the bucket group below
          drift_adoptable: 1, // only `storefront` is actionable; driftscribe-agent is control-plane
          nodes: [
            node({ id: 'r0', label: 'checkout', asset_type: RUN, managed: true }),
            node({ id: 'r1', label: 'storefront', asset_type: RUN, managed: false }),
            node({
              id: 'r2',
              label: 'driftscribe-agent',
              asset_type: RUN,
              managed: false,
              control_plane: true,
            }),
          ],
        }),
        group({
          asset_type: BUCKET,
          label: 'Storage bucket',
          count: 2,
          managed: 1,
          drift: 1,
          adoptable: true,
          adopt_rank: 1, // ranked FIRST — its drift row must sort before Cloud Run's
          nodes: [
            node({ id: 'b0', label: 'prod-state', asset_type: BUCKET, managed: true }),
            node({ id: 'b1', label: 'my-old-uploads', asset_type: BUCKET, managed: false }),
          ],
        }),
        group({
          asset_type: SA,
          label: 'Service account',
          count: 2,
          managed: 1,
          drift: 1,
          adoptable: false, // not an adoptable type, but managed>0 keeps the card PRIMARY
          nodes: [
            node({ id: 's0', label: 'mgd@p', asset_type: SA, managed: true }),
            node({ id: 's1', label: 'drift@p', asset_type: SA, managed: false }),
          ],
        }),
      ],
    });
    const m = estateModel(g, [], t);

    expect(m.drift.map((r) => r.label)).toEqual(['my-old-uploads', 'storefront']);
    expect(m.drift.every((r) => r.status === 'drift' && r.adoptable && r.prefill !== '')).toBe(true);

    expect(m.managed.map((r) => r.label).sort()).toEqual(['checkout', 'mgd@p', 'prod-state']);
    expect(m.managed.every((r) => r.status === 'managed' && !r.adoptable && r.prefill === '')).toBe(
      true,
    );

    expect(m.untracked.map((r) => r.label)).toEqual(['drift@p']);
    expect(m.untracked[0].status).toBe('untracked');
    expect(m.untracked[0].adoptable).toBe(false);
    expect(m.untracked[0].prefill).toBe('');

    expect(m.systemManaged.map((r) => r.label)).toEqual(['driftscribe-agent']);
    expect(m.systemManagedTotal).toBe(1);
  });

  it('driftHidden sums card.hiddenUnmanaged across primary cards — no client-invented re-cap', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          managed: 0,
          drift: 3,
          drift_adoptable: 3, // 3 actionable, but only 1 node sampled server-side
          nodes: [node({ id: 'b0', label: 'sampled-one', managed: false })],
        }),
      ],
    });
    const m = estateModel(g, [], t);
    expect(m.drift).toHaveLength(1);
    expect(m.driftHidden).toBe(2);
  });

  it('sums otherTypes/otherResources from splitCards().other; those rows never leak into drift/managed/untracked', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          managed: 1,
          drift: 0,
          nodes: [node({ id: 'b0', label: 'prod-state', managed: true })],
        }),
        group({
          asset_type: REVISION,
          label: 'Cloud Run revision',
          count: 2,
          managed: 0,
          drift: 2,
          adoptable: false, // not adoptable AND managed===0 → OTHER, not primary
          nodes: [
            node({ id: 'v0', label: 'demo-00018-abc', asset_type: REVISION, managed: false }),
            node({ id: 'v1', label: 'demo-00017-abc', asset_type: REVISION, managed: false }),
          ],
        }),
      ],
    });
    const m = estateModel(g, [], t);
    expect(m.otherTypes).toBe(1);
    expect(m.otherResources).toBe(2);
    expect(m.drift).toEqual([]);
    expect(m.untracked).toEqual([]);
  });
});

describe('estateModel — pendingPr join', () => {
  it('joins pendingPr onto a drift row via asset_type + short name (findPendingPr)', () => {
    const g = graph({
      groups: [
        group({
          asset_type: BUCKET,
          managed: 0,
          drift: 1,
          nodes: [node({ id: 'b0', label: 'shipping-topic', managed: false })],
        }),
      ],
    });
    const approvals = [pending({ asset_type: BUCKET, resource_name: 'shipping-topic', pr_number: 268 })];
    const m = estateModel(g, approvals, t);
    expect(m.drift[0].pendingPr).toBe(268);
  });

  it('a drift row with no matching open PR gets pendingPr: null', () => {
    const m = estateModel(graph(), [pending({ resource_name: 'someone-else-entirely' })], t);
    expect(m.drift[0].pendingPr).toBeNull();
  });

  it('a null/undefined element in the approvals array is skipped, not thrown on (open externally-sourced payload)', () => {
    const approvals: Array<PendingApproval | null | undefined> = [
      null,
      undefined,
      pending({ resource_name: 'demo-node', pr_number: 7 }),
    ];
    const m = estateModel(graph(), approvals, t);
    expect(m.drift[0].pendingPr).toBe(7);
  });
});

describe('firstAdoptableRow', () => {
  it('returns the first drift row that is adoptable and carries no open PR', () => {
    const g = graph({
      groups: [
        group({ asset_type: BUCKET, adopt_rank: 1, nodes: [node({ id: 'b0', label: 'bucket-a' })] }),
        group({
          asset_type: RUN,
          adopt_rank: 2,
          nodes: [node({ id: 'r0', label: 'run-a', asset_type: RUN })],
        }),
      ],
    });
    const m = estateModel(g, [], t);
    expect(firstAdoptableRow(m)?.label).toBe('bucket-a');
  });

  it('skips a row that already has an open adoption PR, falling through to the next adoptable row', () => {
    const g = graph({
      groups: [
        group({ asset_type: BUCKET, adopt_rank: 1, nodes: [node({ id: 'b0', label: 'bucket-a' })] }),
        group({
          asset_type: RUN,
          adopt_rank: 2,
          nodes: [node({ id: 'r0', label: 'run-a', asset_type: RUN })],
        }),
      ],
    });
    const approvals = [pending({ asset_type: BUCKET, resource_name: 'bucket-a', pr_number: 9 })];
    const m = estateModel(g, approvals, t);
    expect(firstAdoptableRow(m)?.label).toBe('run-a');
  });

  it('returns null when there is no drift at all', () => {
    const m = estateModel(graph({ groups: [] }), [], t);
    expect(firstAdoptableRow(m)).toBeNull();
  });

  it('returns null when every drift row is PR\'d (falls through the whole list)', () => {
    const g = graph({
      groups: [group({ asset_type: BUCKET, nodes: [node({ id: 'b0', label: 'bucket-a' })] })],
    });
    const approvals = [pending({ asset_type: BUCKET, resource_name: 'bucket-a', pr_number: 9 })];
    expect(firstAdoptableRow(estateModel(g, approvals, t))).toBeNull();
  });
});
