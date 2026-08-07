import { describe, it, expect } from 'vitest';
import { fmtTokens, shortTrace, fmtPreview, fmtWhen, fmtClock, fmtStamp, sameDay, shortSha, iacStatusLabel, iacStatusHelp, decisionActionLabel, decisionActionHelp, contractStatusLabel, iacApplyMeta, appliedAtDiffersMaterially, normalizeForSearch } from '../../src/lib/format';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// The whole suite asserts English (the shared.en catalog is byte-for-byte the
// original inline text these functions used to return), so every changed
// helper below is called with an EN-bound translator.
const t: TranslateFn = (k, p) => translate('en', k, p);

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
    expect(fmtTokens({ total_token_count: 1234 }, t, 'en')).toBe('1,234 tok');
  });

  it('formats a small total with no grouping needed', () => {
    expect(fmtTokens({ total_token_count: 42 }, t, 'en')).toBe('42 tok');
  });

  it('formats zero as "0 tok" (0 is a present value, not absent)', () => {
    expect(fmtTokens({ total_token_count: 0 }, t, 'en')).toBe('0 tok');
  });

  it('formats large totals with multiple comma groups', () => {
    expect(fmtTokens({ total_token_count: 1234567 }, t, 'en')).toBe('1,234,567 tok');
  });

  it('returns "" when total_token_count is null', () => {
    expect(fmtTokens({ total_token_count: null }, t, 'en')).toBe('');
  });

  it('returns "" when total_token_count is undefined', () => {
    expect(fmtTokens({ total_token_count: undefined }, t, 'en')).toBe('');
  });

  it('returns "" when the field is absent entirely', () => {
    expect(fmtTokens({}, t, 'en')).toBe('');
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

  it('pins a 24-hour clock in EN, matching fmtClock', () => {
    // en-US defaults to 12-hour, which put "03:13 PM" from this function
    // directly above fmtClock's "15:06" for the SAME decision on the desk's
    // stamped card. Asserted on both an afternoon and a morning timestamp: a
    // morning one would read "09:xx" under EITHER cycle, so it alone could not
    // tell h23 from h12 — only the afternoon case has teeth, and the morning
    // case guards against someone "fixing" this by force-padding to 24h wrongly.
    expect(fmtWhen('2026-05-31T15:06:00Z', 'en')).not.toMatch(/AM|PM/);
    expect(fmtWhen('2026-05-31T09:06:00Z', 'en')).not.toMatch(/AM|PM/);
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

describe('fmtClock', () => {
  const ISO = '2026-07-28T09:15:00Z';

  it('returns "" for an empty string', () => {
    expect(fmtClock('')).toBe('');
  });

  it('returns the raw value when it does not parse', () => {
    expect(fmtClock('not-a-date')).toBe('not-a-date');
  });

  // The actual HH:mm digits are host-timezone-dependent (fmtClock pins no
  // zone — see its doc comment) — like fmtWhen above, this suite does not
  // pin a TZ, so we assert the FORMAT (24-hour, zero-padded, no AM/PM), not
  // an absolute clock value that would only hold on one machine/CI runner.
  it('formats as 24-hour HH:mm with no AM/PM, in EITHER app locale', () => {
    const en = fmtClock(ISO, 'en');
    const ja = fmtClock(ISO, 'ja');
    expect(en).toMatch(/^\d{2}:\d{2}$/);
    expect(ja).toMatch(/^\d{2}:\d{2}$/);
    expect(en).not.toMatch(/AM|PM/i);
    expect(ja).not.toMatch(/AM|PM/i);
  });

  // `localeTag('en')` is 'en-US', whose default hour cycle is 12-hour with an
  // AM/PM suffix — this is the exact regression `hourCycle: 'h23'` guards
  // against (see fmtClock's doc comment). Pinned to the SAME instant as the
  // locale-shape test above; both locales must render the identical digits
  // now that hourCycle is fixed rather than locale-default.
  it('EN and JA render the identical string for the same instant', () => {
    expect(fmtClock(ISO, 'en')).toBe(fmtClock(ISO, 'ja'));
  });
});

describe('fmtStamp', () => {
  // 14:32 UTC — an AFTERNOON instant on purpose. In the morning h12 and h23
  // print the same digits and differ only by a suffix, so a morning fixture
  // would still pass against a 12-hour clock in most of the world.
  const PM = '2026-07-28T14:32:00Z';

  it('returns "" when there is no timestamp, and the raw value when it will not parse', () => {
    // The rails hand it optional fields directly; neither absent nor garbage
    // may render as "Invalid Date" on a card.
    expect(fmtStamp('')).toBe('');
    expect(fmtStamp(undefined)).toBe('');
    expect(fmtStamp('not-a-date')).toBe('not-a-date');
  });

  it('carries a date AND a 24-hour clock, with no AM/PM in either locale', () => {
    for (const l of ['en', 'ja'] as const) {
      const out = fmtStamp(PM, l);
      expect(out).not.toMatch(/AM|PM/i);
      // A date part, so it is distinguishable from fmtClock's bare HH:mm —
      // which is the whole reason ConversationThread has two formatters.
      expect(out).not.toMatch(/^\d{2}:\d{2}$/);
      expect(out.length).toBeGreaterThan(5);
    }
  });

  it('agrees with fmtClock on the hour, for the same instant', () => {
    // The regression this function was consolidated to kill (ds-jns PR 3): the
    // conversations rail printed 'Jul 28, 02:32 PM' from its own unpinned copy
    // while the thread turn it links to printed '14:32' — one instant, two
    // clock conventions, both on screen at once. Asserting the SHARED hour is
    // the claim; asserting an absolute value would pin the CI runner's zone.
    for (const l of ['en', 'ja'] as const) {
      const hour = fmtClock(PM, l).slice(0, 2);
      expect(fmtStamp(PM, l)).toContain(`${hour}:32`);
    }
  });
});

// ds-wd2.18 — lifted out of ConversationThread when LedgerStrip needed the same
// day-boundary rule. Every case is expressed in the READER's zone rather than a
// pinned one (see the function's own note), so the fixtures below are built from
// local-time components instead of literal `Z` strings wherever the assertion
// turns on which calendar day an instant lands in. A `Z` literal would make
// these pass or fail depending on the CI runner's timezone.
describe('sameDay', () => {
  /** An ISO string for a given local wall-clock moment, so the assertion is
   *  about the calendar day the READER sees — which is what the function
   *  claims to answer. */
  function localIso(y: number, m: number, d: number, h = 12, min = 0): string {
    return new Date(y, m - 1, d, h, min).toISOString();
  }

  it('true for two instants on the same local calendar day', () => {
    expect(sameDay(localIso(2026, 8, 5, 1, 15), localIso(2026, 8, 5, 23, 45))).toBe(true);
  });

  it('false across a local midnight, even minutes apart', () => {
    expect(sameDay(localIso(2026, 8, 5, 23, 59), localIso(2026, 8, 6, 0, 1))).toBe(false);
  });

  it('false across a month boundary and across a year boundary', () => {
    expect(sameDay(localIso(2026, 7, 31), localIso(2026, 8, 1))).toBe(false);
    expect(sameDay(localIso(2026, 12, 31), localIso(2027, 1, 1))).toBe(false);
  });

  // The one that a naive month/day comparison passes and must not: same day
  // number, same month, a year apart.
  it('false for the same month and day in different years', () => {
    expect(sameDay(localIso(2026, 8, 5), localIso(2027, 8, 5))).toBe(false);
  });

  // Fails toward the FULLER stamp, never toward a silent "these share a day".
  it('false when either side is missing or unparseable', () => {
    const good = localIso(2026, 8, 5);
    expect(sameDay(undefined, good)).toBe(false);
    expect(sameDay(good, undefined)).toBe(false);
    expect(sameDay('', good)).toBe(false);
    expect(sameDay('not-a-date', good)).toBe(false);
    expect(sameDay(good, 'not-a-date')).toBe(false);
    expect(sameDay(undefined, undefined)).toBe(false);
  });
});

describe('iacStatusLabel', () => {
  it('maps each known apply_status to its readable phrase', () => {
    expect(iacStatusLabel('applied', t)).toBe('applied');
    // Operator-facing label is plain "rebuild" (the internal enum stays
    // `waiting_for_rebake`); the cryptic insider term "re-bake" is gone.
    expect(iacStatusLabel('waiting_for_rebake', t)).toBe('awaiting apply');
    expect(iacStatusLabel('failed', t)).toBe('failed');
    // Codex must-fix: failed_state_suspect is a real backend-emitted status.
    expect(iacStatusLabel('failed_state_suspect', t)).toBe('failed (state suspect)');
    expect(iacStatusLabel('ambiguous', t)).toBe('ambiguous');
  });

  it('passes an unrecognised non-empty status through verbatim', () => {
    expect(iacStatusLabel('some_new_status', t)).toBe('some_new_status');
  });

  it('clamps an over-long unknown status to 40 chars + ellipsis', () => {
    const long = 'x'.repeat(60);
    const out = iacStatusLabel(long, t);
    expect(out).toBe('x'.repeat(40) + '…');
    expect(out.length).toBe(41);
  });

  it('passes an unknown status of exactly 40 chars through without an ellipsis', () => {
    const exact = 'y'.repeat(40);
    expect(iacStatusLabel(exact, t)).toBe(exact);
  });

  it('returns "" for empty / null / undefined', () => {
    expect(iacStatusLabel('', t)).toBe('');
    expect(iacStatusLabel(null, t)).toBe('');
    expect(iacStatusLabel(undefined, t)).toBe('');
  });
});

describe('iacStatusHelp', () => {
  it('returns plain-language help for the cryptic statuses', () => {
    for (const status of ['waiting_for_rebake', 'failed_state_suspect', 'ambiguous', 'failed']) {
      const help = iacStatusHelp(status, t);
      expect(typeof help).toBe('string');
      expect((help as string).length).toBeGreaterThan(20);
    }
  });

  it('explains rebuild-of-what for waiting_for_rebake (not a circular "re-bake")', () => {
    const help = iacStatusHelp('waiting_for_rebake', t) as string;
    expect(help.toLowerCase()).toContain('rebuilt');
    expect(help.toLowerCase()).toContain('worker');
    // Must not define the term using the very jargon we removed from the label.
    expect(help.toLowerCase()).not.toContain('re-bake');
  });

  it('explains failed as state-proven-clean with a clear retry next-step', () => {
    const help = iacStatusHelp('failed', t) as string;
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
    expect(iacStatusHelp('applied', t)).toBeNull();
    expect(iacStatusHelp('some_new_status', t)).toBeNull();
  });

  it('returns null for empty / null / undefined', () => {
    expect(iacStatusHelp('', t)).toBeNull();
    expect(iacStatusHelp(null, t)).toBeNull();
    expect(iacStatusHelp(undefined, t)).toBeNull();
  });
});

describe('decisionActionLabel', () => {
  it('remaps no_op from the bare enum to plain language', () => {
    expect(decisionActionLabel('no_op', t)).toBe('No action needed');
  });

  it('remaps the other two actions the backend actually writes', () => {
    // These used to pass through verbatim, which put a bare `rollback` — a
    // Latin-script code identifier — into Japanese operator copy on the desk's
    // ledger, where there is no CTA column to give the row context.
    expect(decisionActionLabel('rollback', t)).toBe('Rollback');
    expect(decisionActionLabel('iac_apply', t)).toBe('Infrastructure change');
  });

  it('passes an action this frontend has never heard of through verbatim', () => {
    // Forward-compat only: a newer coordinator writing a fourth kind. None of
    // these are values the backend writes today (the full set is no_op /
    // rollback / iac_apply), so this is the unknown-action path, not the
    // normal one.
    expect(decisionActionLabel('docs_pr', t)).toBe('docs_pr');
    expect(decisionActionLabel('drift_issue', t)).toBe('drift_issue');
    expect(decisionActionLabel('escalation', t)).toBe('escalation');
  });

  it('does not resolve an Object.prototype member as a mapped action', () => {
    // A decision doc is an open shape; a bare `KEYS[action]` lookup would turn
    // an action named `toString` into a truthy prototype member and hand that
    // function to t() as a translation key.
    expect(decisionActionLabel('toString', t)).toBe('toString');
    expect(decisionActionLabel('constructor', t)).toBe('constructor');
    expect(decisionActionLabel('__proto__', t)).toBe('__proto__');
  });

  it('clamps an over-long unknown action to 40 chars + ellipsis', () => {
    const long = 'x'.repeat(60);
    const out = decisionActionLabel(long, t);
    expect(out).toBe('x'.repeat(40) + '…');
    expect(out.length).toBe(41);
  });

  it('maps every ContractStatus the backend defines, and only those', () => {
    // The full set per agent/models.py:ContractStatus — if a fifth value is
    // added there, the unknown-passthrough case below is what it hits.
    expect(contractStatusLabel('match', t)).toBe('Matches the contract');
    expect(contractStatusLabel('present_allow_manual', t)).toBe('Manual change allowed');
    expect(contractStatusLabel('present_disallow_manual', t)).toBe('Manual change not allowed');
    expect(contractStatusLabel('absent', t)).toBe('Not in the contract');
    // None of these may survive as a raw snake_case identifier — that was the
    // bug: `present_disallow_manual` rendered verbatim in the STATUS column of
    // the judge-facing desk, in Latin script under Japanese copy.
    for (const s of ['match', 'present_allow_manual', 'present_disallow_manual', 'absent']) {
      expect(contractStatusLabel(s, t)).not.toContain('_');
    }
  });

  it('passes an unrecognized status through verbatim rather than blanking it', () => {
    // Honest failure mode for a future backend enum value: show the real thing
    // so the operator can look it up, never an empty cell or invented label.
    expect(contractStatusLabel('some_future_verdict', t)).toBe('some_future_verdict');
  });

  it('does not resolve an Object.prototype member as a mapped status', () => {
    expect(contractStatusLabel('toString', t)).toBe('toString');
    expect(contractStatusLabel('constructor', t)).toBe('constructor');
    expect(contractStatusLabel('__proto__', t)).toBe('__proto__');
  });

  it('returns "" for an empty / null / undefined status', () => {
    expect(contractStatusLabel('', t)).toBe('');
    expect(contractStatusLabel(null, t)).toBe('');
    expect(contractStatusLabel(undefined, t)).toBe('');
  });

  it('returns "" for empty / null / undefined', () => {
    expect(decisionActionLabel('', t)).toBe('');
    expect(decisionActionLabel(null, t)).toBe('');
    expect(decisionActionLabel(undefined, t)).toBe('');
  });
});

describe('decisionActionHelp', () => {
  it('explains the no_op "checked, all clear" receipt in plain language', () => {
    const help = decisionActionHelp('no_op', t) as string;
    expect(typeof help).toBe('string');
    expect(help.length).toBeGreaterThan(20);
    // The core reassurance: nothing was wrong / matched what was expected...
    expect(help.toLowerCase()).toContain('matched');
    // ...and it explicitly names that no side effect was produced.
    expect(help.toLowerCase()).toContain('nothing');
  });

  it('returns null for actions that need no explanation', () => {
    expect(decisionActionHelp('docs_pr', t)).toBeNull();
    expect(decisionActionHelp('iac_apply', t)).toBeNull();
    expect(decisionActionHelp('rollback', t)).toBeNull();
  });

  it('returns null for empty / null / undefined', () => {
    expect(decisionActionHelp('', t)).toBeNull();
    expect(decisionActionHelp(null, t)).toBeNull();
    expect(decisionActionHelp(undefined, t)).toBeNull();
  });
});

describe('iacApplyMeta — merge-aware status for the rail', () => {
  it('applied + merged → terminal "done" with ok tone and help', () => {
    const m = iacApplyMeta('applied', 'merged', undefined, t);
    expect(m.label).toBe('applied & merged');
    expect(m.tone).toBe('ok');
    expect(m.done).toBe(true);
    expect(typeof m.help).toBe('string');
    expect((m.help as string).toLowerCase()).toContain('nothing more to do');
  });

  it('applied + failed → merge pending (warn, not done)', () => {
    const m = iacApplyMeta('applied', 'failed', undefined, t);
    expect(m.label).toBe('applied · merge pending');
    expect(m.tone).toBe('warn');
    expect(m.done).toBe(false);
    expect((m.help as string).toLowerCase()).toContain("hasn't merged");
    // Must NOT promise a plain retry fixes a permanent branch-protection block.
    expect((m.help as string).toLowerCase()).toContain('branch-protection');
  });

  it('applied + pending → merge pending too (forward-compat, not plain "applied")', () => {
    const m = iacApplyMeta('applied', 'pending', undefined, t);
    expect(m.label).toBe('applied · merge pending');
    expect(m.tone).toBe('warn');
    expect(m.done).toBe(false);
  });

  it('applied with no/unknown merge_state → neutral "applied" (cannot claim done)', () => {
    for (const ms of [undefined, null, '', 'n/a', 'weird']) {
      const m = iacApplyMeta('applied', ms, undefined, t);
      expect(m.label).toBe('applied');
      expect(m.tone).toBe('');
      expect(m.done).toBe(false);
      expect(m.help).toBeNull();
    }
  });

  it('non-applied statuses reuse the existing label/help; tone mirrors decision.ts', () => {
    expect(iacApplyMeta('failed', 'n/a', undefined, t)).toMatchObject({ tone: 'danger', done: false });
    expect(iacApplyMeta('failed_state_suspect', 'n/a', undefined, t).tone).toBe('danger');
    expect(iacApplyMeta('ambiguous', 'n/a', undefined, t).tone).toBe('warn'); // mirrors decision.ts (not danger)
    const wait = iacApplyMeta('waiting_for_rebake', 'pending', undefined, t);
    expect(wait.label).toBe('awaiting apply');
    expect(wait.tone).toBe(''); // neutral — carries its own label + help
    expect(typeof wait.help).toBe('string');
  });

  // Codex r4: the ledger stopped claiming the re-bake was outstanding, and this
  // surface was left still claiming it. The coordinator never observes the
  // external build — it writes waiting_for_rebake at merge and leaves it until
  // the operator's second submit (agent/main.py:7187) — so "awaiting rebuild"
  // went stale the instant the build finished, with nothing here able to tell.
  // The rail label is ONE token for both merge_state variants, so it has to be
  // the claim true in both: pre-merge the wait is the merge, post-merge it is
  // the operator's apply, and neither is a rebuild this client can vouch for.
  it.each(['pending', 'merged', 'failed', undefined])(
    'the waiting_for_rebake label names the apply, never the rebuild (merge_state %p)',
    (mergeState) => {
      const meta = iacApplyMeta('waiting_for_rebake', mergeState, undefined, t);
      expect(meta.label).toBe('awaiting apply');
      expect(meta.label).not.toMatch(/rebuild|re-?bake/i);
      // The HELP text may still describe the rebuild step — there it explains
      // why a second step exists rather than asserting its current state.
      expect(meta.help).toMatch(/rebuilt/);
    },
  );

  it('tolerates null/undefined apply_status', () => {
    expect(iacApplyMeta(null, null, undefined, t)).toMatchObject({
      label: '',
      tone: '',
      help: null,
      done: false,
    });
    expect(iacApplyMeta(undefined, undefined, undefined, t).done).toBe(false);
  });

  it('waiting_for_rebake + superseded_by_pr → terminal "superseded" (ok tone, done), regardless of merge_state', () => {
    const merged = iacApplyMeta('waiting_for_rebake', 'merged', 221, t);
    expect(merged).toMatchObject({ label: 'superseded', tone: 'ok', done: true });
    expect(typeof merged.help).toBe('string');
    expect(merged.help as string).toContain('#221');

    // The marker wins regardless of merge_state — the OTHER #216 doc carries
    // merge_state:'pending' and must read the same way.
    const pending = iacApplyMeta('waiting_for_rebake', 'pending', 221, t);
    expect(pending).toMatchObject({ label: 'superseded', tone: 'ok', done: true });
  });

  it('superseded_by_pr is gated to waiting_for_rebake — does not mask applied or failed rows', () => {
    const applied = iacApplyMeta('applied', 'merged', 221, t);
    expect(applied.label).toBe('applied & merged');
    expect(applied.done).toBe(true);

    const failed = iacApplyMeta('failed', 'n/a', 221, t);
    expect(failed.tone).toBe('danger');
    expect(failed.label).not.toBe('superseded');
  });

  it('superseded_by_pr rejects non-positive/non-integer values — falls through to "awaiting apply"', () => {
    for (const bad of [0, -1, 1.5]) {
      const m = iacApplyMeta('waiting_for_rebake', 'pending', bad, t);
      expect(m.label).toBe('awaiting apply');
      expect(m.done).toBe(false);
    }
  });

  it('regression: waiting_for_rebake with no third arg still reads "awaiting apply"', () => {
    const m = iacApplyMeta('waiting_for_rebake', 'pending', undefined, t);
    expect(m.label).toBe('awaiting apply');
    expect(m.done).toBe(false);
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
