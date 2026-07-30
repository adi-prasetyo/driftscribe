import { describe, it, expect } from 'vitest';
import { deskModel, awaitingCount, STAMP_WINDOW_MS, STUCK_APPLYING_MS } from '../../src/lib/desk';
import type { Decision } from '../../src/lib/types';
import type { PendingApproval } from '../../src/lib/infra_graph';
import { ledgerRows } from '../../src/lib/ledger';

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

  // ds-dzd: matching event_keys, i.e. the applied row IS this waiting row's own
  // generation finishing. That is the real prod shape — PRs 221/102/96/68/66/47
  // all carry an applied row and older waiting rows under ONE event_key.
  it('this generation’s own applied row → NOT pending', () => {
    const EK = 'iac-apply-44-samegeneration';
    const waiting = iacDecision({
      decision_id: 'iac-waiting',
      pr_number: 44,
      event_key: EK,
      created_at: '2026-07-28T10:00:00Z',
    });
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 44,
      event_key: EK,
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

  // The negative twin, and the reason event_key scoping exists. Generation A
  // applied but its merge failed, the PR stayed open, the head advanced, and
  // generation B recorded its own waiting row. B is real work the operator has to
  // approve; a PR-wide rule deleted it from the desk entirely.
  it('a DIFFERENT generation’s applied row leaves this waiting row PENDING', () => {
    const appliedA = iacDecision({
      decision_id: 'iac-applied-A',
      pr_number: 44,
      event_key: 'iac-apply-44-generationA',
      apply_status: 'applied',
      merge_state: 'failed',
      applied_at: '2026-07-28T10:30:00Z',
      created_at: '2026-07-28T10:30:00Z',
    });
    const waitingB = iacDecision({
      decision_id: 'iac-waiting-B',
      pr_number: 44,
      event_key: 'iac-apply-44-generationB',
      created_at: '2026-07-28T11:00:00Z',
    });
    const model = deskModel({
      decisions: [appliedA, waitingB],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
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
        phase: 'applied',
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

  it.each(['failed', 'outcome_unknown', 'claimed', 'applying'] as const)(
    'a used approval with phase %s never seals the desk',
    (phase) => {
      const d = rollbackDecision({
        approval: {
          approval_url: '/approvals/rb-1?t=x',
          status: 'used',
          phase,
          // Even WITH a fresh resolved_at, only `applied` may seal. Pinned
          // deliberately: the backend refuses to write resolved_at alongside a
          // non-applied phase, and this asserts the frontend would not seal on
          // one anyway. Two independent guards, because the seal is the single
          // most damaging thing on this screen to get wrong (ds-2mc).
          resolved_at: '2026-07-28T11:58:00Z',
        },
      });
      const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
      expect(model.kind).not.toBe('stamped');
    },
  );

  it('a used approval with NO phase (pre-ds-2mc doc) never seals the desk', () => {
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        resolved_at: '2026-07-28T11:58:00Z',
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).not.toBe('stamped');
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
        phase: 'applied',
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
        phase: 'applied',
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

describe('deskModel — rule 2.5: unresolved rollback outcome', () => {
  // A rollback whose credential was spent but which did not demonstrably
  // apply. The seal ceasing to lie (rule 3's phase gate) is only half the fix:
  // without this rule a failed rollback produces NO desk state at all, and the
  // hero falls through to "Nothing needs your decision right now."

  it.each(['failed', 'outcome_unknown'] as const)(
    'surfaces a %s rollback instead of resting',
    (phase) => {
      const d = rollbackDecision({
        approval: { approval_url: '/approvals/rb-1?t=x', status: 'used', phase },
      });
      const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
      expect(model.kind).toBe('unresolved');
      if (model.kind === 'unresolved') expect(model.phase).toBe(phase);
    },
  );

  it('keeps failed and outcome_unknown distinct all the way through the model', () => {
    // Collapsing them would re-introduce the original defect inverted: an
    // uncancelled operation that may still succeed reported as a failure.
    const unknown = rollbackDecision({
      approval: { approval_url: '/approvals/rb-1?t=x', status: 'used', phase: 'outcome_unknown' },
    });
    const model = deskModel({
      decisions: [unknown], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind === 'unresolved' && model.phase).toBe('outcome_unknown');
    expect(model.kind === 'unresolved' && model.phase === 'failed').toBe(false);
  });

  it('outranks a stamped success seal', () => {
    // The seal is exactly what an operator reads as "all good". A bad outcome
    // must not sit silently underneath one.
    const sealed = iacDecision({
      decision_id: 'iac-done',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:58:00Z',
    });
    const bad = rollbackDecision({
      decision_id: 'rb-bad',
      approval: { approval_url: '/approvals/rb-bad?t=x', status: 'used', phase: 'failed' },
    });
    const model = deskModel({
      decisions: [sealed, bad], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('unresolved');
  });

  it('is outranked by anything actually awaiting a decision', () => {
    const bad = rollbackDecision({
      decision_id: 'rb-bad',
      approval: { approval_url: '/approvals/rb-bad?t=x', status: 'used', phase: 'failed' },
    });
    const model = deskModel({
      decisions: [bad], pendingApprovals: [pendingIac()], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
  });

  it('reaches the operator even when the ledger strip has overflowed past it', () => {
    // The precise scenario that made "it shows up in the ledger" untrue: the
    // strip orders by the PROPOSAL's created_at and caps at four, so a rollback
    // proposed long ago that fails NOW is pushed out entirely by four newer
    // decisions — no seal, no row, hero resting. This is the guaranteed surface.
    const oldFailedRollback = rollbackDecision({
      decision_id: 'rb-old-failed',
      created_at: '2026-07-28T09:00:00Z',
      approval: { approval_url: '/approvals/rb-old?t=x', status: 'used', phase: 'failed' },
    });
    const newer = [1, 2, 3, 4].map((i) =>
      iacDecision({
        decision_id: `iac-newer-${i}`,
        pr_number: 100 + i,
        apply_status: 'applied',
        applied_at: `2026-07-28T1${i}:00:00Z`,
        created_at: `2026-07-28T1${i}:00:00Z`,
      }),
    );

    // Precondition: the failed rollback really is off the end of the strip.
    const rows = ledgerRows([...newer, oldFailedRollback], 4, { now: NOW, origin: ORIGIN });
    expect(rows).toHaveLength(4);
    expect(rows.map((r) => r.decision.decision_id)).not.toContain('rb-old-failed');

    const model = deskModel({
      decisions: [...newer, oldFailedRollback], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('unresolved');
    if (model.kind === 'unresolved') expect(model.decision.decision_id).toBe('rb-old-failed');
  });

  it('does not fire for a FRESH in-flight rollback or a pre-ds-2mc doc', () => {
    // Inside the worker's poll budget /execute is still expected to settle the
    // doc itself; a scary card during every routine rollback would be its own
    // dishonesty. A missing phase is an old doc, not an outcome to escalate.
    for (const approval of [
      {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used' as const,
        phase: 'applying' as const,
        phase_at: new Date(NOW - 30_000).toISOString(), // 30s ago — still working
      },
      { approval_url: '/approvals/rb-1?t=x', status: 'used' as const },
    ]) {
      const model = deskModel({
        decisions: [rollbackDecision({ approval })],
        pendingApprovals: [], now: NOW, origin: ORIGIN,
      });
      expect(model.kind).toBe('resting');
    }
  });

  it('surfaces a STUCK applying rollback as unknown — never as failed', () => {
    // The worker died mid-wait. Nobody is coming back for it, so silence would
    // be the same disappearance this rule exists to prevent. But nothing has
    // been established either, so it reports unknown, not failure.
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase: 'applying',
        phase_at: new Date(NOW - STUCK_APPLYING_MS - 60_000).toISOString(),
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('unresolved');
    if (model.kind === 'unresolved') expect(model.phase).toBe('outcome_unknown');
  });

  it('a later CONFIRMED rollback supersedes an earlier failure', () => {
    // Otherwise the desk pins itself to an ancient failure forever: a new
    // proposal only hides it while pending, and rule 2.5 outranks stamped, so
    // the old failure would re-win over the new success's own seal.
    const oldFailure = rollbackDecision({
      decision_id: 'rb-failed',
      created_at: '2026-07-28T10:00:00Z',
      approval: {
        approval_url: '/approvals/rb-failed?t=x',
        status: 'used',
        phase: 'failed',
        phase_at: '2026-07-28T10:05:00Z',
      },
    });
    const laterSuccess = rollbackDecision({
      decision_id: 'rb-ok',
      created_at: '2026-07-28T11:30:00Z',
      approval: {
        approval_url: '/approvals/rb-ok?t=x',
        status: 'used',
        phase: 'applied',
        phase_at: '2026-07-28T11:55:00Z',
        resolved_at: '2026-07-28T11:55:00Z',
      },
    });
    const model = deskModel({
      decisions: [oldFailure, laterSuccess], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
  });

  it('an EARLIER success does not supersede a later failure', () => {
    const earlierSuccess = rollbackDecision({
      decision_id: 'rb-ok',
      created_at: '2026-07-28T10:00:00Z',
      approval: {
        approval_url: '/approvals/rb-ok?t=x',
        status: 'used',
        phase: 'applied',
        phase_at: '2026-07-28T10:05:00Z',
        resolved_at: '2026-07-28T10:05:00Z',
      },
    });
    const laterFailure = rollbackDecision({
      decision_id: 'rb-failed',
      created_at: '2026-07-28T11:30:00Z',
      approval: {
        approval_url: '/approvals/rb-failed?t=x',
        status: 'used',
        phase: 'failed',
        phase_at: '2026-07-28T11:35:00Z',
      },
    });
    const model = deskModel({
      decisions: [earlierSuccess, laterFailure], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('unresolved');
  });

  it('a late-reconciled success does not post-date and bury a newer real failure', () => {
    // The scenario delayed reconciliation creates:
    //   10:00  attempt A starts, succeeds, but stays unconfirmed
    //   10:05  attempt B starts
    //   10:06  attempt B definitely FAILS
    //   10:07  attempt A is reconciled -> applied, phase_at = 10:07
    // Superseding by phase_at would read A as "the later rollback" and hide B's
    // real failure. Supersession is a question about attempt chronology.
    const attemptA = rollbackDecision({
      decision_id: 'rb-a-succeeded',
      created_at: '2026-07-28T10:00:00Z',
      approval: {
        approval_url: '/approvals/rb-a?t=x',
        status: 'used',
        phase: 'applied',
        phase_at: '2026-07-28T10:07:00Z', // OBSERVED late by /reconcile
        // No resolved_at: reconcile never fabricates one.
      },
    });
    const attemptB = rollbackDecision({
      decision_id: 'rb-b-failed',
      created_at: '2026-07-28T10:05:00Z',
      approval: {
        approval_url: '/approvals/rb-b?t=x',
        status: 'used',
        phase: 'failed',
        phase_at: '2026-07-28T10:06:00Z',
      },
    });
    const model = deskModel({
      decisions: [attemptA, attemptB], pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind).toBe('unresolved');
    if (model.kind === 'unresolved') {
      expect(model.decision.decision_id).toBe('rb-b-failed');
      expect(model.phase).toBe('failed');
    }
  });

  it('a STALE claimed rollback surfaces as unknown rather than vanishing', () => {
    // The worker died between burning the credential and writing the operation
    // handle — possibly AFTER update_service accepted the traffic change.
    // Nothing can reconcile it (no handle to look up), so if the desk stayed
    // silent this would be a burned approval with an unknown outcome that the
    // operator is never told about: the silent `used` in its purest form.
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase: 'claimed',
        phase_at: new Date(NOW - STUCK_APPLYING_MS - 60_000).toISOString(),
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('unresolved');
    if (model.kind === 'unresolved') expect(model.phase).toBe('outcome_unknown');
  });

  it('a FRESH claimed rollback does not surface (execute still owns it)', () => {
    const d = rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase: 'claimed',
        phase_at: new Date(NOW - 5_000).toISOString(),
      },
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('resting');
  });

  it('orders by phase_at (observation time), not the proposal time', () => {
    // resolved_at cannot order these — it exists only on a confirmed success,
    // so every failed/unconfirmed row would tie at null.
    const proposedFirstFailedLast = rollbackDecision({
      decision_id: 'rb-a',
      created_at: '2026-07-28T09:00:00Z',
      approval: {
        approval_url: '/approvals/rb-a?t=x', status: 'used', phase: 'failed',
        phase_at: '2026-07-28T11:50:00Z',
      },
    });
    const proposedLastFailedFirst = rollbackDecision({
      decision_id: 'rb-b',
      created_at: '2026-07-28T11:00:00Z',
      approval: {
        approval_url: '/approvals/rb-b?t=x', status: 'used', phase: 'failed',
        phase_at: '2026-07-28T11:10:00Z',
      },
    });
    const model = deskModel({
      decisions: [proposedFirstFailedLast, proposedLastFailedFirst],
      pendingApprovals: [], now: NOW, origin: ORIGIN,
    });
    expect(model.kind === 'unresolved' && model.decision.decision_id).toBe('rb-a');
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

  // ds-dzd — the live shape that made the band lie on prod. PR #95's
  // `failed_state_suspect` row and BOTH of its `waiting_for_rebake` rows share
  // ONE event_key, so the failure genuinely is those rows' own outcome. The band
  // read "2 awaiting your approval" over a desk showing ONE item, and the phantom
  // was two months old and reachable from no surface at all: the single CTA slot
  // went to the listing lane, and the ledger strip only shows recent rows. A count
  // nobody can act on is worse than a wrong count — it sends the operator hunting
  // for work that does not exist.
  const EK_A = 'iac-apply-95-312b3cac8ba677d62a138f8050de2a34';
  const EK_B = 'iac-apply-95-ffffffffffffffffffffffffffffffff';

  it('a waiting row whose OWN generation reached a terminal state never inflates the count (PR #95)', () => {
    const terminal = iacDecision({
      pr_number: 95,
      apply_status: 'failed_state_suspect',
      event_key: EK_A,
      decision_id: 'term-95',
      created_at: '2026-06-11T05:50:36Z',
    });
    const stale = iacDecision({
      pr_number: 95,
      apply_status: 'waiting_for_rebake',
      event_key: EK_A,
      decision_id: 'stale-95',
      created_at: '2026-06-11T05:39:09Z',
    });
    expect(
      awaitingCount({ decisions: [terminal, stale], pendingApprovals: [], now: NOW, origin: ORIGIN }),
    ).toBe(0);
  });

  // THE REGRESSION GUARD. Generations are not serialized against each other, so
  // two can be in flight and finish out of order: terminal(A) can be NEWER than
  // waiting(B) while saying nothing about generation B. A per-PR "newest terminal"
  // rule retires the live B row — hiding the exact work the recovery runbook
  // tells the operator to approve. Matching on event_key cannot.
  it('a terminal in one generation never retires a live waiting row in another', () => {
    const waitingA = iacDecision({
      pr_number: 95, apply_status: 'waiting_for_rebake', event_key: EK_A,
      decision_id: 'wA', created_at: '2026-07-30T10:00:00Z',
    });
    const waitingB = iacDecision({
      pr_number: 95, apply_status: 'waiting_for_rebake', event_key: EK_B,
      decision_id: 'wB', created_at: '2026-07-30T11:00:00Z',
    });
    const terminalA = iacDecision({
      pr_number: 95, apply_status: 'failed_state_suspect', event_key: EK_A,
      decision_id: 'tA', created_at: '2026-07-30T12:00:00Z',
    });
    // Generation B is still awaiting the operator, so the PR still counts once.
    expect(
      awaitingCount({
        decisions: [waitingA, waitingB, terminalA],
        pendingApprovals: [],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });

  // The whole prod snapshot, reduced: one genuinely-actionable listing item plus
  // the #95 ghost. The band must read 1 — what the page can actually show.
  it('reports only the reachable item when a superseded ghost sits alongside it', () => {
    const ghostTerminal = iacDecision({
      pr_number: 95, apply_status: 'failed_state_suspect', event_key: EK_A,
      decision_id: 'term-95', created_at: '2026-06-11T05:50:36Z',
    });
    const ghostStale = iacDecision({
      pr_number: 95, apply_status: 'waiting_for_rebake', event_key: EK_A,
      decision_id: 'stale-95', created_at: '2026-06-11T05:39:09Z',
    });
    const real = pendingIac({
      pr_number: 168,
      title: 'Adopt Pub/Sub topic adopt-probe-topic into IaC management',
      asset_type: 'pubsub.googleapis.com/Topic',
      resource_name: 'adopt-probe-topic',
    });
    expect(
      awaitingCount({
        decisions: [ghostTerminal, ghostStale],
        pendingApprovals: [real],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });

  // A failed apply must NOT retire the PR PR-wide, or estate.ts's
  // reconcileApprovals would drop a listing entry it holds no counter-evidence
  // for ("an in-progress or failed apply leaves the entry standing").
  it('a terminal failure does not suppress the same PR in the LISTING lane', () => {
    const failed = iacDecision({
      pr_number: 168, apply_status: 'failed', event_key: 'iac-apply-168-deadbeef',
      decision_id: 'f-168', created_at: '2026-07-30T10:00:00Z',
    });
    expect(
      awaitingCount({
        decisions: [failed],
        pendingApprovals: [pendingIac({ pr_number: 168 })],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });

  // Fails safe toward showing work in the degenerate cases.
  it('a waiting row with no event_key or no created_at keeps counting', () => {
    const terminal = iacDecision({
      pr_number: 95, apply_status: 'failed', event_key: EK_A,
      decision_id: 'term-95', created_at: '2026-07-30T12:00:00Z',
    });
    const noKey = iacDecision({
      pr_number: 95, apply_status: 'waiting_for_rebake', event_key: undefined,
      decision_id: 'no-key', created_at: '2026-07-30T10:00:00Z',
    });
    expect(
      awaitingCount({ decisions: [terminal, noKey], pendingApprovals: [], now: NOW, origin: ORIGIN }),
    ).toBe(1);
    const noTs = iacDecision({
      pr_number: 95, apply_status: 'waiting_for_rebake', event_key: EK_A,
      decision_id: 'no-ts', created_at: undefined,
    });
    expect(
      awaitingCount({ decisions: [terminal, noTs], pendingApprovals: [], now: NOW, origin: ORIGIN }),
    ).toBe(1);
  });

  it('a waiting_for_rebake row with no terminal sibling still counts', () => {
    const waiting = iacDecision({
      pr_number: 301, apply_status: 'waiting_for_rebake', event_key: 'iac-apply-301-abc',
      decision_id: 'w-301', created_at: '2026-07-30T10:00:00Z',
    });
    expect(
      awaitingCount({ decisions: [waiting], pendingApprovals: [], now: NOW, origin: ORIGIN }),
    ).toBe(1);
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

// ---------------------------------------------------------------------------
// ds-mml — the boundaries desk.ts pins religiously everywhere else.
//
// Each of these is currently correct; none had a test. They are the kind of
// thing a well-meaning refactor flips without noticing, and every one of them
// decides what the product's front door claims about a rollback.
// ---------------------------------------------------------------------------
describe('desk — selection boundaries', () => {
  function inFlight(phase: 'applying' | 'claimed', ageMs: number): Decision {
    return rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase,
        phase_at: new Date(NOW - ageMs).toISOString(),
        resolved_at: null,
      },
    });
  }

  it.each(['applying', 'claimed'] as const)(
    'a %s rollback exactly AT the stuck threshold is not yet unresolved',
    (phase) => {
      const model = deskModel({
        decisions: [inFlight(phase, STUCK_APPLYING_MS)],
        pendingApprovals: [],
        now: NOW,
        origin: ORIGIN,
      });
      // Strict `>`: at the boundary the rollback is still within its budget, and
      // calling it unconfirmed one millisecond early is its own dishonesty.
      expect(model.kind).not.toBe('unresolved');
    },
  );

  it.each(['applying', 'claimed'] as const)(
    'a %s rollback one millisecond PAST the threshold surfaces as unconfirmed',
    (phase) => {
      const model = deskModel({
        decisions: [inFlight(phase, STUCK_APPLYING_MS + 1)],
        pendingApprovals: [],
        now: NOW,
        origin: ORIGIN,
      });
      expect(model.kind).toBe('unresolved');
      // Never "failed" — in both phases the rollback may well have applied.
      if (model.kind === 'unresolved') expect(model.phase).toBe('outcome_unknown');
    },
  );

  it('an applied rollback with NO created_at cannot supersede an unresolved one', () => {
    // parseForOrdering returns -Infinity for a missing created_at, so such a
    // decision loses every supersession comparison. That is the safe direction
    // and it is deliberate: superseding means HIDING an unresolved outcome from
    // the front door, so a decision we cannot place in time must not win it.
    const unresolved = rollbackDecision({
      decision_id: 'rb-unresolved',
      created_at: '2026-07-28T11:00:00Z',
      approval: {
        approval_url: '/approvals/rb-unresolved?t=x',
        status: 'used',
        phase: 'outcome_unknown',
        phase_at: '2026-07-28T11:01:00Z',
        resolved_at: null,
      },
    });
    const appliedButUndated = rollbackDecision({
      decision_id: 'rb-applied',
      created_at: undefined,
      approval: {
        approval_url: '/approvals/rb-applied?t=x',
        status: 'used',
        phase: 'applied',
        resolved_at: '2026-07-28T11:59:00Z',
      },
    });

    const model = deskModel({
      decisions: [unresolved, appliedButUndated],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('unresolved');
  });

  it('an exact cross-kind tie between an iac stamp and a rollback stamp goes to iac', () => {
    // `bestIac.ts >= bestRollback.ts` — the tie-break is a real decision, not an
    // accident of comparison order, and swapping it silently changes which
    // artifact the hero shows on a dead heat.
    const tie = '2026-07-28T11:58:00Z';
    const model = deskModel({
      decisions: [
        iacDecision({ decision_id: 'iac-tie', apply_status: 'applied', applied_at: tie }),
        rollbackDecision({
          decision_id: 'rb-tie',
          approval: {
            approval_url: '/approvals/rb-tie?t=x',
            status: 'used',
            phase: 'applied',
            resolved_at: tie,
          },
        }),
      ],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('stamped');
    if (model.kind === 'stamped') expect(model.source).toBe('iac');
  });

  it('a stamp exactly AT the end of its window is still valid', () => {
    const appliedAt = new Date(NOW - STAMP_WINDOW_MS).toISOString();
    const model = deskModel({
      decisions: [iacDecision({ apply_status: 'applied', applied_at: appliedAt })],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    // `now <= until` — inclusive, so the receipt does not vanish a tick early.
    expect(model.kind).toBe('stamped');
  });
});

// ---------------------------------------------------------------------------
// ds-0rm — the 60s pending-approvals cache must not outrank a terminal applied
// state. Rule 2a is tried BEFORE rule 2b and used to skip the resolved-PR
// filter that 2b applies, so a merged+applied PR still sitting in the backend
// cache re-presented itself as "needs your approval" and inflated the band.
// ---------------------------------------------------------------------------

describe('deskModel — rule 2a honors resolvedIacPrNumbers (ds-0rm)', () => {
  it('does not present a listing PR that a decision row shows as already applied', () => {
    // The exact skew: GitHub's open-PR listing is stale (cached up to 60s), the
    // decisions log already carries the applied row for the same PR.
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: 'merged',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [applied],
      pendingApprovals: [pendingIac({ pr_number: 7 })],
      now: NOW,
      origin: ORIGIN,
    });
    // Must reach the seal, not the CTA — the operator already made this call.
    expect(model.kind).toBe('stamped');
  });

  // ds-dzd: `applied` alone is NOT proof. An apply that succeeded while its merge
  // FAILED leaves the PR open, and a later generation of it appears in the listing
  // BEFORE it has any decision row (one is written on the first approval click).
  // Suppressing here made that live work vanish with nothing for the decision-row
  // fallback to find. Only applied+merged is positive proof.
  it('does NOT suppress a listing PR whose apply succeeded but whose merge FAILED', () => {
    const appliedMergeFailed = iacDecision({
      decision_id: 'iac-applied-merge-failed',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: 'failed',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [appliedMergeFailed],
      pendingApprovals: [pendingIac({ pr_number: 7 })],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') expect(model.prNumber).toBe(7);
  });

  it('does NOT suppress a listing PR when the applied row has no merge_state', () => {
    const appliedUnknownMerge = iacDecision({
      decision_id: 'iac-applied-unknown-merge',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: undefined,
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [appliedUnknownMerge],
      pendingApprovals: [pendingIac({ pr_number: 7 })],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
  });

  it('still presents a listing PR that is genuinely open', () => {
    // Control for the test above: a DIFFERENT PR being applied must not
    // suppress an unrelated open one, or the filter would be over-broad.
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 99,
      apply_status: 'applied',
      merge_state: 'merged',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [applied],
      pendingApprovals: [pendingIac({ pr_number: 7 })],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.prNumber).toBe(7);
    } else {
      throw new Error('expected a pending iac from the listing');
    }
  });

  it('falls through to the next listing entry rather than dropping the lane', () => {
    // A resolved head-of-list must not hide a genuinely open row behind it.
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: 'merged',
      applied_at: '2026-07-28T11:59:00Z',
    });
    const model = deskModel({
      decisions: [applied],
      pendingApprovals: [pendingIac({ pr_number: 7 }), pendingIac({ pr_number: 8 })],
      now: NOW,
      origin: ORIGIN,
    });
    expect(model.kind).toBe('pending');
    if (model.kind === 'pending' && model.source === 'iac') {
      expect(model.prNumber).toBe(8);
    } else {
      throw new Error('expected the second listing entry');
    }
  });
});

describe('awaitingCount — listing lane honors resolvedIacPrNumbers (ds-0rm)', () => {
  it('does not count a listing PR that is already applied', () => {
    const applied = iacDecision({
      decision_id: 'iac-applied',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: 'merged',
      applied_at: '2026-07-28T11:59:00Z',
    });
    expect(
      awaitingCount({
        decisions: [applied],
        pendingApprovals: [pendingIac({ pr_number: 7 })],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(0);
  });

  // The count must not lose a listing-only PR whose earlier generation applied but
  // failed to merge — there is no decision row for it yet, so nothing else would
  // report it (ds-dzd).
  it('still counts a listing PR whose apply succeeded but whose merge FAILED', () => {
    const appliedMergeFailed = iacDecision({
      decision_id: 'iac-applied-merge-failed',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: 'failed',
      applied_at: '2026-07-28T11:59:00Z',
    });
    expect(
      awaitingCount({
        decisions: [appliedMergeFailed],
        pendingApprovals: [pendingIac({ pr_number: 7 })],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });

  it('still counts a listing PR when the applied row has NO merge_state', () => {
    // The unknown-merge twin of the test above: absent merge state is not proof
    // the PR closed, so the listing entry must survive here too.
    const appliedUnknownMerge = iacDecision({
      decision_id: 'iac-applied-unknown-merge',
      pr_number: 7,
      apply_status: 'applied',
      merge_state: undefined,
      applied_at: '2026-07-28T11:59:00Z',
    });
    expect(
      awaitingCount({
        decisions: [appliedUnknownMerge],
        pendingApprovals: [pendingIac({ pr_number: 7 })],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });

  it('counts an open listing PR exactly once even when both lanes see it', () => {
    // Dedup across lanes was already correct; this pins that the new filter
    // did not turn the union into a subtraction.
    const awaiting = iacDecision({ pr_number: 7, apply_status: 'waiting_for_rebake' });
    expect(
      awaitingCount({
        decisions: [awaiting],
        pendingApprovals: [pendingIac({ pr_number: 7 })],
        now: NOW,
        origin: ORIGIN,
      }),
    ).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// ds-eh6 — the desk must not assert an all-clear it has not established.
// ---------------------------------------------------------------------------

describe('deskModel — unknown vs resting (ds-eh6)', () => {
  it('an empty snapshot that has SETTLED is resting', () => {
    // The all-clear remains available; it just has to be earned.
    expect(deskModel({ decisions: [], pendingApprovals: [], now: NOW, settled: true }).kind).toBe(
      'resting',
    );
  });

  it('defaults to settled, so existing callers keep the resting behavior', () => {
    expect(deskModel({ decisions: [], pendingApprovals: [], now: NOW }).kind).toBe('resting');
  });

  it('an unsettled snapshot is unknown/loading, NOT resting', () => {
    const model = deskModel({
      decisions: [],
      pendingApprovals: [],
      now: NOW,
      settled: false,
    });
    expect(model.kind).toBe('unknown');
    if (model.kind === 'unknown') expect(model.reason).toBe('loading');
  });

  it('a degraded snapshot is unknown/degraded, NOT resting', () => {
    const model = deskModel({
      decisions: [],
      pendingApprovals: [],
      now: NOW,
      settled: true,
      degraded: true,
    });
    expect(model.kind).toBe('unknown');
    if (model.kind === 'unknown') expect(model.reason).toBe('degraded');
  });

  it('loading outranks degraded — before the first cycle there is no cycle to describe', () => {
    const model = deskModel({
      decisions: [],
      pendingApprovals: [],
      now: NOW,
      settled: false,
      degraded: true,
    });
    expect(model.kind).toBe('unknown');
    if (model.kind === 'unknown') expect(model.reason).toBe('loading');
  });

  it('a pending item still wins over unknown — a provable card always shows', () => {
    // The load state suppresses only the claim about ABSENCE. Anything the
    // desk can actually prove from the snapshot it holds must still render,
    // or a slow graph fetch would hide a live approval.
    const d = rollbackDecision();
    const model = deskModel({
      decisions: [d],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
      settled: false,
      degraded: true,
    });
    expect(model.kind).toBe('pending');
  });

  it('a stamped item still wins over unknown', () => {
    const applied = iacDecision({
      apply_status: 'applied',
      applied_at: new Date(NOW - 60_000).toISOString(),
    });
    const model = deskModel({
      decisions: [applied],
      pendingApprovals: [],
      now: NOW,
      settled: false,
    });
    expect(model.kind).toBe('stamped');
  });
});

// ---------------------------------------------------------------------------
// ds-wd2.15 — the pending card's reasoning link.
// ---------------------------------------------------------------------------

const TRACE = 'a'.repeat(32);

describe('deskModel — pending traceId (ds-wd2.15)', () => {
  it('the rollback arm carries its decision trace', () => {
    // Sound ONLY on this arm: a rollback decision is written by the proposing
    // run itself, so its trace_id really is the reasoning behind the proposal.
    const model = deskModel({
      decisions: [rollbackDecision({ trace_id: TRACE })],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending') throw new Error('expected pending');
    expect(model.traceId).toBe(TRACE);
  });

  it('a trace id that cannot round-trip a ?reasoning= link is refused', () => {
    for (const bad of ['', 'not-hex', 'A'.repeat(32), 'a'.repeat(31), 'a'.repeat(33)]) {
      const model = deskModel({
        decisions: [rollbackDecision({ trace_id: bad })],
        pendingApprovals: [],
        now: NOW,
        origin: ORIGIN,
      });
      if (model.kind !== 'pending') throw new Error(`expected pending for ${JSON.stringify(bad)}`);
      expect(model.traceId).toBeNull();
    }
  });

  it('is null when the decision carries no trace at all', () => {
    const model = deskModel({
      decisions: [rollbackDecision()],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending') throw new Error('expected pending');
    expect(model.traceId).toBeNull();
  });

  // The iac arms deliberately carry NO trace. An iac_apply decision's trace_id
  // is stamped by _record_iac_decision inside the approve/apply POST
  // (agent/main.py ~5352) — a different request from the crew run that authored
  // the PR — so linking "view the reasoning behind this" to it would point the
  // operator at the trace of their own approval click. These two tests exist to
  // stop someone "fixing" the missing link by reaching for the nearest trace_id.
  // Sharper since ds-qua: the listing arm CAN now carry a trace, but only the one
  // the backend recorded on the approval itself. It must never fall back to a
  // decision's trace just because the PR numbers line up.
  it('the LISTING arm carries no trace, even when a decision shares its PR', () => {
    const model = deskModel({
      decisions: [iacDecision({ pr_number: 7, trace_id: TRACE })],
      pendingApprovals: [pendingIac({ pr_number: 7 })],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending' || model.source !== 'iac') throw new Error('expected pending iac');
    expect(model.provenance.kind).toBe('listing');
    expect(model.traceId).toBeNull();
  });

  it('the decisions-derived iac arm carries no trace either', () => {
    const model = deskModel({
      decisions: [iacDecision({ trace_id: TRACE })],
      pendingApprovals: [],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending' || model.source !== 'iac') throw new Error('expected pending iac');
    expect(model.provenance.kind).toBe('decision');
    expect(model.traceId).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ds-qua — the LISTING arm's authoring trace.
//
// Rule 2a builds its card from the open-PR listing, which carries no decision
// document: `activeDecision()` returns null, `DriftDiffCard` self-suppresses, and
// the card is a title, a PR number and two buttons. That made the front door's
// highest-stakes CTA the least-evidenced thing on the page.
//
// The backend now records, per (repo, PR), which reasoning run actually opened the
// PR — written at the editor worker's confirm gate from the worker's own result,
// never from model prose — and ships it as `authoring_trace_id`.
// ---------------------------------------------------------------------------

describe('deskModel — listing-arm authoring trace (ds-qua)', () => {
  it('surfaces the trace the backend recorded for this PR', () => {
    const model = deskModel({
      decisions: [],
      pendingApprovals: [pendingIac({ authoring_trace_id: TRACE })],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending' || model.source !== 'iac') throw new Error('expected pending iac');
    expect(model.provenance.kind).toBe('listing');
    expect(model.traceId).toBe(TRACE);
  });

  it('is null when the backend recorded nothing', () => {
    // A PR opened before the record existed. No link is the honest answer; the
    // desk must not reach for a nearby trace to fill the gap.
    for (const value of [undefined, '']) {
      const model = deskModel({
        decisions: [],
        pendingApprovals: [pendingIac({ authoring_trace_id: value })],
        now: NOW,
        origin: ORIGIN,
      });
      if (model.kind !== 'pending') throw new Error('expected pending');
      expect(model.traceId).toBeNull();
    }
  });

  it('refuses a trace that cannot round-trip a ?reasoning= link', () => {
    // Same gate as the rollback arm: never offer a link that opens once and then
    // fails to restore when the operator shares it.
    for (const bad of ['not-hex', 'A'.repeat(32), 'a'.repeat(31), 'a'.repeat(33), `${TRACE}\n`]) {
      const model = deskModel({
        decisions: [],
        pendingApprovals: [pendingIac({ authoring_trace_id: bad })],
        now: NOW,
        origin: ORIGIN,
      });
      if (model.kind !== 'pending') throw new Error(`expected pending for ${JSON.stringify(bad)}`);
      expect(model.traceId).toBeNull();
    }
  });

  it('a pending ROLLBACK still outranks it — rule 1 is unchanged', () => {
    const model = deskModel({
      decisions: [rollbackDecision({ trace_id: TRACE })],
      pendingApprovals: [pendingIac({ authoring_trace_id: 'b'.repeat(32) })],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending') throw new Error('expected pending');
    expect(model.source).toBe('rollback');
    expect(model.traceId).toBe(TRACE);
  });

  it('the trace travels with the PR the desk actually selected', () => {
    // Two open PRs, each with its own recorded trace. Whichever row rule 2a picks,
    // the trace on the model must be that row's — not the other one's.
    const first = pendingIac({ pr_number: 11, authoring_trace_id: 'c'.repeat(32) });
    const second = pendingIac({ pr_number: 12, authoring_trace_id: 'd'.repeat(32) });
    const model = deskModel({
      decisions: [],
      pendingApprovals: [first, second],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending' || model.source !== 'iac') throw new Error('expected pending iac');
    expect(model.prNumber).toBe(11);
    expect(model.traceId).toBe('c'.repeat(32));
  });

  it('a malformed pr_number is skipped, and takes its trace with it', () => {
    // The href guard skips the row; the next candidate must supply BOTH the PR
    // number and the trace, never a mix of the two rows.
    const model = deskModel({
      decisions: [],
      pendingApprovals: [
        pendingIac({ pr_number: 0, authoring_trace_id: 'c'.repeat(32) }),
        pendingIac({ pr_number: 12, authoring_trace_id: 'd'.repeat(32) }),
      ],
      now: NOW,
      origin: ORIGIN,
    });
    if (model.kind !== 'pending' || model.source !== 'iac') throw new Error('expected pending iac');
    expect(model.prNumber).toBe(12);
    expect(model.traceId).toBe('d'.repeat(32));
  });
});

// ds-y5i — the CHAT-initiated rollback lane.
//
// `propose_rollback_tool` (agent/adk_tools.py) now records a decision doc for a
// rollback proposed through chat; before that, only the autonomous path wrote
// one, so a chat rollback that really shifted Cloud Run traffic produced no
// pending hero, no seal, and no ledger row.
//
// These tests exist to pin the CONTRACT rather than to re-test desk.ts: the fix
// deliberately changed only the backend, on the claim that the chat row's shape
// is already one the desk handles. The row is thinner than the autonomous one —
// no `diffs`, no `rationale`, no `rendered_body` (the model's prose is dropped
// for the same reason it never reaches the approval page) — so if a future desk
// change starts depending on any of those, it must fail HERE and not on prod.
describe('deskModel — a chat-initiated rollback row (ds-y5i)', () => {
  /** The exact doc agent/adk_tools.py::_record_chat_rollback_decision writes. */
  function chatRollbackDecision(approval: Decision['approval']): Decision {
    return {
      decision_id: 'e0a4b1c2-3d4e-5f60-7a8b-9c0d1e2f3a4b',
      event_key: 'chat-rollback-9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15',
      trace_id: 'a'.repeat(32),
      action: 'rollback',
      decision_path: 'adk',
      target_revision: 'payment-demo-00010-abc',
      requires_human_review: true,
      dry_run: false,
      dry_run_effective: false,
      autonomy_mode: 'propose',
      trigger: 'chat',
      created_at: '2026-07-28T11:00:00Z',
      approval,
    } as Decision;
  }

  it('surfaces as the pending hero while its approval is live', () => {
    const d = chatRollbackDecision({
      approval_id: '9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15',
      approval_url: '/approvals/9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15?t=tok',
      expires_at: '2026-07-28T23:00:00Z',
      status: 'pending',
      resolved_at: null,
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('pending');
    if (model.kind !== 'pending' || model.source !== 'rollback') throw new Error('expected a pending rollback');
    expect(model.href).toBe('/approvals/9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15?t=tok');
    // ...and the ledger agrees it is the operator's to act on.
    expect(ledgerRows([d], 4, { now: NOW, origin: ORIGIN })[0].state).toBe('open');
  });

  it('stamps the seal once approved and applied, and the ledger reads applied', () => {
    const resolvedAt = new Date(NOW - 60_000).toISOString();
    const d = chatRollbackDecision({
      approval_id: '9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15',
      approval_url: '/approvals/9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15?t=tok',
      expires_at: '2026-07-28T23:00:00Z',
      status: 'used',
      phase: 'applied',
      resolved_at: resolvedAt,
    });
    const model = deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN });
    expect(model.kind).toBe('stamped');
    expect(ledgerRows([d], 4, { now: NOW, origin: ORIGIN })[0].state).toBe('applied');
  });

  it('does not depend on diffs / rationale / rendered_body being present', () => {
    const d = chatRollbackDecision({
      approval_id: '9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15',
      approval_url: '/approvals/9f2c1b40-6d3e-4a58-9c07-1b8e2f4a6d15?t=tok',
      expires_at: '2026-07-28T23:00:00Z',
      status: 'pending',
      resolved_at: null,
    });
    // The thin row is the one the backend actually writes: assert the absence
    // is real, so this test cannot silently start passing on a fatter fixture.
    expect(d.diffs).toBeUndefined();
    expect((d as Record<string, unknown>).rationale).toBeUndefined();
    expect((d as Record<string, unknown>).rendered_body).toBeUndefined();
    expect(deskModel({ decisions: [d], pendingApprovals: [], now: NOW, origin: ORIGIN }).kind).toBe('pending');
  });
});
