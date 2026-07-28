import { describe, it, expect } from 'vitest';
import { deskModel, awaitingCount, STAMP_WINDOW_MS } from '../../src/lib/desk';
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

  it('on an exact created_at tie, the first-encountered candidate wins (deterministic, not last-write-wins)', () => {
    // Quality review finding #3: `ts > best.ts` (strict greater-than) means a
    // later element with an EQUAL timestamp never displaces the earlier one.
    // Pin that explicitly so a future `>=` "simplification" is caught.
    const first = rollbackDecision({
      decision_id: 'rb-first',
      created_at: '2026-07-28T11:00:00Z',
      approval: { approval_url: '/approvals/rb-first?t=1', status: 'pending' },
    });
    const second = rollbackDecision({
      decision_id: 'rb-second',
      created_at: '2026-07-28T11:00:00Z', // exact tie with `first`
      approval: { approval_url: '/approvals/rb-second?t=1', status: 'pending' },
    });
    const model = deskModel({
      decisions: [first, second],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'rollback') {
      expect(model.decision.decision_id).toBe('rb-first');
    } else {
      throw new Error('expected the first-encountered candidate to win the tie');
    }
  });

  it('a missing/unparseable created_at still qualifies when it is the only candidate, but never displaces a dated rival', () => {
    const undated = rollbackDecision({
      decision_id: 'rb-undated',
      created_at: undefined,
      approval: { approval_url: '/approvals/rb-undated?t=1', status: 'pending' },
    });
    const solo = deskModel({ decisions: [undated], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(solo.kind).toBe('pending');
    if (solo.kind === 'pending' && solo.source === 'rollback') {
      expect(solo.decision.decision_id).toBe('rb-undated');
    }

    const dated = rollbackDecision({
      decision_id: 'rb-dated',
      created_at: '2026-01-01T00:00:00Z', // far older in calendar time than "undated" would sort
      approval: { approval_url: '/approvals/rb-dated?t=1', status: 'pending' },
    });
    // Order shouldn't matter — the undated row sorts as -Infinity either way.
    const withRival = deskModel({
      decisions: [undated, dated],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(withRival.kind).toBe('pending');
    if (withRival.kind === 'pending' && withRival.source === 'rollback') {
      expect(withRival.decision.decision_id).toBe('rb-dated');
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
      expect(model.prNumber).toBe(7);
      expect(model.provenance).toEqual({ kind: 'listing', approval: items[0] });
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

  it('a malformed first item (bad pr_number, dead href) is skipped, falling through to the next entry', () => {
    // Defensive fallthrough, not part of the literal "first item" spec text —
    // PendingApproval.pr_number is typed as a real number, so this only bites
    // a malformed backend row. Still must never render a dead CTA.
    const items = [pendingIac({ pr_number: 0 }), pendingIac({ pr_number: 9 })];
    const model = deskModel({
      decisions: [],
      pendingApprovals: items,
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.href).toBe('/iac-approvals/9');
      expect(model.prNumber).toBe(9);
      expect(model.provenance).toEqual({ kind: 'listing', approval: items[1] });
    } else {
      throw new Error('expected the second (valid) iac approval to win');
    }
  });
});

describe('deskModel — rule 2b: pending iac approval derived from decisions (open-PR-listing gap)', () => {
  // agent/main.py's `_list_pending_approvals()` is `state="open"` GitHub
  // issues — a MERGED PR drops out of it forever, even though it can still
  // genuinely need the operator's post-merge, post-rebake Apply (the rail's
  // own `iacApproveLabel` still calls a non-superseded `waiting_for_rebake`
  // row "Review & approve →"). This is the gap rule 2b closes: the desk must
  // reach the same "still awaiting you" verdict as the rail even when the
  // open-PR listing has nothing to say about it.
  it('a merged, non-superseded waiting_for_rebake decision is PENDING even with an empty pendingApprovals array', () => {
    const d = iacDecision(); // apply_status: 'waiting_for_rebake', pr_number: 42 (see helper default)
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.prNumber).toBe(42);
      expect(model.href).toBe('/iac-approvals/42');
      expect(model.provenance).toEqual({ kind: 'decision', decision: d });
    } else {
      throw new Error('expected a decisions-derived pending iac approval, not resting');
    }
  });

  it('explicit superseded_by_pr → NOT pending (falls through to resting when nothing else qualifies)', () => {
    const d = iacDecision({ superseded_by_pr: 43 });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('a later applied row for the same PR (resolvedIacPrNumbers) → NOT pending', () => {
    const waiting = iacDecision({ decision_id: 'iac-waiting', pr_number: 44, created_at: '2026-07-28T10:00:00Z' });
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 44,
      apply_status: 'applied',
      applied_at: '2026-07-28T10:30:00Z',
      created_at: '2026-07-28T11:00:00Z',
    });
    const model = deskModel({
      decisions: [waiting, applied],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    // The applied row IS a valid stamp candidate — but it must not ALSO leave
    // the superseded waiting row pending. Assert it isn't pending at all.
    expect(model.kind).not.toBe('pending');
  });

  it('dedup: the same PR present in BOTH the listing and the decisions log yields exactly one desk state, from the listing', () => {
    const listingItem = pendingIac({ pr_number: 42, title: 'From the open-PR listing' });
    const decisionItem = iacDecision({ pr_number: 42 });
    const model = deskModel({
      decisions: [decisionItem],
      pendingApprovals: [listingItem],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.prNumber).toBe(42);
      // provenance proves it came from the LISTING (carries the title), not
      // the decisions-derived fallback (which only carries the decision doc).
      expect(model.provenance).toEqual({ kind: 'listing', approval: listingItem });
    } else {
      throw new Error('expected a pending iac approval sourced from the listing');
    }
  });

  it('picks the NEWEST decisions-derived candidate by created_at when several qualify', () => {
    const older = iacDecision({ decision_id: 'iac-old', pr_number: 50, created_at: '2026-07-28T09:00:00Z' });
    const newer = iacDecision({ decision_id: 'iac-new', pr_number: 51, created_at: '2026-07-28T11:00:00Z' });
    const model = deskModel({
      decisions: [older, newer],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.prNumber).toBe(51);
    } else {
      throw new Error('expected the newer decisions-derived candidate to win');
    }
  });

  it('a pending rollback still beats a decisions-derived pending iac approval', () => {
    const rb = rollbackDecision();
    const iac = iacDecision({ decision_id: 'iac-other' });
    const model = deskModel({
      decisions: [rb, iac],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending') expect(model.source).toBe('rollback');
  });

  it('a decisions-derived pending iac approval still beats a stamped candidate', () => {
    const stampedIac = iacDecision({
      decision_id: 'iac-done',
      pr_number: 60,
      apply_status: 'applied',
      applied_at: '2026-07-28T11:58:00Z',
    });
    const awaitingIac = iacDecision({ decision_id: 'iac-waiting', pr_number: 61 });
    const model = deskModel({
      decisions: [stampedIac, awaitingIac],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') expect(model.prNumber).toBe(61);
  });

  // A null-element test for THIS loop specifically turned out toothless:
  // isIacAwaitingOperator (approval.ts) already null-tolerates internally
  // (it has to — ledger.ts's classify() passes decisions straight from the
  // same externally-sourced array with no upstream filtering), so removing
  // desk.ts's own `if (decision == null) continue` guard here does not
  // reproduce a failure — the predicate already returns false for null
  // before this loop's body would do anything unsafe with it. See
  // approval.test.ts's `isIacAwaitingOperator` suite for the genuine,
  // fail-first-verified null/undefined coverage instead.
  it('a null element in decisions does not stop a valid decisions-derived candidate later in the array from winning', () => {
    const good = iacDecision({ decision_id: 'iac-good', pr_number: 62 });
    const model = deskModel({
      decisions: [null, undefined, good] as unknown as Decision[],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') expect(model.prNumber).toBe(62);
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

  it('mirror: when the rollback resolution is the more recent of the two, the rollback wins', () => {
    // Same setup as the previous test with the timestamps swapped — pins that
    // the tiebreak is genuinely recency-based, not "iac always wins" (which
    // would pass the previous test too).
    const iac = iacDecision({
      decision_id: 'iac-older',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:50:00Z',
    });
    const rb = rollbackDecision({
      decision_id: 'rb-recent',
      approval: {
        approval_url: '/approvals/rb-recent?t=x',
        status: 'used',
        resolved_at: '2026-07-28T11:59:00Z',
      },
    });
    const model = deskModel({
      decisions: [iac, rb],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') {
      expect(model.source).toBe('rollback');
      expect(model.decision.decision_id).toBe('rb-recent');
    }
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

describe('deskModel — defensive: malformed array elements', () => {
  // Quality review finding #1: /decisions and /infra/pending-approvals are
  // both open, externally-sourced arrays (cast from JSON.parse output — see
  // overviewStore.ts's `as Decision[]` / `as PendingApproval[]`), so a
  // null/undefined element is a realistic runtime shape even though today's
  // backend never emits one. A null element must be skipped, never thrown on
  // — this function is the app's front door, and a throw here blanks the
  // whole desk. Mirrors resolvedIacPrNumbers's tolerance of null/undefined
  // entries on the same Decision[] shape (approval.ts:194).

  it('a null element in decisions is skipped (rule 1) — a valid rollback later in the array still wins', () => {
    const good = rollbackDecision({
      decision_id: 'rb-good',
      approval: { approval_url: '/approvals/rb-good?t=1', status: 'pending' },
    });
    const model = deskModel({
      decisions: [null, undefined, good] as unknown as Decision[],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'rollback') {
      expect(model.decision.decision_id).toBe('rb-good');
    } else {
      throw new Error('expected the valid rollback to win despite the null/undefined elements');
    }
  });

  it('a null element in pendingApprovals is skipped (rule 2) — a valid entry later in the array still wins', () => {
    const good = pendingIac({ pr_number: 11 });
    const model = deskModel({
      decisions: [],
      pendingApprovals: [null, undefined, good] as unknown as PendingApproval[],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.href).toBe('/iac-approvals/11');
      expect(model.prNumber).toBe(11);
      expect(model.provenance).toEqual({ kind: 'listing', approval: good });
    } else {
      throw new Error('expected the valid pending-approval entry to win despite the null/undefined elements');
    }
  });

  it('a null element in decisions is skipped (rule 3) — a valid applied iac row later in the array still stamps', () => {
    const applied = iacDecision({
      decision_id: 'iac-good',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:58:00Z',
    });
    const model = deskModel({
      decisions: [null, undefined, applied] as unknown as Decision[],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') expect(model.decision.decision_id).toBe('iac-good');
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

describe('awaitingCount — the InstrumentBand "awaiting your approval" figure', () => {
  it('zero when nothing is awaiting', () => {
    expect(awaitingCount({ decisions: [], pendingApprovals: [], now: NOW, origin: ORIGIN })).toBe(0);
  });

  it('both lanes contribute: one pending rollback + one pending iac (from the listing) = 2', () => {
    const rb = rollbackDecision();
    const iac = pendingIac({ pr_number: 7 });
    const n = awaitingCount({ decisions: [rb], pendingApprovals: [iac], now: NOW, origin: ORIGIN });
    expect(n).toBe(2);
  });

  it('several awaiting rollbacks all count (not clamped to 1 the way deskModel picks only the newest)', () => {
    const a = rollbackDecision({
      decision_id: 'rb-a',
      approval: { approval_url: '/approvals/rb-a?t=1', status: 'pending' },
    });
    const b = rollbackDecision({
      decision_id: 'rb-b',
      approval: { approval_url: '/approvals/rb-b?t=1', status: 'pending' },
    });
    expect(awaitingCount({ decisions: [a, b], pendingApprovals: [], now: NOW, origin: ORIGIN })).toBe(2);
  });

  it('several distinct pending iac PRs all count', () => {
    const items = [pendingIac({ pr_number: 7 }), pendingIac({ pr_number: 9 })];
    expect(awaitingCount({ decisions: [], pendingApprovals: items, now: NOW, origin: ORIGIN })).toBe(2);
  });

  it('the SAME PR present in both the listing AND the decisions-derived lane counts ONCE, not twice', () => {
    const listingItem = pendingIac({ pr_number: 42 });
    const decisionItem = iacDecision({ pr_number: 42 }); // waiting_for_rebake, same PR
    const n = awaitingCount({
      decisions: [decisionItem],
      pendingApprovals: [listingItem],
      now: NOW,
      origin: ORIGIN,
    });
    expect(n).toBe(1);
  });

  it('a decision NOT awaiting (already applied) never inflates the iac count', () => {
    const applied = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T11:00:00Z' });
    expect(awaitingCount({ decisions: [applied], pendingApprovals: [], now: NOW, origin: ORIGIN })).toBe(0);
  });

  it('an expired rollback approval never inflates the rollback count (mirrors isRollbackAwaitingOperator)', () => {
    const expired = rollbackDecision({
      approval: { approval_url: '/approvals/rb-1?t=x', status: 'pending', expires_at: '2026-07-28T11:00:00Z' },
    });
    expect(awaitingCount({ decisions: [expired], pendingApprovals: [], now: NOW, origin: ORIGIN })).toBe(0);
  });

  // The test that pins the actual bug this function exists to fix: the
  // desk's OWN state (deskModel) shows exactly ONE pending card at a time —
  // that must never be mistaken for "only one thing is awaiting". Here two
  // genuinely-independent items are awaiting (a rollback AND a distinct iac
  // PR); deskModel's rule 1 picks the rollback as the single pending card,
  // but awaitingCount must still honestly report 2.
  it('desk shows a single pending card while awaitingCount reports 2 — the numbers are NOT the same fact', () => {
    const rb = rollbackDecision();
    const iac = pendingIac({ pr_number: 7 });
    const model = deskModel({ decisions: [rb], pendingApprovals: [iac], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending') expect(model.source).toBe('rollback'); // rule 1 wins — only ONE card shown

    const n = awaitingCount({ decisions: [rb], pendingApprovals: [iac], now: NOW, origin: ORIGIN });
    expect(n).toBeGreaterThan(1);
    expect(n).toBe(2);
  });

  it('malformed/null array elements are skipped, not thrown on', () => {
    const good = rollbackDecision();
    const n = awaitingCount({
      decisions: [null, undefined, good] as unknown as Decision[],
      pendingApprovals: [null, undefined, pendingIac({ pr_number: 3 })] as unknown as PendingApproval[],
      now: NOW,
      origin: ORIGIN,
    });
    expect(n).toBe(2);
  });

  it('ignores a malformed pr_number in the pending-approvals LISTING lane', () => {
    // The decisions lane's isPositiveIntPr guard is covered above; the listing
    // lane's was not, and deleting it left every awaitingCount test green.
    // PendingApproval.pr_number is TYPED as a plain number, so only a
    // malformed backend row reaches this — but that row would inflate the
    // band's "awaiting your approval" figure, which is a number an operator is
    // asked to trust, and one the ledger strip directly beneath it would then
    // contradict. Each bad value is paired with one good row so the assertion
    // distinguishes "guard dropped the bad one" (1) from "guard dropped
    // everything" (0).
    for (const bad of [0, -1, 1.5, NaN, Infinity, '7', null, undefined]) {
      const n = awaitingCount({
        decisions: [],
        pendingApprovals: [
          pendingIac({ pr_number: bad as unknown as number }),
          pendingIac({ pr_number: 9 }),
        ],
        now: NOW,
        origin: ORIGIN,
      });
      expect(n, `pr_number=${String(bad)} must not be counted`).toBe(1);
    }
  });

  it('tolerates a null/undefined decisions and pendingApprovals list entirely (0, not a crash)', () => {
    const n = awaitingCount({
      decisions: null as unknown as Decision[],
      pendingApprovals: undefined as unknown as PendingApproval[],
      now: NOW,
      origin: ORIGIN,
    });
    expect(n).toBe(0);
  });
});
