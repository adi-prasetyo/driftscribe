import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  TOUR_DONE_KEY,
  tourDone,
  markTourDone,
  shouldOfferTour,
  TOUR_STEPS,
  welcomeLine,
  estateLine,
  controlsLine,
  nextLine,
  adoptStepState,
} from '../../src/lib/tour';
import type { InfraGraph, InfraGroup, InfraNode, PendingApproval } from '../../src/lib/infra_graph';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// tour.ts's copy-producing functions take a `t: TranslateFn` (i18n fan-out) —
// thread an en-bound translator so these assertions keep reading the exact
// pinned English (design doc: "Helper-signature tests... thread an en-bound
// t; assertions stay English").
const t: TranslateFn = (key, params) => translate('en', key, params);

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

  it('suppressed when the operator arrived with intent (?ask_pr / ?preview_pr / ?reasoning)', () => {
    expect(shouldOfferTour('?ask_pr=102', false)).toBe(false);
    expect(shouldOfferTour('?preview_pr=7', false)).toBe(false);
    expect(shouldOfferTour('?reasoning=eba334f9211d46cabc79e50ed200a5a1', false)).toBe(false);
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

function makePending(over: Partial<PendingApproval> = {}): PendingApproval {
  return {
    pr_number: 168,
    title: 'Adopt adopt-probe-topic',
    url: 'https://github.com/example/repo/pull/168',
    asset_type: 'pubsub.googleapis.com/Topic',
    resource_name: 'adopt-probe-topic',
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
    expect(welcomeLine(t, makeGraph())).toContain(
      'the GCP project driftscribe-hack-2026',
    );
    expect(welcomeLine(t, null)).toContain('your GCP project');
    // Honesty T1/T2: approval framing without a blanket safety promise.
    expect(welcomeLine(t, null)).toContain('wait for your approval');
    expect(welcomeLine(t, null).toLowerCase()).not.toContain('safe');
  });

  it('welcomeLine introduces the crew honestly — only Anchor is autonomous', () => {
    const line = welcomeLine(t, null);
    // All four crew identities are named.
    for (const name of ['Anchor', 'Patch', 'Provision', 'Explore']) {
      expect(line).toContain(name);
    }
    // Lifecycle-loop framing: the crew reads as a loop, Provision creates,
    // Anchor guards what is live.
    expect(line).toContain('it works as a loop');
    expect(line).toContain('Provision stands infrastructure up');
    expect(line).toContain('Anchor then guards');
    // Anchor is the one that runs on its own; the rest wait to be asked.
    expect(line).toContain('runs on its own, the only crew that does');
    expect(line).toContain('wait for you to ask');
    // Honesty: Anchor REACTS to drift, but remediation stays gated — the copy
    // must never claim it fixes drift on its own.
    expect(line.toLowerCase()).not.toContain('auto-fix');
    expect(line.toLowerCase()).not.toContain('fixes it');
    // Explore is surfaced as the crew you ask to understand the system itself
    // (the read-only crew that carries the whole-system overview).
    expect(line).toContain('how DriftScribe itself works');
  });

  it('estateLine reports SCOPE coverage (matches the panel), not raw totals', () => {
    // 13 indexed: an adoptable Bucket type (3 res, 2 managed, 1 drift) + a
    // non-adoptable Revision type (10, all noise). The line must mirror the
    // panel's scope numbers (2 of 3 · 67%), never "9 of 13".
    const g = makeGraph({
      totals: { resources: 13, managed: 2, drift: 11 },
      groups: [
        makeGroup({
          count: 3,
          managed: 2,
          drift: 1,
          nodes: [
            makeNode({ id: 'b0', label: 'm', managed: true }),
            makeNode({ id: 'b1', label: 'd', managed: false }),
          ],
        }),
        makeGroup({
          asset_type: 'run.googleapis.com/Revision',
          label: 'Revision',
          adoptable: false,
          count: 10,
          managed: 0,
          drift: 10,
          nodes: [makeNode({ id: 'r0', label: 'rev', asset_type: 'run.googleapis.com/Revision', managed: false })],
        }),
      ],
    });
    const line = estateLine(t, g);
    expect(line).toContain('13 resources indexed');
    expect(line).toContain('2 of 3'); // scope managed / scope resources
    expect(line).toContain('(67%)');
    expect(line).toContain('10'); // out-of-scope count
    expect(line).toContain('The coverage meter below tracks your migration.');
    expect(line).not.toContain('9 of 13');
    expect(line).not.toContain('2 of 13');
  });

  it('estateLine omits the out-of-scope sentence when the whole estate is in scope', () => {
    const g = makeGraph({
      totals: { resources: 3, managed: 2, drift: 1 },
      groups: [makeGroup({ count: 3, managed: 2, drift: 1, nodes: [makeNode({ id: 'b0', label: 'm', managed: true })] })],
    });
    const line = estateLine(t, g);
    expect(line).toContain('2 of 3');
    expect(line).not.toMatch(/doesn't manage|does not manage|other/i);
  });

  it('estateLine stays grammatical when NOTHING is in scope (no "The other N")', () => {
    // All indexed resources are types DriftScribe doesn't support → no in-scope
    // group. The old phrasing produced "None of them are supported… The other N",
    // which contradicts itself (Workflow finding).
    const g = makeGraph({
      totals: { resources: 10, managed: 0, drift: 10 },
      groups: [
        makeGroup({
          asset_type: 'run.googleapis.com/Revision',
          label: 'Revision',
          adoptable: false,
          count: 10,
          managed: 0,
          drift: 10,
          nodes: [makeNode({ id: 'r0', label: 'rev', asset_type: 'run.googleapis.com/Revision', managed: false })],
        }),
      ],
    });
    const line = estateLine(t, g);
    expect(line).toContain('10 resources indexed');
    expect(line).toContain('none are in resource types DriftScribe supports');
    expect(line).not.toContain('The other');
    expect(line).toContain('The coverage meter below tracks your migration.');
  });

  it('estateLine is honest while loading and when degraded (T3)', () => {
    expect(estateLine(t, null)).toContain('still loading');
    expect(estateLine(t, makeGraph({ degraded: true }))).toContain('unavailable');
  });

  it('controlsLine scopes the gate claim to infrastructure edits (T2)', () => {
    expect(controlsLine(t)).toContain('infrastructure edits pass your explicit approval gate');
    expect(controlsLine(t)).toContain('routine dependency updates');
    expect(controlsLine(t)).toContain('Pause');
    expect(controlsLine(t).toLowerCase()).not.toContain('safety');
    // Pause now lives in the header pill, not a content card — copy points there.
    expect(controlsLine(t)).toContain('top bar');
    // Reworded for the header-pill redesign: name the visible "Mode control",
    // not a "dial" (the spotlit element is now a compact pill).
    expect(controlsLine(t)).toContain('Mode control');
    expect(controlsLine(t).toLowerCase()).not.toContain('dial');
  });

  it('nextLine is scoped to THIS request and the review-page gate (T6)', () => {
    expect(nextLine(t)).toContain('this adopt request');
    expect(nextLine(t)).toContain('pull request');
    expect(nextLine(t)).toContain(
      'applied only after you approve it on the review page',
    );
    expect(nextLine(t)).toContain('Tour button');
    // The old blanket claim must not return — propose_apply may merge
    // dependency PRs on its own (Codex MF1).
    expect(nextLine(t)).not.toContain('Nothing is applied');
  });
});

describe('adoptStepState', () => {
  it('unavailable while loading or degraded (T3)', () => {
    expect(adoptStepState(t, null).kind).toBe('unavailable');
    expect(adoptStepState(t, makeGraph({ degraded: true })).kind).toBe('unavailable');
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
    const s = adoptStepState(t, g);
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
    const s = adoptStepState(t, g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('pick-me');
  });

  it('an unranked adoptable group still yields a target, without a hint', () => {
    const g = makeGraph({
      groups: [makeGroup({ adopt_hint: 'should not show — unranked' })],
    });
    const s = adoptStepState(t, g);
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
    const s = adoptStepState(t, g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('named-bucket');

    // All adoptable nodes unnamed → the copy must say "no named adopt
    // target", NOT "not adoptable types" (Codex round-2 must-fix: an
    // unnamed bucket is still an adoptable type).
    const allUnnamed = adoptStepState(
      t,
      makeGraph({ groups: [makeGroup({ nodes: [makeNode({ label: ' ' })] })] }),
    );
    expect(allUnnamed.kind).toBe('none');
    expect(allUnnamed.line).toContain('named adopt target');
    expect(allUnnamed.line).not.toContain('not adoptable types');
  });

  it('all-managed congratulates; non-adoptable leftovers stay honest (T5)', () => {
    const allManaged = adoptStepState(
      t,
      makeGraph({
        totals: { resources: 9, managed: 9, drift: 0 },
        groups: [makeGroup({ drift: 0, nodes: [makeNode({ managed: true })] })],
      }),
    );
    expect(allManaged.kind).toBe('none');
    expect(allManaged.line).toContain('already under IaC management');

    const leftovers = adoptStepState(
      t,
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
    const s = adoptStepState(t, g);
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
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') expect(s.prefill).toContain('`orders`');
  });

  it('a subscription target carries its topic and no location clause (no stall from the tour path)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Subscription',
          label: 'Pub/Sub subscription',
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({
              id: 'sub0',
              label: 'adopt-probe-sub',
              asset_type: 'pubsub.googleapis.com/Subscription',
              managed: false,
              location: 'global',
              topic: 'adopt-probe-topic',
            }),
          ],
        }),
      ],
    });
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('target');
    if (s.kind !== 'target') throw new Error('unreachable');
    expect(s.prefill).toBe(
      'Adopt the Pub/Sub subscription `adopt-probe-sub` into IaC management. Its topic is `adopt-probe-topic`.',
    );
    expect(s.prefill).not.toContain(' in global');
  });

  it('a run service target carries its image and keeps the location (no stall from the tour path)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'run.googleapis.com/Service',
          label: 'Cloud Run service',
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({
              id: 'run0',
              label: 'adopt-probe-svc',
              asset_type: 'run.googleapis.com/Service',
              managed: false,
              location: 'asia-northeast1',
              image: 'gcr.io/cloudrun/hello',
            }),
          ],
        }),
      ],
    });
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('target');
    if (s.kind !== 'target') throw new Error('unreachable');
    expect(s.prefill).toBe(
      'Adopt the Cloud Run service `adopt-probe-svc` in asia-northeast1 into IaC management. Its image is `gcr.io/cloudrun/hello`.',
    );
    expect(s.prefill).toContain(' in asia-northeast1');
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
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('control-plane');
      expect(s.line).toContain('denylist');
      // honesty: not misdescribed as a naming problem or a type problem
      expect(s.line).not.toContain('named adopt target');
      expect(s.line).not.toContain('not adoptable types');
    }
  });

  it('reports hidden adoptable drift (aggregate drift_adoptable) beyond the sample, not "system-managed only"', () => {
    // An adoptable Cloud Run group with 11 unmanaged services: 10 control-plane
    // workers + 1 real probe, but the ≤10 sample surfaced only control-plane
    // nodes. drift_adoptable=1 says there IS an adoptable target — the tour must
    // not claim everything left is system-managed.
    const g = makeGraph({
      totals: { resources: 12, managed: 0, drift: 11 },
      groups: [
        makeGroup({
          adoptable: true,
          adopt_rank: 1,
          count: 12,
          managed: 0,
          drift: 11,
          drift_adoptable: 1,
          nodes: [makeNode({ label: 'driftscribe-agent', managed: false, control_plane: true })],
        }),
      ],
    });
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('could adopt');
      expect(s.line).not.toContain('control-plane'); // not the system-managed-only line
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
    const s = adoptStepState(t, g);
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
    const s = adoptStepState(t, g);
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
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('Google service');
      expect(s.line).toContain('denylist');
    }
  });

  // Pending-adoption-PR awareness (the "why does the tour suggest a resource
  // that already has PR #168 open?" gap): the panel and the propose_adoption
  // dupe-guard both know about open adoption PRs, but adoptStepState only saw
  // the graph. A resource with a review-pending PR is still graph-unmanaged
  // (the PR is not merged/applied), so it was still the rank-1 suggestion.
  it('skips a node whose resource already has an open adoption PR, falling to the next group', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ id: 't0', label: 'adopt-probe-topic', asset_type: 'pubsub.googleapis.com/Topic', managed: false })],
        }),
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 2,
          nodes: [makeNode({ id: 't1', label: 'orders', asset_type: 'pubsub.googleapis.com/Topic', managed: false })],
        }),
      ],
    });
    const pending = [makePending({ asset_type: 'pubsub.googleapis.com/Topic', resource_name: 'adopt-probe-topic', pr_number: 168 })];
    const s = adoptStepState(t, g, pending);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') {
      expect(s.prefill).toContain('`orders`');
      expect(s.prefill).not.toContain('adopt-probe-topic');
    }
  });

  it('within a group, picks the sibling without an open PR', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({ id: 't0', label: 'adopt-probe-topic', asset_type: 'pubsub.googleapis.com/Topic', managed: false }),
            makeNode({ id: 't1', label: 'orders', asset_type: 'pubsub.googleapis.com/Topic', managed: false }),
          ],
        }),
      ],
    });
    const pending = [makePending({ asset_type: 'pubsub.googleapis.com/Topic', resource_name: 'adopt-probe-topic', pr_number: 168 })];
    const s = adoptStepState(t, g, pending);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') expect(s.prefill).toContain('`orders`');
  });

  it('when every adoptable target already has an open PR, points at the open change instead of suggesting one', () => {
    const g = makeGraph({
      totals: { resources: 1, managed: 0, drift: 1 },
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ id: 't0', label: 'adopt-probe-topic', asset_type: 'pubsub.googleapis.com/Topic', managed: false })],
        }),
      ],
    });
    const pending = [makePending({ asset_type: 'pubsub.googleapis.com/Topic', resource_name: 'adopt-probe-topic', pr_number: 168 })];
    const s = adoptStepState(t, g, pending);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('already');
      expect(s.line).toContain('Open infra changes');
      // honesty: not misdescribed as a naming/type/system-managed problem
      expect(s.line).not.toContain('named adopt target');
      expect(s.line).not.toContain('not adoptable types');
      expect(s.line).not.toContain('system-managed');
    }
  });

  it('a PR-d named row alongside an UNNAMED actionable row falls through to the no-named-target line, not the all-PR-d line', () => {
    // Honesty guard (Codex 019f4012): the unnamed row is adoptable-but-unnameable,
    // NOT PR'd — claiming "everything already has a PR" would overclaim.
    const g = makeGraph({
      totals: { resources: 2, managed: 0, drift: 2 },
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 1,
          nodes: [
            makeNode({ id: 't0', label: 'adopt-probe-topic', asset_type: 'pubsub.googleapis.com/Topic', managed: false }),
            makeNode({ id: 't1', label: '   ', asset_type: 'pubsub.googleapis.com/Topic', managed: false }), // normalizes to ''
          ],
        }),
      ],
    });
    const pending = [makePending({ asset_type: 'pubsub.googleapis.com/Topic', resource_name: 'adopt-probe-topic', pr_number: 168 })];
    const s = adoptStepState(t, g, pending);
    expect(s.kind).toBe('none');
    if (s.kind === 'none') {
      expect(s.line).toContain('named adopt target');
      expect(s.line).not.toContain('Open infra changes');
    }
  });

  it('without a pending-approvals list, behaves exactly as before (undefined = no filtering)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'pubsub.googleapis.com/Topic',
          label: 'Pub/Sub topic',
          adoptable: true,
          adopt_rank: 1,
          nodes: [makeNode({ id: 't0', label: 'adopt-probe-topic', asset_type: 'pubsub.googleapis.com/Topic', managed: false })],
        }),
      ],
    });
    const s = adoptStepState(t, g);
    expect(s.kind).toBe('target');
    if (s.kind === 'target') expect(s.prefill).toContain('`adopt-probe-topic`');
  });
});
