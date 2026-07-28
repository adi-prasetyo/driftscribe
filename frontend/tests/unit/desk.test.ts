import { describe, it, expect } from 'vitest';
import { deskModel, STAMP_WINDOW_MS } from '../../src/lib/desk';
import type { Decision } from '../../src/lib/types';
import type { PendingApproval } from '../../src/lib/infra_graph';

// deskModel (Task 3.1) is the pure selection logic behind the three-state
// approval desk: pending / stamped / resting. It is fully time/origin/locale
// injectable (see lib/desk.ts's header) so every expiry/decay boundary here
// is deterministic — no real clock, no fake timers, no window access.

const ORIGIN = 'https://coordinator.example';
const NOW = Date.parse('2026-07-28T12:00:00Z');

function rollbackDecision(
  overrides: Partial<Decision> & { approval?: Decision['approval'] } = {},
): Decision {
  return {
    decision_id: 'rb-1',
    action: 'rollback',
    created_at: '2026-07-28T11:00:00Z',
    approval: {
      approval_url: '/approvals/rb-1?t=abc',
      expires_at: '2026-07-28T23:00:00Z',
      status: 'pending',
      resolved_at: null,
    },
    ...overrides,
  };
}

function iacDecision(overrides: Partial<Decision> = {}): Decision {
  return {
    decision_id: 'iac-1',
    action: 'iac_apply',
    created_at: '2026-07-28T11:00:00Z',
    pr_number: 42,
    apply_status: 'waiting_for_rebake',
    ...overrides,
  };
}

function pendingIac(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    pr_number: 7,
    title: 'Adopt bucket',
    url: 'https://github.com/x/y/pull/7',
    asset_type: 'storage.googleapis.com/Bucket',
    resource_name: 'b1',
    ...overrides,
  };
}

describe('deskModel — rule 1: pending rollback', () => {
  it('selects a rollback decision with a pending approval', () => {
    const d = rollbackDecision();
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'rollback') {
      expect(model.href).toBe('/approvals/rb-1?t=abc');
      expect(model.decision).toBe(d);
    } else {
      throw new Error('expected a pending rollback');
    }
  });

  it('absent approval.status is treated as pending (pre-enrichment compatibility)', () => {
    const d = rollbackDecision({
      approval: { approval_url: '/approvals/rb-2?t=x', expires_at: null },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('pending');
  });

  it('denied status is NOT pending and NOT stamped — falls through to resting', () => {
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-3?t=x',
        status: 'denied',
        resolved_at: '2026-07-28T11:59:00Z',
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('an expired approval is ignored (falls through to resting when nothing else qualifies)', () => {
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-4?t=x',
        status: 'pending',
        expires_at: '2026-07-28T11:00:00Z', // in the past relative to NOW
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('an unsafe/off-origin approval_url is skipped, falling through to the next candidate', () => {
    const bad = rollbackDecision({
      decision_id: 'rb-bad',
      approval: { approval_url: 'https://evil.example/approvals/x', status: 'pending' },
    });
    const good = rollbackDecision({
      decision_id: 'rb-good',
      created_at: '2026-07-28T10:00:00Z',
      approval: { approval_url: '/approvals/rb-good?t=1', status: 'pending' },
    });
    const model = deskModel({
      decisions: [bad, good],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'rollback') {
      expect(model.decision.decision_id).toBe('rb-good');
    } else {
      throw new Error('expected the safe rollback to win');
    }
  });

  it('when every candidate is unsafe, resting (no dead CTA is ever rendered)', () => {
    const bad = rollbackDecision({
      approval: { approval_url: 'javascript:alert(1)', status: 'pending' },
    });
    const model = deskModel({ decisions: [bad], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('picks the NEWEST pending rollback by created_at when several qualify', () => {
    const older = rollbackDecision({
      decision_id: 'rb-old',
      created_at: '2026-07-28T09:00:00Z',
      approval: { approval_url: '/approvals/rb-old?t=1', status: 'pending' },
    });
    const newer = rollbackDecision({
      decision_id: 'rb-new',
      created_at: '2026-07-28T11:30:00Z',
      approval: { approval_url: '/approvals/rb-new?t=1', status: 'pending' },
    });
    const model = deskModel({
      decisions: [older, newer],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'rollback') {
      expect(model.decision.decision_id).toBe('rb-new');
    } else {
      throw new Error('expected the newer rollback to win');
    }
  });
});

describe('deskModel — rule 2: pending iac approval', () => {
  it('selects the first item of the pending-approvals payload when no rollback is pending', () => {
    const items = [pendingIac({ pr_number: 7 }), pendingIac({ pr_number: 9 })];
    const model = deskModel({
      decisions: [],
      pendingApprovals: items,
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.href).toBe('/iac-approvals/7');
      expect(model.approval).toBe(items[0]);
    } else {
      throw new Error('expected a pending iac approval');
    }
  });

  it('a pending rollback beats a pending iac approval', () => {
    const rb = rollbackDecision();
    const items = [pendingIac()];
    const model = deskModel({
      decisions: [rb],
      pendingApprovals: items,
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending') expect(model.source).toBe('rollback');
  });
});

describe('deskModel — rule 3: stamped', () => {
  it('a recently-applied iac decision stamps the desk (nothing pending)', () => {
    const d = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T11:55:00Z' });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') {
      expect(model.source).toBe('iac');
      expect(model.stampedUntil).toBe(Date.parse('2026-07-28T11:55:00Z') + STAMP_WINDOW_MS);
      expect(model.decision).toBe(d);
    }
  });

  it('a recently-resolved (used) rollback approval stamps the desk', () => {
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        resolved_at: '2026-07-28T11:58:00Z',
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') {
      expect(model.source).toBe('rollback');
      expect(model.stampedUntil).toBe(Date.parse('2026-07-28T11:58:00Z') + STAMP_WINDOW_MS);
    }
  });

  it('stamping keys off applied_at, NOT created_at (merge-only reconcile rewrites created_at)', () => {
    // created_at is fresh (looks "just happened") but applied_at is stale —
    // must NOT stamp on created_at's recency.
    const d = iacDecision({
      apply_status: 'applied',
      created_at: '2026-07-28T11:59:00Z',
      applied_at: '2026-07-28T01:00:00Z',
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('resolved_at: null on a used approval is NOT stampable (never falls back to created_at)', () => {
    const d = rollbackDecision({
      created_at: '2026-07-28T11:59:00Z',
      approval: { approval_url: '/approvals/rb-1?t=x', status: 'used', resolved_at: null },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('anything pending wins even when a stamped candidate also exists', () => {
    const stampedIac = iacDecision({
      decision_id: 'iac-done',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:58:00Z',
    });
    const pendingRb = rollbackDecision({ decision_id: 'rb-pending' });
    const model = deskModel({
      decisions: [stampedIac, pendingRb],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
  });

  it('decay boundary: just inside the 10-minute window is stamped', () => {
    const appliedAt = NOW - STAMP_WINDOW_MS + 1; // 1ms before the boundary
    const d = iacDecision({ apply_status: 'applied', applied_at: new Date(appliedAt).toISOString() });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('stamped');
  });

  it('decay boundary: just outside the 10-minute window is resting', () => {
    const appliedAt = NOW - STAMP_WINDOW_MS - 1; // 1ms past the boundary
    const d = iacDecision({ apply_status: 'applied', applied_at: new Date(appliedAt).toISOString() });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('decay boundary: exactly at stampedUntil is still stamped (inclusive)', () => {
    const appliedAt = NOW - STAMP_WINDOW_MS;
    const d = iacDecision({ apply_status: 'applied', applied_at: new Date(appliedAt).toISOString() });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('stamped');
  });

  it('an applied row NOT gated to action iac_apply never stamps (mirrors resolvedIacPrNumbers)', () => {
    const d = iacDecision({
      action: 'drift_issue',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:58:00Z',
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('when both an iac-applied and a rollback-used candidate are within window, the more recent one wins', () => {
    const iac = iacDecision({
      decision_id: 'iac-recent',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const rb = rollbackDecision({
      decision_id: 'rb-older',
      approval: {
        approval_url: '/approvals/rb-older?t=x',
        status: 'used',
        resolved_at: '2026-07-28T11:50:00Z',
      },
    });
    const model = deskModel({
      decisions: [iac, rb],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') expect(model.source).toBe('iac');
  });

  it('picks the newest applied iac row (by applied_at) among several applied rows', () => {
    const stale = iacDecision({
      decision_id: 'iac-stale',
      apply_status: 'applied',
      applied_at: '2026-07-28T01:00:00Z', // long outside the window
    });
    const fresh = iacDecision({
      decision_id: 'iac-fresh',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [stale, fresh],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') expect(model.decision.decision_id).toBe('iac-fresh');
  });
});

describe('deskModel — rule 4: resting', () => {
  it('resting when there is nothing pending and nothing recently stamped', () => {
    const model = deskModel({ decisions: [], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model).toEqual({ kind: 'resting' });
  });

  it('tolerates null/undefined decisions and pendingApprovals (resting, not a crash)', () => {
    const model = deskModel({
      decisions: null as unknown as Decision[],
      pendingApprovals: undefined as unknown as PendingApproval[],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model).toEqual({ kind: 'resting' });
  });
});

describe('deskModel — locale pass-through', () => {
  it('passes locale through to safeApprovalHref for a pending rollback', () => {
    const d = rollbackDecision();
    const model = deskModel({
      decisions: [d],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
      locale: 'ja',
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending') expect(model.href).toBe('/approvals/rb-1?t=abc&lang=ja');
  });

  it('passes locale through to iacApprovalHref for a pending iac approval', () => {
    const model = deskModel({
      decisions: [],
      pendingApprovals: [pendingIac({ pr_number: 12 })],
      now: NOW,
      origin: ORIGIN,
      locale: 'ja',
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending') expect(model.href).toBe('/iac-approvals/12?lang=ja');
  });
});
