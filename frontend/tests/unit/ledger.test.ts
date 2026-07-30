import { describe, it, expect } from 'vitest';
import { ledgerRows } from '../../src/lib/ledger';
import type { Decision } from '../../src/lib/types';

// ledgerRows() reduces the decisions list to the desk's "Recent record" strip
// rows: classified (applied/open/noted), newest-first, capped. See
// lib/ledger.ts's header comment for the precedence rules this mirrors from
// desk.ts / approval.ts.

const NOW = Date.parse('2026-07-28T12:00:00Z');
const ORIGIN = 'https://coordinator.example';

function decision(overrides: Partial<Decision> & { decision_id: string }): Decision {
  return {
    action: 'no_op',
    ...overrides,
  } as Decision;
}

describe('ledgerRows', () => {
  describe('three-way classification', () => {
    it('classifies apply_status==="applied" as applied', () => {
      const d = decision({ decision_id: 'a1', action: 'iac_apply', apply_status: 'applied', created_at: '2026-07-28T09:00:00Z' });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('applied');
    });

    // This block previously contained a test literally named
    // 'classifies approval.status==="used" as applied (rollback lane)'. That
    // was the defect written down as a specification: the flip to `used` is the
    // anti-replay claim and runs BEFORE the Cloud Run traffic shift, so it
    // cannot testify that anything applied. The outcome lives in `phase`
    // (ds-2mc). Each phase now gets its own row state.

    it('classifies a CONFIRMED rollback (used + phase applied) as applied', () => {
      const d = decision({
        decision_id: 'a2',
        approval: { approval_url: '/approvals/a2', status: 'used', phase: 'applied' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('applied');
    });

    it('classifies a failed rollback (used + phase failed) as failed, not applied', () => {
      const d = decision({
        decision_id: 'a3',
        approval: { approval_url: '/approvals/a3', status: 'used', phase: 'failed' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('failed');
    });

    it('classifies an unconfirmed rollback as unconfirmed — NOT failed, NOT applied', () => {
      // The operation is uncancelled and may yet succeed. Claiming either
      // outcome would be a false statement; this is the third answer.
      const d = decision({
        decision_id: 'a4',
        approval: { approval_url: '/approvals/a4', status: 'used', phase: 'outcome_unknown' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('unconfirmed');
    });

    it.each(['claimed', 'applying'] as const)(
      'does not report an in-flight rollback (phase %s) as applied',
      (phase) => {
        const d = decision({
          decision_id: `a5-${phase}`,
          approval: { approval_url: '/approvals/a5', status: 'used', phase },
          created_at: '2026-07-28T09:00:00Z',
        });
        const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
        expect(rows[0].state).not.toBe('applied');
      },
    );

    it('does not report a pre-ds-2mc rollback (used, no phase) as applied', () => {
      // Absence of a phase means the outcome is unknown, which is exactly what
      // it was for every rollback resolved before this field existed. Unknown
      // must never inherit success from `status: used`.
      const d = decision({
        decision_id: 'a6',
        approval: { approval_url: '/approvals/a6', status: 'used' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).not.toBe('applied');
    });

    it('classifies a live pending rollback approval as open', () => {
      const d = decision({
        decision_id: 'o1',
        approval: { approval_url: '/approvals/o1', status: 'pending', expires_at: '2026-07-29T00:00:00Z' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('open');
    });

    it('treats an absent approval.status as pending (pre-enrichment rows) → open', () => {
      const d = decision({
        decision_id: 'o2',
        approval: { approval_url: '/approvals/o2', expires_at: '2026-07-29T00:00:00Z' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('open');
    });

    it('classifies a non-superseded waiting_for_rebake iac row as open', () => {
      const d = decision({
        decision_id: 'o3',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 300,
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('open');
    });

    it('falls back to noted for everything else (e.g. no_op)', () => {
      const d = decision({ decision_id: 'n1', action: 'no_op', created_at: '2026-07-28T09:00:00Z' });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('noted');
    });
  });

  it('an expired rollback approval → noted, not open', () => {
    const d = decision({
      decision_id: 'e1',
      approval: { approval_url: '/approvals/e1', status: 'pending', expires_at: '2026-07-28T00:00:00Z' },
      created_at: '2026-07-28T09:00:00Z',
    });
    const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
    expect(rows[0].state).toBe('noted');
  });

  it('a denied rollback approval → noted, not open', () => {
    const d = decision({
      decision_id: 'e2',
      approval: { approval_url: '/approvals/e2', status: 'denied' },
      created_at: '2026-07-28T09:00:00Z',
    });
    const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
    expect(rows[0].state).toBe('noted');
  });

  describe('actionability gate (safeApprovalHref) on the rollback lane', () => {
    // The rollback lane's `open` classification must agree with desk.ts's
    // selectPendingRollback — both are answering the SAME question ("is a
    // human really being asked to act here") and must never disagree, or the
    // ledger would advertise an approval the desk can't surface (or the
    // reverse). safeApprovalHref is the single source of truth for that.
    it('a `<redacted>` scrub-placeholder token → noted, not open (dead button — no working Approve/Reject path)', () => {
      const d = decision({
        decision_id: 'r1',
        approval: { approval_url: '/approvals/r1?t=<redacted>', status: 'pending', expires_at: '2026-07-29T00:00:00Z' },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('noted');
    });

    it('an off-origin approval_url → noted, not open', () => {
      const d = decision({
        decision_id: 'r2',
        approval: {
          approval_url: 'https://attacker.example/approvals/r2',
          status: 'pending',
          expires_at: '2026-07-29T00:00:00Z',
        },
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('noted');
    });
  });

  describe('superseded waiting_for_rebake', () => {
    it('explicit superseded_by_pr → noted, not open', () => {
      const d = decision({
        decision_id: 's1',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 301,
        superseded_by_pr: 302,
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('noted');
    });

    it('this generation’s own applied row (same event_key) → noted, not open', () => {
      const waiting = decision({
        decision_id: 's2',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 400,
        event_key: 'iac-apply-400-samegeneration',
        created_at: '2026-07-28T08:00:00Z',
      });
      const applied = decision({
        decision_id: 's3',
        action: 'iac_apply',
        apply_status: 'applied',
        pr_number: 400,
        event_key: 'iac-apply-400-samegeneration',
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([waiting, applied], 4, { now: NOW, origin: ORIGIN });
      const waitingRow = rows.find((r) => r.decision.decision_id === 's2');
      expect(waitingRow?.state).toBe('noted');
    });

    // ds-dzd: a terminal row from a DIFFERENT generation is not this row's
    // outcome, so the ledger must keep showing it as open work.
    it('a different generation’s applied row leaves the waiting row OPEN', () => {
      const waiting = decision({
        decision_id: 's4',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 400,
        event_key: 'iac-apply-400-generationB',
        created_at: '2026-07-28T09:30:00Z',
      });
      const applied = decision({
        decision_id: 's5',
        action: 'iac_apply',
        apply_status: 'applied',
        pr_number: 400,
        event_key: 'iac-apply-400-generationA',
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([waiting, applied], 4, { now: NOW, origin: ORIGIN });
      expect(rows.find((r) => r.decision.decision_id === 's4')?.state).toBe('open');
    });
  });

  describe('ordering', () => {
    it('sorts newest-first by created_at', () => {
      const older = decision({ decision_id: 'x1', created_at: '2026-07-28T08:00:00Z' });
      const newer = decision({ decision_id: 'x2', created_at: '2026-07-28T10:00:00Z' });
      const middle = decision({ decision_id: 'x3', created_at: '2026-07-28T09:00:00Z' });
      const rows = ledgerRows([older, newer, middle], 4, { now: NOW, origin: ORIGIN });
      expect(rows.map((r) => r.decision.decision_id)).toEqual(['x2', 'x3', 'x1']);
    });

    it('a null/unparseable created_at sorts last but still eligible for a slot', () => {
      const dated = decision({ decision_id: 'y1', created_at: '2026-07-28T08:00:00Z' });
      const missing = decision({ decision_id: 'y2', created_at: undefined });
      const garbled = decision({ decision_id: 'y3', created_at: 'not-a-date' });
      const rows = ledgerRows([missing, dated, garbled], 4, { now: NOW, origin: ORIGIN });
      expect(rows.map((r) => r.decision.decision_id)).toEqual(['y1', 'y2', 'y3']);
      expect(rows[1].ts).toBeNull();
      expect(rows[2].ts).toBeNull();
    });
  });

  it('caps to max (default 4)', () => {
    const decisions = Array.from({ length: 10 }, (_, i) =>
      decision({ decision_id: `c${i}`, created_at: `2026-07-28T${String(i).padStart(2, '0')}:00:00Z` }),
    );
    const rows = ledgerRows(decisions, undefined, { now: NOW, origin: ORIGIN });
    expect(rows).toHaveLength(4);
    // newest-first: the 4 highest hour values
    expect(rows.map((r) => r.decision.decision_id)).toEqual(['c9', 'c8', 'c7', 'c6']);
  });

  it('caps to an explicit max', () => {
    const decisions = Array.from({ length: 6 }, (_, i) => decision({ decision_id: `c${i}` }));
    const rows = ledgerRows(decisions, 2, { now: NOW, origin: ORIGIN });
    expect(rows).toHaveLength(2);
  });

  describe('defensive input handling', () => {
    it('a null list returns []', () => {
      expect(ledgerRows(null, 4, { now: NOW, origin: ORIGIN })).toEqual([]);
    });

    it('an undefined list returns []', () => {
      expect(ledgerRows(undefined, 4, { now: NOW, origin: ORIGIN })).toEqual([]);
    });

    it('null/undefined elements in the list are skipped, not thrown on', () => {
      const d = decision({ decision_id: 'z1', created_at: '2026-07-28T09:00:00Z' });
      const rows = ledgerRows([null, d, undefined], 4, { now: NOW, origin: ORIGIN });
      expect(rows.map((r) => r.decision.decision_id)).toEqual(['z1']);
    });

    it('max <= 0 returns [] (multi-item list — guards against slice(0,-1) coincidentally emptying a 1-item list)', () => {
      const decisions = Array.from({ length: 3 }, (_, i) => decision({ decision_id: `z${i}` }));
      expect(ledgerRows(decisions, 0, { now: NOW, origin: ORIGIN })).toEqual([]);
      expect(ledgerRows(decisions, -1, { now: NOW, origin: ORIGIN })).toEqual([]);
    });

    it('a non-finite max falls back to the default of 4', () => {
      const decisions = Array.from({ length: 6 }, (_, i) => decision({ decision_id: `w${i}` }));
      expect(ledgerRows(decisions, NaN, { now: NOW, origin: ORIGIN })).toHaveLength(4);
      expect(ledgerRows(decisions, Infinity, { now: NOW, origin: ORIGIN })).toHaveLength(4);
    });
  });
});
