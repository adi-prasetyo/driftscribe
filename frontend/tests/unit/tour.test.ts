import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  TOUR_DONE_KEY,
  tourDone,
  markTourDone,
  shouldOfferTour,
  TOUR_STEPS,
  welcomeLine,
  estateLine,
  CONTROLS_LINE,
  NEXT_LINE,
  adoptStepState,
} from '../../src/lib/tour';
import type { InfraGraph, InfraGroup, InfraNode } from '../../src/lib/infra_graph';

describe('tour done flag (localStorage)', () => {
  beforeEach(() => window.localStorage.clear());

  it('tourDone is false on a fresh profile', () => {
    expect(tourDone()).toBe(false);
  });

  it('markTourDone persists and tourDone reads it back', () => {
    markTourDone();
    expect(window.localStorage.getItem(TOUR_DONE_KEY)).toBe('1');
    expect(tourDone()).toBe(true);
  });

  it('swallows storage failures (strict privacy modes)', () => {
    const get = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    const set = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    try {
      expect(tourDone()).toBe(false);
      expect(() => markTourDone()).not.toThrow();
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
});

describe('shouldOfferTour', () => {
  it('offers on a clean first visit', () => {
    expect(shouldOfferTour('', false)).toBe(true);
    expect(shouldOfferTour('?other=1', false)).toBe(true);
  });

  it('never offers once done', () => {
    expect(shouldOfferTour('', true)).toBe(false);
  });

  it('suppressed when the operator arrived with intent (?ask_pr / ?preview_pr)', () => {
    expect(shouldOfferTour('?ask_pr=102', false)).toBe(false);
    expect(shouldOfferTour('?preview_pr=7', false)).toBe(false);
  });
});

function makeNode(over: Partial<InfraNode> = {}): InfraNode {
  return {
    id: 'g0n0',
    label: 'demo-bucket',
    asset_type: 'storage.googleapis.com/Bucket',
    managed: false,
    location: 'asia-northeast1',
    ...over,
  };
}

function makeGroup(over: Partial<InfraGroup> = {}): InfraGroup {
  return {
    asset_type: 'storage.googleapis.com/Bucket',
    label: 'Storage bucket',
    count: 1,
    managed: 0,
    drift: 1,
    sensitive: false,
    nodes: [makeNode()],
    adoptable: true,
    ...over,
  };
}

function makeGraph(over: Partial<InfraGraph> = {}): InfraGraph {
  return {
    generated_at: null,
    project: 'driftscribe-hack-2026',
    caveat: 'CAI may lag recent changes.',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 12, managed: 9, drift: 3 },
    groups: [makeGroup()],
    edges: [],
    ...over,
  };
}

describe('TOUR_STEPS', () => {
  it('is the locked 5-step sequence with spotlight targets', () => {
    expect(TOUR_STEPS.map((s) => s.id)).toEqual([
      'welcome',
      'estate',
      'controls',
      'adopt',
      'next',
    ]);
    expect(TOUR_STEPS.map((s) => s.target)).toEqual([
      null,
      'estate',
      'controls',
      'estate',
      'composer',
    ]);
  });
});

describe('step copy', () => {
  it('welcomeLine names the project when known, generic otherwise', () => {
    expect(welcomeLine(makeGraph())).toContain(
      'the GCP project driftscribe-hack-2026',
    );
    expect(welcomeLine(null)).toContain('your GCP project');
    // Honesty T1/T2: approval framing without a blanket safety promise.
    expect(welcomeLine(null)).toContain('wait for your approval');
    expect(welcomeLine(null).toLowerCase()).not.toContain('safe');
  });

  it('welcomeLine introduces the crew honestly — only Anchor is autonomous', () => {
    const line = welcomeLine(null);
    // All four crew identities are named.
    for (const name of ['Anchor', 'Patch', 'Provision', 'Explore']) {
      expect(line).toContain(name);
    }
    // Anchor is the one that runs on its own; the rest wait to be asked.
    expect(line).toContain('Anchor runs on its');
    expect(line).toContain('wait for you to ask');
  });

  it('estateLine renders live totals with coverage percent', () => {
    expect(estateLine(makeGraph())).toBe(
      '12 resources indexed: 9 under IaC management (75%), 3 not yet. ' +
        'The coverage meter below tracks your migration.',
    );
  });

  it('estateLine is honest while loading and when degraded (T3)', () => {
    expect(estateLine(null)).toContain('still loading');
    expect(estateLine(makeGraph({ degraded: true }))).toContain('unavailable');
  });

  it('CONTROLS_LINE scopes the gate claim to infrastructure edits (T2)', () => {
    expect(CONTROLS_LINE).toContain('infrastructure edits pass your explicit approval gate');
    expect(CONTROLS_LINE).toContain('routine dependency updates');
    expect(CONTROLS_LINE).toContain('Pause');
    expect(CONTROLS_LINE.toLowerCase()).not.toContain('safety');
    // Pause now lives in the header pill, not a content card — copy points there.
    expect(CONTROLS_LINE).toContain('top bar');
  });

  it('NEXT_LINE is scoped to THIS request and the review-page gate (T6)', () => {
    expect(NEXT_LINE).toContain('this adopt request');
    expect(NEXT_LINE).toContain('pull request');
    expect(NEXT_LINE).toContain(
      'applied only after you approve it on the review page',
    );
    expect(NEXT_LINE).toContain('Tour button');
    // The old blanket claim must not return — propose_apply may merge
    // dependency PRs on its own (Codex MF1).
    expect(NEXT_LINE).not.toContain('Nothing is applied');
  });
});

describe('adoptStepState', () => {
  it('unavailable while loading or degraded (T3)', () => {
    expect(adoptStepState(null).kind).toBe('unavailable');
    expect(adoptStepState(makeGraph({ degraded: true })).kind).toBe('unavailable');
  });

  it('picks the rank-1 group first (same order as the panel)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'run.googleapis.com/Service',
          label: 'Cloud Run service',
          adopt_rank: 2,
          nodes: [makeNode({ id: 'g0n0', label: 'svc-a', asset_type: 'run.googleapis.com/Service' })],
        }),
        makeGroup({
          adopt_rank: 1,
          adopt_hint: 'Buckets are the simplest first adoption.',
          nodes: [makeNode({ id: 'g1n0', label: 'demo-bucket' })],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('target');
    if (s.kind !== 'target') throw new Error('unreachable');
    expect(s.prefill).toBe(
      'Adopt the Storage bucket `demo-bucket` in asia-northeast1 into IaC management.',
    );
    expect(s.line).toContain('demo-bucket');
    expect(s.line).toContain('Buckets are the simplest first adoption.');
    expect(s.line).toContain('zero-change import');
  });

  it('skips sensitive and non-adoptable groups and managed nodes', () => {
    const g = makeGraph({
      groups: [
        makeGroup({ sensitive: true, nodes: [] }),
        makeGroup({ adoptable: false, label: 'Project' }),
        makeGroup({ nodes: [makeNode({ managed: true })] }),
        makeGroup({ nodes: [makeNode({ id: 'g3n0', label: 'pick-me' })] }),
      ],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('pick-me');
  });

  it('an unranked adoptable group still yields a target, without a hint', () => {
    const g = makeGraph({
      groups: [makeGroup({ adopt_hint: 'should not show — unranked' })],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.line).not.toContain('should not show');
  });

  it('skips nodes with an empty normalized label — no empty-backtick prefill (T7)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          nodes: [
            makeNode({ id: 'g0n0', label: '   ' }), // normalizes to ''
            makeNode({ id: 'g0n1', label: 'named-bucket' }),
          ],
        }),
      ],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('named-bucket');

    // All adoptable nodes unnamed → the copy must say "no named adopt
    // target", NOT "not adoptable types" (Codex round-2 must-fix: an
    // unnamed bucket is still an adoptable type).
    const allUnnamed = adoptStepState(
      makeGraph({ groups: [makeGroup({ nodes: [makeNode({ label: ' ' })] })] }),
    );
    expect(allUnnamed.kind).toBe('none');
    expect(allUnnamed.line).toContain('named adopt target');
    expect(allUnnamed.line).not.toContain('not adoptable types');
  });

  it('all-managed congratulates; non-adoptable leftovers stay honest (T5)', () => {
    const allManaged = adoptStepState(
      makeGraph({
        totals: { resources: 9, managed: 9, drift: 0 },
        groups: [makeGroup({ drift: 0, nodes: [makeNode({ managed: true })] })],
      }),
    );
    expect(allManaged.kind).toBe('none');
    expect(allManaged.line).toContain('already under IaC management');

    const leftovers = adoptStepState(
      makeGraph({ groups: [makeGroup({ adoptable: false })] }),
    );
    expect(leftovers.kind).toBe('none');
    expect(leftovers.line).toContain('not adoptable types.');
    expect(leftovers.line).not.toContain('already under IaC management');
  });

  it('skips control-plane nodes — the live papercut: rank-1 must not be our own bucket', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({ label: 'demo-tofu-artifacts', managed: false, control_plane: true }),
            makeNode({ id: 'n2', label: 'demo-assets', managed: false }),
          ],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') {
      expect(s.prefill).toContain('`demo-assets`');
      expect(s.prefill).not.toContain('tofu-artifacts');
    }
  });

  it('falls through to the NEXT group when a whole group is control-plane', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ label: 'demo-tofu-state', managed: false, control_plane: true })],
        }),
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 2,
          nodes: [makeNode({ id: 'n2', label: 'orders', managed: false })],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') expect(s.prefill).toContain('`orders`');
  });

  it('all-control-plane estate gets the honest denylist line, not the unnamed line', () => {
    const g = makeGraph({
      totals: { resources: 1, managed: 0, drift: 1 },
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ label: 'demo-tofu-artifacts', managed: false, control_plane: true })],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('control-plane');
      expect(s.line).toContain('denylist');
      // honesty: not misdescribed as a naming problem or a type problem
      expect(s.line).not.toContain('named adopt target');
      expect(s.line).not.toContain('not adoptable types');
    }
  });

  it('control-plane plus unnamed still reports the unnamed line (non-CP nodes exist)', () => {
    const g = makeGraph({
      totals: { resources: 2, managed: 0, drift: 2 },
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({ label: 'demo-tofu-state', managed: false, control_plane: true }),
            makeNode({ id: 'n2', label: '   ', managed: false }),
          ],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') expect(s.line).toContain('named adopt target');
  });

  it('skips a Google-service-managed bucket — never prefills _cloudbuild', () => {
    // The same control_plane flag covers service-managed buckets, so the tour
    // must skip demo_cloudbuild exactly as it skips our own -tofu-* buckets.
    const g = makeGraph({
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({ label: 'demo_cloudbuild', managed: false, control_plane: true }),
            makeNode({ id: 'n2', label: 'demo-assets', managed: false }),
          ],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') {
      expect(s.prefill).toContain('`demo-assets`');
      expect(s.prefill).not.toContain('cloudbuild');
    }
  });

  it('all-suppressed line honestly names Google-service-managed buckets too', () => {
    const g = makeGraph({
      totals: { resources: 1, managed: 0, drift: 1 },
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ label: 'demo_cloudbuild', managed: false, control_plane: true })],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('Google service');
      expect(s.line).toContain('denylist');
    }
  });
});
