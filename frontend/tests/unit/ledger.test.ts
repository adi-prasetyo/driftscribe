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

    // ds-db0: waiting_for_rebake is written at MERGE, so the operator has
    // already approved. It still needs them (the re-bake), but it is NOT the
    // same state as an unspent rollback approval and must not borrow its copy.
    it('classifies a non-superseded waiting_for_rebake iac row as awaiting_rebake, not open', () => {
      const d = decision({
        decision_id: 'o3',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        merge_state: 'merged',
        pr_number: 300,
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([d], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('awaiting_rebake');
      expect(rows[0].state).not.toBe('open');
    });

    // waiting_for_rebake is written TWICE — before the merge (merge_state
    // 'pending', agent/main.py:7280) and after it (:7331). Only the second is
    // actually waiting on the re-bake; naming it while the merge is unfinished
    // would replace one false claim with another (Codex review of ds-db0).
    it('separates the pre-merge waiting_for_rebake record from the post-merge one', () => {
      const beforeMerge = decision({
        decision_id: 'm1',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        merge_state: 'pending',
        pr_number: 301,
        created_at: '2026-07-28T09:00:00Z',
      });
      const rows = ledgerRows([beforeMerge], 4, { now: NOW, origin: ORIGIN });
      expect(rows[0].state).toBe('awaiting_merge');
      expect(rows[0].state).not.toBe('awaiting_rebake');
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

    // ds-b0k CHANGED THIS: the earlier waiting doc used to survive as its own
    // 'noted' row, so one event drew two rows. It is now collapsed away by the
    // event_key dedup, which satisfies this test's original intent (the stale
    // waiting doc must never read as live work) more strongly than 'noted' did
    // — an absent row cannot mislead at all.
    it('this generation’s own applied row (same event_key) collapses the earlier waiting doc', () => {
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
      // One event → exactly one row, and it is the LATEST phase of that event.
      expect(rows).toHaveLength(1);
      expect(rows[0].decision.decision_id).toBe('s3');
      expect(rows[0].state).toBe('applied');
      // The superseded waiting doc is gone, not merely relabelled.
      expect(rows.find((r) => r.decision.decision_id === 's2')).toBeUndefined();
      expect(rows.some((r) => r.state === 'open' || r.state === 'awaiting_rebake')).toBe(false);
    });

    // ds-dzd: a terminal row from a DIFFERENT generation is not this row's
    // outcome, so the ledger must keep showing it as open work.
    // Also guards ds-b0k's dedup blast radius: the key is the GENERATION, not
    // the PR number, so two generations of the same PR must both survive.
    it('a different generation’s applied row leaves the waiting row awaiting_rebake', () => {
      const waiting = decision({
        decision_id: 's4',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        merge_state: 'merged',
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
      expect(rows.find((r) => r.decision.decision_id === 's4')?.state).toBe('awaiting_rebake');
      // Distinct event_keys → the dedup must NOT fold them together.
      expect(rows).toHaveLength(2);
    });

    // ds-b0k, drawn from the live PR #168 pair: the backend records a phase
    // sequence under ONE event_key (waiting_for_rebake+pending at 15:31:01,
    // then waiting_for_rebake+merged at 15:31:08). Both classify identically,
    // so neither supersedes the other and the desk drew the same event twice.
    it('collapses same-event_key phase-transition docs to the latest phase', () => {
      const pendingPhase = decision({
        decision_id: '26f475d9',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 168,
        event_key: 'iac-apply-168-d58dd9c035304939e834717bb17bcac1',
        created_at: '2026-07-31T15:31:01Z',
      });
      const mergedPhase = decision({
        decision_id: 'f66d3ce4',
        action: 'iac_apply',
        apply_status: 'waiting_for_rebake',
        pr_number: 168,
        event_key: 'iac-apply-168-d58dd9c035304939e834717bb17bcac1',
        created_at: '2026-07-31T15:31:08Z',
      });
      const rows = ledgerRows([pendingPhase, mergedPhase], 4, { now: NOW, origin: ORIGIN });
      expect(rows).toHaveLength(1);
      expect(rows[0].decision.decision_id).toBe('f66d3ce4');
    });

    // Unknown must fail toward RETENTION on an audit surface (Codex). The fold
    // is a positive test against waiting_for_rebake, so a legacy row with no
    // apply_status — or a status this build has never heard of — keeps its row
    // rather than being discarded sight unseen. A `!isTerminal` test would
    // have silently swallowed both.
    it.each([undefined, 'some_future_status'])(
      'never folds an iac row whose apply_status is unrecognised (%s)',
      (status) => {
        const older = decision({
          decision_id: 'u1',
          action: 'iac_apply',
          apply_status: status,
          pr_number: 700,
          event_key: 'iac-apply-700-onegeneration',
          created_at: '2026-07-28T08:00:00Z',
        });
        const newer = decision({
          decision_id: 'u2',
          action: 'iac_apply',
          apply_status: 'waiting_for_rebake',
          merge_state: 'merged',
          pr_number: 700,
          event_key: 'iac-apply-700-onegeneration',
          created_at: '2026-07-28T09:00:00Z',
        });
        const rows = ledgerRows([older, newer], 4, { now: NOW, origin: ORIGIN });
        expect(rows).toHaveLength(2);
      },
    );

    // A terminal record is a completed historical fact. Two applies 27 days
    // apart really do share an event_key in the live feed (iac-apply-32-...),
    // so collapsing them would delete a real record.
    it('never folds a terminal iac row into a newer one sharing its event_key', () => {
      const first = decision({
        decision_id: 't1',
        action: 'iac_apply',
        apply_status: 'applied',
        merge_state: 'merged',
        pr_number: 32,
        event_key: 'iac-apply-32-a1473c8a',
        created_at: '2026-05-30T11:16:12Z',
      });
      const second = decision({
        decision_id: 't2',
        action: 'iac_apply',
        apply_status: 'applied',
        merge_state: 'merged',
        pr_number: 32,
        event_key: 'iac-apply-32-a1473c8a',
        created_at: '2026-06-26T16:03:27Z',
      });
      const rows = ledgerRows([first, second], 4, { now: NOW, origin: ORIGIN });
      expect(rows).toHaveLength(2);
    });

    // The rollback/eventarc key namespace identifies {trigger, service,
    // contract, live_env} — NOT one approval attempt — and an expired approval
    // is deliberately replaced under the same key (agent/main.py:1155, :2486).
    // Folding there would hide a FAILED rollback behind a newer benign row.
    it('never folds non-iac lanes, so a failed rollback survives a newer retry', () => {
      const failed = decision({
        decision_id: 'rb1',
        action: 'rollback',
        approval: { approval_url: '/approvals/rb1', status: 'used', phase: 'failed' },
        event_key: 'eventarc-payment-demo-ae4170632c79c599',
        created_at: '2026-07-29T05:28:40Z',
      });
      const retry = decision({
        decision_id: 'rb2',
        action: 'rollback',
        approval: { approval_url: '/approvals/rb2', status: 'pending', expires_at: '2026-07-31T23:00:00Z' },
        event_key: 'eventarc-payment-demo-ae4170632c79c599',
        created_at: '2026-07-31T02:18:12Z',
      });
      const rows = ledgerRows([failed, retry], 4, { now: NOW, origin: ORIGIN });
      expect(rows).toHaveLength(2);
      expect(rows.find((r) => r.decision.decision_id === 'rb1')?.state).toBe('failed');
    });

    // An absent event_key is UNKNOWN identity, not SHARED identity — folding
    // those together would merge unrelated decisions into one row.
    it('never collapses rows that carry no event_key', () => {
      const a = decision({ decision_id: 'k1', created_at: '2026-07-28T09:00:00Z' });
      const b = decision({ decision_id: 'k2', created_at: '2026-07-28T08:00:00Z' });
      const rows = ledgerRows([a, b], 4, { now: NOW, origin: ORIGIN });
      expect(rows).toHaveLength(2);
    });

    // The cap must count EVENTS, not documents: dedup runs before the slice, so
    // one chatty event can no longer crowd out three unrelated ones.
    it('applies the row cap after dedup, not before', () => {
      const noisy = [1, 2, 3, 4, 5].map((n) =>
        decision({
          decision_id: `noise${n}`,
          action: 'iac_apply',
          apply_status: 'waiting_for_rebake',
          pr_number: 500,
          event_key: 'iac-apply-500-onegeneration',
          created_at: `2026-07-28T0${n}:00:00Z`,
        }),
      );
      const other = decision({ decision_id: 'other', created_at: '2026-07-28T06:00:00Z' });
      const rows = ledgerRows([...noisy, other], 4, { now: NOW, origin: ORIGIN });
      expect(rows).toHaveLength(2);
      expect(rows.map((r) => r.decision.decision_id)).toContain('other');
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
