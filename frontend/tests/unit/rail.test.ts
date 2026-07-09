import { describe, it, expect } from 'vitest';
import {
  groupRailDecisions,
  lifecycleSummaryLabel,
  hasAnomalousStep,
  railRowIcon,
  showPrNumberingHint,
  matchesDecision,
  railItemMatches,
  capRailItems,
  traceButtonLabel,
  type RailItem,
} from '../../src/lib/rail';
import type { Decision } from '../../src/lib/types';

// Fixture shapes mirror live /decisions data (2026-06-10): newest-first,
// create-class lifecycles = applied + waiting_for_rebake×2 per PR, and the
// PR-32 failed→applied retry. The paired waiting docs share one trace_id
// (written seconds apart in the same request) — faithful, not a test shortcut.

function iac(id: string, pr: number | undefined, status: string, over: Partial<Decision> = {}): Decision {
  return {
    decision_id: id,
    action: 'iac_apply',
    pr_number: pr,
    apply_status: status,
    // Real /decisions rows always carry the server-derived GitHub link; include
    // it so showPrNumberingHint (gated on iacPrHref) sees a numbered row. Tests
    // that exercise the missing-link path override `github` via `over`.
    github: { url: `https://github.com/adi-prasetyo/driftscribe/pull/${pr ?? 'x'}` },
    ...over,
  } as Decision;
}
const other = (id: string): Decision => ({ decision_id: id, action: 'drift_issue' } as Decision);

describe('groupRailDecisions', () => {
  it('folds same-PR iac_apply docs into one group anchored at the newest doc, newest-first inside', () => {
    const a = iac('a', 68, 'applied');
    const w1 = iac('w1', 68, 'waiting_for_rebake');
    const w2 = iac('w2', 68, 'waiting_for_rebake');
    const items = groupRailDecisions([a, w1, w2]);
    expect(items).toEqual([{ kind: 'group', pr: 68, docs: [a, w1, w2] }]);
  });

  it('preserves overall newest-first order: groups sit where their newest doc sat, singles in place', () => {
    const o1 = other('o1');
    const a68 = iac('a68', 68, 'applied');
    const w68 = iac('w68', 68, 'waiting_for_rebake');
    const a32 = iac('a32', 32, 'applied');
    const f32 = iac('f32', 32, 'failed');
    const items = groupRailDecisions([a68, o1, w68, a32, f32]);
    expect(items).toEqual([
      { kind: 'group', pr: 68, docs: [a68, w68] },
      { kind: 'single', d: o1 },
      { kind: 'group', pr: 32, docs: [a32, f32] },
    ]);
  });

  it('keeps a lone iac_apply doc as a single (no 1-doc groups)', () => {
    const w = iac('w', 71, 'waiting_for_rebake');
    expect(groupRailDecisions([w])).toEqual([{ kind: 'single', d: w }]);
  });

  it('a lone valid iac doc stays a single alongside a grouped PR (count-then-emit interaction)', () => {
    // Live shape: PR 68 has a multi-doc lifecycle while PR 71 has exactly one
    // doc — the count pass must keep 71 a single while 68 folds.
    const a68 = iac('a68', 68, 'applied');
    const w68 = iac('w68', 68, 'waiting_for_rebake');
    const loneW71 = iac('loneW71', 71, 'waiting_for_rebake');
    expect(groupRailDecisions([a68, w68, loneW71])).toEqual([
      { kind: 'group', pr: 68, docs: [a68, w68] },
      { kind: 'single', d: loneW71 },
    ]);
  });

  it('never groups iac docs with a missing/invalid pr_number (mirrors the iacApprovalHref guard)', () => {
    const bad1 = iac('b1', undefined, 'applied');
    const bad2 = iac('b2', undefined, 'applied');
    const zero1 = iac('z1', 0, 'applied');
    const zero2 = iac('z2', 0, 'applied');
    const frac1 = iac('f1', 1.5, 'applied');
    const frac2 = iac('f2', 1.5, 'applied');
    const items = groupRailDecisions([bad1, bad2, zero1, zero2, frac1, frac2]);
    expect(items.every((i) => i.kind === 'single')).toBe(true);
    expect(items).toHaveLength(6);
  });

  it('never groups non-iac actions even when they carry a pr_number (docs_pr etc.)', () => {
    const d1 = { decision_id: 'd1', action: 'docs_pr', pr_number: 9 } as Decision;
    const d2 = { decision_id: 'd2', action: 'docs_pr', pr_number: 9 } as Decision;
    expect(groupRailDecisions([d1, d2])).toEqual([
      { kind: 'single', d: d1 },
      { kind: 'single', d: d2 },
    ]);
  });

  it('tolerates null/undefined list and entries (matches resolvedIacPrNumbers style)', () => {
    expect(groupRailDecisions(null)).toEqual([]);
    expect(groupRailDecisions(undefined)).toEqual([]);
    const a = iac('a', 68, 'applied');
    const w = iac('w', 68, 'waiting_for_rebake');
    // null entries are dropped, not crashed on and not rendered.
    expect(groupRailDecisions([a, null, w] as unknown as Decision[])).toEqual([
      { kind: 'group', pr: 68, docs: [a, w] },
    ]);
  });

  it('does not assume contiguity: same-PR docs separated by other rows still fold into one group', () => {
    const a = iac('a', 47, 'applied');
    const o = other('o');
    const w = iac('w', 47, 'waiting_for_rebake');
    expect(groupRailDecisions([a, o, w])).toEqual([
      { kind: 'group', pr: 47, docs: [a, w] },
      { kind: 'single', d: o },
    ]);
  });
});

describe('lifecycleSummaryLabel', () => {
  // `earlier` is docs.slice(1) in list (newest-first) order — the helper owns
  // the oldest-first presentation ordering.
  it('renders count + status composition for the live create-class shape', () => {
    const earlier = [iac('w1', 68, 'waiting_for_rebake'), iac('w2', 68, 'waiting_for_rebake')];
    expect(lifecycleSummaryLabel(earlier)).toBe('2 earlier steps · awaiting rebuild ×2');
  });

  it('singular wording + bare label for one step (PR-32 failed→applied shape)', () => {
    expect(lifecycleSummaryLabel([iac('f', 32, 'failed')])).toBe('1 earlier step · failed');
  });

  it('multi-status composition is ordered by first appearance oldest-first', () => {
    // newest-first input: waiting (newer), failed (oldest) → oldest-first = failed first.
    const earlier = [iac('w', 9, 'waiting_for_rebake'), iac('f', 9, 'failed')];
    expect(lifecycleSummaryLabel(earlier)).toBe('2 earlier steps · failed, awaiting rebuild');
  });

  it('a missing/empty status renders the neutral token, never the action string', () => {
    expect(lifecycleSummaryLabel([iac('x', 9, '')])).toBe('1 earlier step · status not recorded');
  });

  it('is merge-aware: applied steps read their merge outcome, not a bare "applied"', () => {
    expect(
      lifecycleSummaryLabel([iac('a', 32, 'applied', { merge_state: 'merged' })]),
    ).toBe('1 earlier step · applied & merged');
    expect(
      lifecycleSummaryLabel([iac('a', 32, 'applied', { merge_state: 'failed' })]),
    ).toBe('1 earlier step · applied · merge pending');
  });

  it('a superseded_by_pr earlier step composes as "superseded", not "awaiting rebuild"', () => {
    expect(
      lifecycleSummaryLabel([
        iac('w', 216, 'waiting_for_rebake', { superseded_by_pr: 221 }),
      ]),
    ).toBe('1 earlier step · superseded');
  });
});

describe('hasAnomalousStep', () => {
  it('calm: applied / waiting_for_rebake steps are not anomalous', () => {
    expect(hasAnomalousStep([iac('w', 68, 'waiting_for_rebake'), iac('a', 68, 'applied')])).toBe(false);
  });

  it('failed / failed_state_suspect / ambiguous are anomalous', () => {
    expect(hasAnomalousStep([iac('f', 32, 'failed')])).toBe(true);
    expect(hasAnomalousStep([iac('f', 32, 'failed_state_suspect')])).toBe(true);
    expect(hasAnomalousStep([iac('f', 32, 'ambiguous')])).toBe(true);
  });

  it('fails OPEN to visible: unknown and missing statuses count as anomalous', () => {
    expect(hasAnomalousStep([iac('u', 9, 'something_new')])).toBe(true);
    expect(hasAnomalousStep([iac('m', 9, '')])).toBe(true);
    expect(hasAnomalousStep([{ decision_id: 'n', action: 'iac_apply', pr_number: 9 } as Decision])).toBe(true);
  });

  it('an applied step whose merge is still pending (failed/pending) is anomalous', () => {
    // The expanded step shows "merge pending"; the collapsed summary must not hide
    // it behind a closed expander — open so it's visible without a click.
    expect(hasAnomalousStep([iac('a', 32, 'applied', { merge_state: 'failed' })])).toBe(true);
    expect(hasAnomalousStep([iac('a', 32, 'applied', { merge_state: 'pending' })])).toBe(true);
  });

  it('an applied + merged (done) step is calm', () => {
    expect(hasAnomalousStep([iac('a', 32, 'applied', { merge_state: 'merged' })])).toBe(false);
  });

  it('empty list is calm', () => {
    expect(hasAnomalousStep([])).toBe(false);
  });
});

describe('railRowIcon', () => {
  // Fail-safe: null / undefined / empty → file-text
  it('returns file-text for null', () => {
    expect(railRowIcon(null)).toBe('file-text');
  });
  it('returns file-text for undefined', () => {
    expect(railRowIcon(undefined)).toBe('file-text');
  });
  it('returns file-text for empty string', () => {
    expect(railRowIcon('')).toBe('file-text');
  });

  // rollback branch
  it('returns rotate-ccw for "rollback"', () => {
    expect(railRowIcon('rollback')).toBe('rotate-ccw');
  });
  it('returns rotate-ccw for an action that contains "rollback" (e.g. "auto_rollback")', () => {
    expect(railRowIcon('auto_rollback')).toBe('rotate-ccw');
  });

  // iac branch
  it('returns git-merge for "iac_apply"', () => {
    expect(railRowIcon('iac_apply')).toBe('git-merge');
  });
  it('returns git-merge for any action containing "iac"', () => {
    expect(railRowIcon('check_iac')).toBe('git-merge');
  });

  // upgrade / pr branch
  it('returns git-pull-request for "upgrade_pr"', () => {
    expect(railRowIcon('upgrade_pr')).toBe('git-pull-request');
  });
  it('returns git-pull-request for "docs_pr" (contains "pr")', () => {
    expect(railRowIcon('docs_pr')).toBe('git-pull-request');
  });
  it('returns git-pull-request for an action that contains "upgrade"', () => {
    expect(railRowIcon('upgrade')).toBe('git-pull-request');
  });

  // issue / drift / report branch
  it('returns alert-triangle for "drift_issue"', () => {
    expect(railRowIcon('drift_issue')).toBe('alert-triangle');
  });
  it('returns alert-triangle for "escalation" (contains "issue" — no, but word "report"/"drift"?)', () => {
    // "escalation" matches none of the keywords → file-text
    expect(railRowIcon('escalation')).toBe('file-text');
  });
  it('returns alert-triangle for an action containing "drift"', () => {
    expect(railRowIcon('drift')).toBe('alert-triangle');
  });
  it('returns alert-triangle for an action containing "report"', () => {
    expect(railRowIcon('cost_report')).toBe('alert-triangle');
  });
  it('returns alert-triangle for an action containing "issue"', () => {
    expect(railRowIcon('open_issue')).toBe('alert-triangle');
  });

  // rollback takes priority over pr (e.g. if action were "rollback_pr")
  it('rollback keyword beats pr keyword (priority: first-match)', () => {
    expect(railRowIcon('rollback_pr')).toBe('rotate-ccw');
  });

  // default fallback
  it('returns file-text for an unrecognised action like "no_op"', () => {
    expect(railRowIcon('no_op')).toBe('file-text');
  });
  it('returns file-text for "observe"', () => {
    expect(railRowIcon('observe')).toBe('file-text');
  });
});

describe('showPrNumberingHint', () => {
  // The header PR-numbering hint (why the numbers skip values) shows only once
  // there are ≥2 DISTINCT iac_apply PR numbers — with 0 or 1 there is no
  // sequence to explain. Counts DISTINCT numbers so a multi-doc lifecycle for
  // one PR (applied + waiting×2) does not, by itself, trip the hint.
  it('is false for an empty list (nothing to explain)', () => {
    expect(showPrNumberingHint([])).toBe(false);
  });

  it('is false for a single numbered row', () => {
    expect(showPrNumberingHint([iac('a', 68, 'applied')])).toBe(false);
  });

  it('is false when several docs all share ONE pr_number (distinct count is 1)', () => {
    const lifecycle = [
      iac('a', 68, 'applied'),
      iac('w1', 68, 'waiting_for_rebake'),
      iac('w2', 68, 'waiting_for_rebake'),
    ];
    expect(showPrNumberingHint(lifecycle)).toBe(false);
  });

  it('is true once there are two distinct iac_apply PR numbers', () => {
    expect(showPrNumberingHint([iac('a', 68, 'applied'), iac('b', 71, 'applied')])).toBe(true);
  });

  it('ignores non-iac_apply decisions when counting distinct numbers', () => {
    // drift_issue has no pr_number; with only ONE real iac number, no hint.
    expect(showPrNumberingHint([other('o'), iac('a', 68, 'applied')])).toBe(false);
    // …but two real iac numbers alongside other rows DO trip it.
    expect(showPrNumberingHint([other('o'), iac('a', 68, 'applied'), iac('b', 102, 'applied')])).toBe(
      true,
    );
  });

  it('ignores iac rows with a missing/invalid pr_number (mirrors the grouping guard)', () => {
    expect(showPrNumberingHint([iac('a', 68, 'applied'), iac('b', undefined, 'applied')])).toBe(false);
    expect(showPrNumberingHint([iac('a', 68, 'applied'), iac('z', 0, 'applied')])).toBe(false);
  });

  it('counts only rows that render a linked PR # — a number with no usable GitHub link is excluded', () => {
    // Fail-soft attach_iac_pr_link shape: pr_number present, github.url null ⇒
    // the row renders as plain `iac_apply`, so it must not inflate the count.
    const linked = iac('a', 68, 'applied');
    const noLink = iac('b', 71, 'applied', { github: { url: null } });
    expect(showPrNumberingHint([linked, noLink])).toBe(false); // only #68 is numbered
    expect(showPrNumberingHint([linked, noLink, iac('c', 95, 'applied')])).toBe(true); // #68 + #95
  });

  it('tolerates null/undefined input', () => {
    expect(showPrNumberingHint(null as unknown as Decision[])).toBe(false);
    expect(showPrNumberingHint(undefined as unknown as Decision[])).toBe(false);
  });
});

describe('matchesDecision', () => {
  it('matches on the PR title', () => {
    const d = iac('a', 168, 'applied', { pr_title: 'Adopt the probe topic' });
    expect(matchesDecision(d, 'probe')).toBe(true);
    expect(matchesDecision(d, 'rollback')).toBe(false);
  });

  it('matches the PR number in every displayed/typed form', () => {
    const d = iac('a', 168, 'applied');
    expect(matchesDecision(d, '168')).toBe(true);
    expect(matchesDecision(d, '#168')).toBe(true);
    expect(matchesDecision(d, 'PR 168')).toBe(true);
    expect(matchesDecision(d, 'pr #168')).toBe(true);
  });

  it('matches the action raw and friendly', () => {
    const noop = { decision_id: 'n', action: 'no_op' } as Decision;
    expect(matchesDecision(noop, 'no_op')).toBe(true);
    expect(matchesDecision(noop, 'no action')).toBe(true); // decisionActionLabel
  });

  it('matches the crew raw value and display name', () => {
    const d = iac('a', 1, 'applied', { workload: 'drift' } as Partial<Decision>);
    expect(matchesDecision(d, 'drift')).toBe(true);
    expect(matchesDecision(d, 'anchor')).toBe(true);
  });

  it('matches the status raw and the friendly label', () => {
    const d = iac('a', 1, 'applied', { merge_state: 'merged' });
    expect(matchesDecision(d, 'merged')).toBe(true);
    expect(matchesDecision(d, 'applied merged')).toBe(true); // iacApplyMeta label
    const pending = iac('b', 2, 'applied', { merge_state: 'failed' });
    expect(matchesDecision(pending, 'merge pending')).toBe(true);
  });

  it('an empty query matches everything', () => {
    expect(matchesDecision(other('x'), '')).toBe(true);
    expect(matchesDecision(other('x'), '   ')).toBe(true);
  });
});

describe('railItemMatches', () => {
  it('a single defers to its decision', () => {
    const item: RailItem = { kind: 'single', d: iac('a', 168, 'applied', { pr_title: 'probe' }) };
    expect(railItemMatches(item, 'probe')).toBe(true);
    expect(railItemMatches(item, 'nope')).toBe(false);
  });

  it('a group matches when ANY folded doc matches (status only on an earlier doc)', () => {
    const face = iac('f', 70, 'applied', { merge_state: 'merged' });
    const earlier = iac('e', 70, 'failed');
    const item: RailItem = { kind: 'group', pr: 70, docs: [face, earlier] };
    expect(railItemMatches(item, 'failed')).toBe(true); // only the earlier doc
    expect(railItemMatches(item, '70')).toBe(true); // shared, hits the face
  });

  it('an empty query matches everything', () => {
    const item: RailItem = { kind: 'single', d: other('x') };
    expect(railItemMatches(item, '')).toBe(true);
  });
});

describe('traceButtonLabel', () => {
  // iac_apply decisions are recorded directly by the approval handler (no
  // coordinator reasoning run), so their button reads "view details →";
  // everything else keeps the reasoning-backed label.
  it('returns "view details →" for iac_apply', () => {
    expect(traceButtonLabel('iac_apply')).toBe('view details →');
  });

  it('returns "view reasoning →" for rollback', () => {
    expect(traceButtonLabel('rollback')).toBe('view reasoning →');
  });

  it('returns "view reasoning →" for recheck', () => {
    expect(traceButtonLabel('recheck')).toBe('view reasoning →');
  });

  it('returns "view reasoning →" for null/undefined', () => {
    expect(traceButtonLabel(null)).toBe('view reasoning →');
    expect(traceButtonLabel(undefined)).toBe('view reasoning →');
  });
});

describe('capRailItems', () => {
  const items: RailItem[] = Array.from(
    { length: 14 },
    (_, i): RailItem => ({ kind: 'single', d: iac(`d${i}`, i + 1, 'applied', { trace_id: `t${i}` }) }),
  );

  it('returns the list unchanged when it fits within max', () => {
    expect(capRailItems(items.slice(0, 6), 10, null)).toHaveLength(6);
  });

  it('keeps only the newest max when nothing active is hidden', () => {
    expect(capRailItems(items, 10, null)).toHaveLength(10);
  });

  it('appends the active item (by trace_id) when it falls outside the cap', () => {
    const out = capRailItems(items, 10, 't13');
    expect(out).toHaveLength(11);
    expect(out[10]).toBe(items[13]);
  });

  it('matches the active trace inside a group, and does not duplicate when already visible', () => {
    const grouped: RailItem[] = [
      ...items.slice(0, 10),
      { kind: 'group', pr: 999, docs: [iac('g0', 999, 'applied', { trace_id: 'gx' }), iac('g1', 999, 'failed', { trace_id: 'gy' })] },
    ];
    const out = capRailItems(grouped, 10, 'gy');
    expect(out).toHaveLength(11);
    expect(capRailItems(items, 10, 't3')).toHaveLength(10); // active already visible
  });
});
