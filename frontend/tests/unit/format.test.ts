import { describe, it, expect } from 'vitest';
import { fmtTokens, shortTrace, fmtPreview, fmtWhen, shortSha, iacStatusLabel, iacStatusHelp, decisionActionLabel, decisionActionHelp, iacApplyMeta, appliedAtDiffersMaterially, normalizeForSearch } from '../../src/lib/format';

describe('normalizeForSearch', () => {
  it('lowercases', () => {
    expect(normalizeForSearch('IaC Apply')).toBe('iac apply');
  });

  it('collapses every run of non-alphanumerics to a single space and trims', () => {
    expect(normalizeForSearch('  iac_apply  ')).toBe('iac apply');
    expect(normalizeForSearch('applied & merged')).toBe('applied merged');
    expect(normalizeForSearch('PR #168')).toBe('pr 168');
    expect(normalizeForSearch('waiting_for_rebake')).toBe('waiting for rebake');
  });

  it('preserves Unicode letters and digits (Japanese stays searchable)', () => {
    expect(normalizeForSearch('ドリフト 確認')).toBe('ドリフト 確認');
  });

  it('returns "" for null/undefined/empty/whitespace', () => {
    expect(normalizeForSearch(null)).toBe('');
    expect(normalizeForSearch(undefined)).toBe('');
    expect(normalizeForSearch('   ')).toBe('');
  });
});

describe('fmtTokens', () => {
  it('formats a present total with comma grouping and " tok" suffix', () => {
    expect(fmtTokens({ total_token_count: 1234 })).toBe('1,234 tok');
  });

  it('formats a small total with no grouping needed', () => {
    expect(fmtTokens({ total_token_count: 42 })).toBe('42 tok');
  });

  it('formats zero as "0 tok" (0 is a present value, not absent)', () => {
    expect(fmtTokens({ total_token_count: 0 })).toBe('0 tok');
  });

  it('formats large totals with multiple comma groups', () => {
    expect(fmtTokens({ total_token_count: 1234567 })).toBe('1,234,567 tok');
  });

  it('returns "" when total_token_count is null', () => {
    expect(fmtTokens({ total_token_count: null })).toBe('');
  });

  it('returns "" when total_token_count is undefined', () => {
    expect(fmtTokens({ total_token_count: undefined })).toBe('');
  });

  it('returns "" when the field is absent entirely', () => {
    expect(fmtTokens({})).toBe('');
  });
});

describe('shortTrace', () => {
  it('returns the first 8 chars of a long trace id', () => {
    expect(shortTrace('0123456789abcdef0123456789abcdef')).toBe('01234567');
  });

  it('returns the whole string when shorter than 8 chars', () => {
    expect(shortTrace('abc')).toBe('abc');
  });

  it('returns exactly 8 chars when input is exactly 8 chars', () => {
    expect(shortTrace('abcdef12')).toBe('abcdef12');
  });

  it('handles an empty string safely', () => {
    expect(shortTrace('')).toBe('');
  });

  it('handles null/undefined input safely (returns "")', () => {
    // Defensive: callers may pass an unset trace id.
    expect(shortTrace(null as unknown as string)).toBe('');
    expect(shortTrace(undefined as unknown as string)).toBe('');
  });
});

describe('shortSha', () => {
  it('returns the first 7 chars of a commit sha', () => {
    expect(shortSha('0496b305deadbeefcafe')).toBe('0496b30');
  });

  it('returns the whole string when shorter than 7 chars', () => {
    expect(shortSha('abc')).toBe('abc');
  });

  it('returns "" for empty / null / undefined / non-string input', () => {
    expect(shortSha('')).toBe('');
    expect(shortSha(null as unknown as string)).toBe('');
    expect(shortSha(undefined as unknown as string)).toBe('');
    expect(shortSha(123 as unknown as string)).toBe('');
  });
});

describe('fmtPreview', () => {
  it('returns short input unchanged (no ellipsis)', () => {
    expect(fmtPreview('hello world')).toBe('hello world');
  });

  it('returns input unchanged when exactly at the default max (2000)', () => {
    const s = 'x'.repeat(2000);
    expect(fmtPreview(s)).toBe(s);
  });

  it('truncates to max chars and appends an ellipsis when longer than default max', () => {
    const s = 'x'.repeat(2001);
    const out = fmtPreview(s);
    expect(out).toBe('x'.repeat(2000) + '…');
    expect(out.length).toBe(2001);
  });

  it('honors a custom max and appends an ellipsis when truncated', () => {
    expect(fmtPreview('abcdef', 3)).toBe('abc…');
  });

  it('does not append an ellipsis when input length equals the custom max', () => {
    expect(fmtPreview('abc', 3)).toBe('abc');
  });

  it('does not append an ellipsis when input is shorter than the custom max', () => {
    expect(fmtPreview('ab', 3)).toBe('ab');
  });

  it('handles an empty string', () => {
    expect(fmtPreview('')).toBe('');
  });

  it('handles a custom max of 0 (empty truncation with ellipsis for non-empty input)', () => {
    expect(fmtPreview('abc', 0)).toBe('…');
    expect(fmtPreview('', 0)).toBe('');
  });

  it('handles null/undefined input safely (returns "")', () => {
    expect(fmtPreview(null as unknown as string)).toBe('');
    expect(fmtPreview(undefined as unknown as string)).toBe('');
  });
});

describe('fmtWhen', () => {
  it('formats a valid ISO timestamp into a readable string with the year', () => {
    const out = fmtWhen('2026-05-31T08:27:45.434428+00:00');
    // Locale/tz-dependent exact text; assert it parsed (year present) and is not
    // the raw ISO string.
    expect(out).toContain('2026');
    expect(out).not.toContain('T08:27');
  });

  it('returns "" for an empty string', () => {
    expect(fmtWhen('')).toBe('');
  });

  it('returns the raw value when it does not parse', () => {
    expect(fmtWhen('not-a-date')).toBe('not-a-date');
  });

  it('handles null/undefined input safely (returns "")', () => {
    expect(fmtWhen(null as unknown as string)).toBe('');
    expect(fmtWhen(undefined as unknown as string)).toBe('');
  });
});

describe('iacStatusLabel', () => {
  it('maps each known apply_status to its readable phrase', () => {
    expect(iacStatusLabel('applied')).toBe('applied');
    // Operator-facing label is plain "rebuild" (the internal enum stays
    // `waiting_for_rebake`); the cryptic insider term "re-bake" is gone.
    expect(iacStatusLabel('waiting_for_rebake')).toBe('awaiting rebuild');
    expect(iacStatusLabel('failed')).toBe('failed');
    // Codex must-fix: failed_state_suspect is a real backend-emitted status.
    expect(iacStatusLabel('failed_state_suspect')).toBe('failed (state suspect)');
    expect(iacStatusLabel('ambiguous')).toBe('ambiguous');
  });

  it('passes an unrecognised non-empty status through verbatim', () => {
    expect(iacStatusLabel('some_new_status')).toBe('some_new_status');
  });

  it('clamps an over-long unknown status to 40 chars + ellipsis', () => {
    const long = 'x'.repeat(60);
    const out = iacStatusLabel(long);
    expect(out).toBe('x'.repeat(40) + '…');
    expect(out.length).toBe(41);
  });

  it('passes an unknown status of exactly 40 chars through without an ellipsis', () => {
    const exact = 'y'.repeat(40);
    expect(iacStatusLabel(exact)).toBe(exact);
  });

  it('returns "" for empty / null / undefined', () => {
    expect(iacStatusLabel('')).toBe('');
    expect(iacStatusLabel(null)).toBe('');
    expect(iacStatusLabel(undefined)).toBe('');
  });
});

describe('iacStatusHelp', () => {
  it('returns plain-language help for the cryptic statuses', () => {
    for (const status of ['waiting_for_rebake', 'failed_state_suspect', 'ambiguous', 'failed']) {
      const help = iacStatusHelp(status);
      expect(typeof help).toBe('string');
      expect((help as string).length).toBeGreaterThan(20);
    }
  });

  it('explains rebuild-of-what for waiting_for_rebake (not a circular "re-bake")', () => {
    const help = iacStatusHelp('waiting_for_rebake') as string;
    expect(help.toLowerCase()).toContain('rebuilt');
    expect(help.toLowerCase()).toContain('worker');
    // Must not define the term using the very jargon we removed from the label.
    expect(help.toLowerCase()).not.toContain('re-bake');
  });

  it('explains failed as state-proven-clean with a clear retry next-step', () => {
    const help = iacStatusHelp('failed') as string;
    expect(typeof help).toBe('string');
    // The distinguishing fact vs failed_state_suspect: live state was left untouched...
    expect(help.toLowerCase()).toContain('unchanged');
    // ...with an actionable next step.
    expect(help.toLowerCase()).toContain('retry');
    // The OpenTofu error is surfaced nowhere operator-facing (captured stderr, only a
    // 500-char tail in the isolated apply-audit), so the copy must not promise a
    // location — not the coordinator-scoped trace.
    expect(help.toLowerCase()).not.toContain('open the trace');
  });

  it('returns null for self-evident statuses and unknown values', () => {
    expect(iacStatusHelp('applied')).toBeNull();
    expect(iacStatusHelp('some_new_status')).toBeNull();
  });

  it('returns null for empty / null / undefined', () => {
    expect(iacStatusHelp('')).toBeNull();
    expect(iacStatusHelp(null)).toBeNull();
    expect(iacStatusHelp(undefined)).toBeNull();
  });
});

describe('decisionActionLabel', () => {
  it('remaps no_op from the bare enum to plain language', () => {
    expect(decisionActionLabel('no_op')).toBe('No action needed');
  });

  it('passes other action tokens through verbatim (those rows carry their own CTA)', () => {
    expect(decisionActionLabel('docs_pr')).toBe('docs_pr');
    expect(decisionActionLabel('drift_issue')).toBe('drift_issue');
    expect(decisionActionLabel('escalation')).toBe('escalation');
    expect(decisionActionLabel('rollback')).toBe('rollback');
  });

  it('clamps an over-long unknown action to 40 chars + ellipsis', () => {
    const long = 'x'.repeat(60);
    const out = decisionActionLabel(long);
    expect(out).toBe('x'.repeat(40) + '…');
    expect(out.length).toBe(41);
  });

  it('returns "" for empty / null / undefined', () => {
    expect(decisionActionLabel('')).toBe('');
    expect(decisionActionLabel(null)).toBe('');
    expect(decisionActionLabel(undefined)).toBe('');
  });
});

describe('decisionActionHelp', () => {
  it('explains the no_op "checked, all clear" receipt in plain language', () => {
    const help = decisionActionHelp('no_op') as string;
    expect(typeof help).toBe('string');
    expect(help.length).toBeGreaterThan(20);
    // The core reassurance: nothing was wrong / matched what was expected...
    expect(help.toLowerCase()).toContain('matched');
    // ...and it explicitly names that no side effect was produced.
    expect(help.toLowerCase()).toContain('nothing');
  });

  it('returns null for actions that need no explanation', () => {
    expect(decisionActionHelp('docs_pr')).toBeNull();
    expect(decisionActionHelp('iac_apply')).toBeNull();
    expect(decisionActionHelp('rollback')).toBeNull();
  });

  it('returns null for empty / null / undefined', () => {
    expect(decisionActionHelp('')).toBeNull();
    expect(decisionActionHelp(null)).toBeNull();
    expect(decisionActionHelp(undefined)).toBeNull();
  });
});

describe('iacApplyMeta — merge-aware status for the rail', () => {
  it('applied + merged → terminal "done" with ok tone and help', () => {
    const m = iacApplyMeta('applied', 'merged');
    expect(m.label).toBe('applied & merged');
    expect(m.tone).toBe('ok');
    expect(m.done).toBe(true);
    expect(typeof m.help).toBe('string');
    expect((m.help as string).toLowerCase()).toContain('nothing more to do');
  });

  it('applied + failed → merge pending (warn, not done)', () => {
    const m = iacApplyMeta('applied', 'failed');
    expect(m.label).toBe('applied · merge pending');
    expect(m.tone).toBe('warn');
    expect(m.done).toBe(false);
    expect((m.help as string).toLowerCase()).toContain("hasn't merged");
    // Must NOT promise a plain retry fixes a permanent branch-protection block.
    expect((m.help as string).toLowerCase()).toContain('branch-protection');
  });

  it('applied + pending → merge pending too (forward-compat, not plain "applied")', () => {
    const m = iacApplyMeta('applied', 'pending');
    expect(m.label).toBe('applied · merge pending');
    expect(m.tone).toBe('warn');
    expect(m.done).toBe(false);
  });

  it('applied with no/unknown merge_state → neutral "applied" (cannot claim done)', () => {
    for (const ms of [undefined, null, '', 'n/a', 'weird']) {
      const m = iacApplyMeta('applied', ms);
      expect(m.label).toBe('applied');
      expect(m.tone).toBe('');
      expect(m.done).toBe(false);
      expect(m.help).toBeNull();
    }
  });

  it('non-applied statuses reuse the existing label/help; tone mirrors decision.ts', () => {
    expect(iacApplyMeta('failed', 'n/a')).toMatchObject({ tone: 'danger', done: false });
    expect(iacApplyMeta('failed_state_suspect', 'n/a').tone).toBe('danger');
    expect(iacApplyMeta('ambiguous', 'n/a').tone).toBe('warn'); // mirrors decision.ts (not danger)
    const wait = iacApplyMeta('waiting_for_rebake', 'pending');
    expect(wait.label).toBe('awaiting rebuild');
    expect(wait.tone).toBe(''); // neutral — carries its own label + help
    expect(typeof wait.help).toBe('string');
  });

  it('tolerates null/undefined apply_status', () => {
    expect(iacApplyMeta(null, null)).toMatchObject({ label: '', tone: '', help: null, done: false });
    expect(iacApplyMeta(undefined, undefined).done).toBe(false);
  });
});

describe('appliedAtDiffersMaterially — chronology cue gate', () => {
  it('true when applied_at and created_at differ by ≥ the threshold', () => {
    expect(
      appliedAtDiffersMaterially('2026-05-30T11:16:12Z', '2026-06-26T16:03:27Z'),
    ).toBe(true);
  });

  it('false when within the threshold (same apply/activity moment)', () => {
    expect(
      appliedAtDiffersMaterially('2026-06-26T16:03:00Z', '2026-06-26T16:03:27Z'),
    ).toBe(false);
  });

  it('respects a custom threshold', () => {
    // 2h apart: false at the 24h default, true at a 1h threshold.
    const a = '2026-06-26T10:00:00Z';
    const c = '2026-06-26T12:00:00Z';
    expect(appliedAtDiffersMaterially(a, c)).toBe(false);
    expect(appliedAtDiffersMaterially(a, c, 3_600_000)).toBe(true);
  });

  it('false for any unparseable / missing input (no cue)', () => {
    expect(appliedAtDiffersMaterially('nope', '2026-06-26T16:03:27Z')).toBe(false);
    expect(appliedAtDiffersMaterially('2026-06-26T16:03:27Z', undefined)).toBe(false);
    expect(appliedAtDiffersMaterially(null, null)).toBe(false);
  });
});
