import { describe, it, expect } from 'vitest';
import {
  safeApprovalHref,
  iacApprovalHref,
  isExpired,
  isRollbackAwaitingOperator,
  notifyFailed,
  isRollbackApprovalUnresolved,
  isIacAwaitingOperator,
  safeGithubHref,
  iacPrHref,
  resolvedIacPrNumbers,
  supersededWaitingIds,
  iacApproveLabel,
} from '../../src/lib/approval';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// iacApproveLabel now resolves its wording through the shared.* catalog; the
// suite asserts English (byte-for-byte the original inline text), so pin an
// EN-bound translator, same as format.test.ts.
const t: TranslateFn = (k, p) => translate('en', k, p);

// SECURITY-CRITICAL guard. This file re-homes the assertions previously made
// in tests/integration/test_ui_transparency.py:148-166 (the legacy
// _safeApprovalHref guard in agent/templates/transparency.html). The legacy
// guard accepts both relative ("/approvals/<id>?t=") and same-origin absolute
// ("https://<coordinator>/approvals/<id>") forms, returning the RELATIVE href
// on success, and rejects anything off-origin / non-http(s) / non-/approvals.

// Use an explicit origin arg for determinism (jsdom default is http://localhost,
// but we pin it so the test is independent of the environment).
const ORIGIN = 'https://coordinator.example';

describe('safeApprovalHref', () => {
  it('accepts a relative /approvals/ URL and returns it (relative, with query)', () => {
    expect(safeApprovalHref('/approvals/x?t=1', ORIGIN)).toBe('/approvals/x?t=1');
  });

  it('accepts a relative /approvals/ URL with no query', () => {
    expect(safeApprovalHref('/approvals/abc123', ORIGIN)).toBe('/approvals/abc123');
  });

  it('accepts a same-origin ABSOLUTE URL and downgrades it to a relative href', () => {
    expect(
      safeApprovalHref('https://coordinator.example/approvals/x?t=1', ORIGIN),
    ).toBe('/approvals/x?t=1');
  });

  it('returns pathname+search only (drops any fragment / off-origin host)', () => {
    // Never echo an absolute attacker-controlled string back into the DOM.
    const out = safeApprovalHref('https://coordinator.example/approvals/x?t=1#frag', ORIGIN);
    expect(out).not.toContain('coordinator.example');
    expect(out!.startsWith('/approvals/')).toBe(true);
  });

  it('rejects an off-origin absolute URL even when the path is /approvals/', () => {
    expect(safeApprovalHref('https://evil.com/approvals/x', ORIGIN)).toBeNull();
  });

  it('rejects an off-origin URL whose host merely PREFIXES the base origin', () => {
    // open-redirect shape: https://coordinator.example.evil.com/...
    expect(
      safeApprovalHref('https://coordinator.example.evil.com/approvals/x', ORIGIN),
    ).toBeNull();
  });

  it('rejects a javascript: scheme', () => {
    expect(safeApprovalHref('javascript:alert(1)', ORIGIN)).toBeNull();
  });

  it('rejects a data: scheme', () => {
    expect(safeApprovalHref('data:text/html,<script>alert(1)</script>', ORIGIN)).toBeNull();
  });

  it('rejects the demo scrub\'s literal "<redacted>" token placeholder', () => {
    // The anonymous serve scrub masks the one-time token to the literal
    // "<redacted>"; the link can never act (Approve AND Reject verify the
    // real token), so no CTA must render for it.
    expect(safeApprovalHref('/approvals/x?t=<redacted>', ORIGIN)).toBeNull();
    expect(
      safeApprovalHref('https://coordinator.example/approvals/x?t=<redacted>', ORIGIN),
    ).toBeNull();
  });

  it('rejects the URL-encoded form of the "<redacted>" placeholder', () => {
    expect(safeApprovalHref('/approvals/x?t=%3Credacted%3E', ORIGIN)).toBeNull();
  });

  it('rejects a relative path that is not under /approvals/', () => {
    expect(safeApprovalHref('/other/path', ORIGIN)).toBeNull();
  });

  it('rejects a path that only contains "/approvals" without the trailing slash', () => {
    expect(safeApprovalHref('/approvals', ORIGIN)).toBeNull();
    expect(safeApprovalHref('/approvalsX/y', ORIGIN)).toBeNull();
  });

  it('rejects an empty string', () => {
    expect(safeApprovalHref('', ORIGIN)).toBeNull();
  });

  it('rejects garbage / malformed input', () => {
    expect(safeApprovalHref('::::not a url::::', ORIGIN)).toBeNull();
    expect(safeApprovalHref('http://', ORIGIN)).toBeNull();
  });

  it('rejects a same-origin path on a non-http(s) origin (e.g. file:)', () => {
    // protocol must be http/https even if the origin would match.
    expect(safeApprovalHref('/approvals/x', 'file://')).toBeNull();
  });

  it('falls back to window.location.origin when origin arg is omitted', () => {
    // Derive the absolute form from the live jsdom origin (whatever port it
    // runs on) so the fallback test does not hard-code a host:port.
    const self = window.location.origin;
    expect(safeApprovalHref('/approvals/x?t=1')).toBe('/approvals/x?t=1');
    expect(safeApprovalHref(`${self}/approvals/x?t=1`)).toBe('/approvals/x?t=1');
    expect(safeApprovalHref('https://evil.com/approvals/x')).toBeNull();
  });

  it('locale "ja" appends &lang=ja after an existing query', () => {
    expect(safeApprovalHref('/approvals/x?t=1', ORIGIN, 'ja')).toBe('/approvals/x?t=1&lang=ja');
  });

  it('locale "ja" appends ?lang=ja when there is no existing query', () => {
    expect(safeApprovalHref('/approvals/abc123', ORIGIN, 'ja')).toBe('/approvals/abc123?lang=ja');
  });

  it('locale "ja" does not duplicate an already-present lang param', () => {
    expect(safeApprovalHref('/approvals/x?lang=ja', ORIGIN, 'ja')).toBe('/approvals/x?lang=ja');
    expect(safeApprovalHref('/approvals/x?t=1&lang=en', ORIGIN, 'ja')).toBe(
      '/approvals/x?t=1&lang=en',
    );
  });

  it('no locale or locale "en" leaves the href unchanged (no lang param)', () => {
    expect(safeApprovalHref('/approvals/x?t=1', ORIGIN)).toBe('/approvals/x?t=1');
    expect(safeApprovalHref('/approvals/x?t=1', ORIGIN, 'en')).toBe('/approvals/x?t=1');
  });

  it('locale "ja" has no effect on a rejected (off-origin) URL — still null', () => {
    expect(safeApprovalHref('https://evil.com/approvals/x', ORIGIN, 'ja')).toBeNull();
  });
});

describe('isExpired', () => {
  const NOW = Date.parse('2026-06-02T00:00:00Z');

  it('returns true for a timestamp strictly in the past (relative to now)', () => {
    expect(isExpired('2026-06-01T00:00:00Z', NOW)).toBe(true);
  });

  it('returns true for a timestamp exactly equal to now (<= now)', () => {
    expect(isExpired('2026-06-02T00:00:00Z', NOW)).toBe(true);
  });

  it('returns false for a timestamp in the future', () => {
    expect(isExpired('2026-06-03T00:00:00Z', NOW)).toBe(false);
  });

  it('fail-safe: returns false for null (missing expires_at → NOT expired)', () => {
    expect(isExpired(null, NOW)).toBe(false);
  });

  it('fail-safe: returns false for undefined', () => {
    expect(isExpired(undefined, NOW)).toBe(false);
  });

  it('fail-safe: returns false for an empty string', () => {
    expect(isExpired('', NOW)).toBe(false);
  });

  it('fail-safe: returns false for an unparseable string', () => {
    expect(isExpired('not-a-date', NOW)).toBe(false);
  });

  it('defaults now to the current clock when omitted', () => {
    const past = new Date(Date.now() - 60_000).toISOString();
    const future = new Date(Date.now() + 60_000).toISOString();
    expect(isExpired(past)).toBe(true);
    expect(isExpired(future)).toBe(false);
  });
});

describe('isRollbackApprovalUnresolved — the STATUS half, without the clock (ds-d4z)', () => {
  it('true for pending and for an absent status field', () => {
    expect(isRollbackApprovalUnresolved({ status: 'pending' })).toBe(true);
    expect(isRollbackApprovalUnresolved({})).toBe(true);
  });

  it('false for used and denied — a spent credential is not merely timed out', () => {
    expect(isRollbackApprovalUnresolved({ status: 'used' })).toBe(false);
    expect(isRollbackApprovalUnresolved({ status: 'denied' })).toBe(false);
  });

  it('false when the backend could not read the status (ds-mml)', () => {
    expect(isRollbackApprovalUnresolved({ status_unavailable: true })).toBe(false);
    // status_unavailable wins even alongside a stale-looking pending value.
    expect(isRollbackApprovalUnresolved({ status: 'pending', status_unavailable: true })).toBe(false);
  });

  it('false for null/undefined', () => {
    expect(isRollbackApprovalUnresolved(null)).toBe(false);
    expect(isRollbackApprovalUnresolved(undefined)).toBe(false);
  });

  it('ignores expires_at entirely — that is the point of the split', () => {
    const longExpired = { status: 'pending' as const, expires_at: '2000-01-01T00:00:00Z' };
    expect(isRollbackApprovalUnresolved(longExpired)).toBe(true);
  });
});

describe('isRollbackAwaitingOperator — shared "awaiting the operator" predicate, rollback lane', () => {
  const NOW = Date.parse('2026-07-28T12:00:00Z');

  it('true: approval_url present + safe, status pending, not expired', () => {
    const d = {
      approval: {
        approval_url: '/approvals/x?t=1',
        status: 'pending' as const,
        expires_at: '2026-07-29T00:00:00Z',
      },
    };
    expect(isRollbackAwaitingOperator(d, { now: NOW, origin: ORIGIN })).toBe(true);
  });

  it('true: status absent (pre-enrichment rows never had the field at all)', () => {
    const d = { approval: { approval_url: '/approvals/x?t=1' } };
    expect(isRollbackAwaitingOperator(d, { now: NOW, origin: ORIGIN })).toBe(true);
  });

  it('false: no approval_url at all', () => {
    expect(isRollbackAwaitingOperator({ approval: { status: 'pending' as const } }, { now: NOW, origin: ORIGIN })).toBe(
      false,
    );
    expect(isRollbackAwaitingOperator({ approval: null }, { now: NOW, origin: ORIGIN })).toBe(false);
    expect(isRollbackAwaitingOperator({}, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('false: safeApprovalHref rejects the URL (off-origin)', () => {
    const d = { approval: { approval_url: 'https://evil.example/approvals/x', status: 'pending' as const } };
    expect(isRollbackAwaitingOperator(d, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('false: safeApprovalHref rejects the URL (`<redacted>` scrub-placeholder token — dead button)', () => {
    const d = { approval: { approval_url: '/approvals/x?t=<redacted>', status: 'pending' as const } };
    expect(isRollbackAwaitingOperator(d, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('false: status is "used" or "denied"', () => {
    const used = { approval: { approval_url: '/approvals/x?t=1', status: 'used' as const } };
    const denied = { approval: { approval_url: '/approvals/x?t=1', status: 'denied' as const } };
    expect(isRollbackAwaitingOperator(used, { now: NOW, origin: ORIGIN })).toBe(false);
    expect(isRollbackAwaitingOperator(denied, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('false: expired', () => {
    const d = {
      approval: { approval_url: '/approvals/x?t=1', status: 'pending' as const, expires_at: '2026-07-28T00:00:00Z' },
    };
    expect(isRollbackAwaitingOperator(d, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('tolerates null/undefined decision and null/undefined approval (never throws)', () => {
    expect(isRollbackAwaitingOperator(null, { now: NOW, origin: ORIGIN })).toBe(false);
    expect(isRollbackAwaitingOperator(undefined, { now: NOW, origin: ORIGIN })).toBe(false);
    expect(isRollbackAwaitingOperator({ approval: undefined }, { now: NOW, origin: ORIGIN })).toBe(false);
  });

  it('opts defaults to {} (now/origin both omitted) without throwing', () => {
    // now defaults to Date.now() inside isExpired; origin defaults to
    // window.location.origin inside safeApprovalHref.
    const d = { approval: { approval_url: '/approvals/x?t=1', status: 'pending' as const } };
    expect(() => isRollbackAwaitingOperator(d)).not.toThrow();
  });
});

describe('iacApprovalHref', () => {
  it('builds the same-origin relative path for a positive integer PR number', () => {
    expect(iacApprovalHref(68)).toBe('/iac-approvals/68');
    expect(iacApprovalHref(1)).toBe('/iac-approvals/1');
  });

  it('rejects zero and negative PR numbers', () => {
    expect(iacApprovalHref(0)).toBeNull();
    expect(iacApprovalHref(-5)).toBeNull();
  });

  it('rejects non-integers (floats, NaN, Infinity)', () => {
    expect(iacApprovalHref(4.5)).toBeNull();
    expect(iacApprovalHref(Number.NaN)).toBeNull();
    expect(iacApprovalHref(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it('rejects non-number inputs (string/undefined/null/object/boolean)', () => {
    // A numeric string is NOT accepted — the caller must pass a real number,
    // so there is never an attacker-controlled string in the constructed path.
    expect(iacApprovalHref('68')).toBeNull();
    expect(iacApprovalHref('68/../../evil')).toBeNull();
    expect(iacApprovalHref(undefined)).toBeNull();
    expect(iacApprovalHref(null)).toBeNull();
    expect(iacApprovalHref({ pr_number: 68 })).toBeNull();
    // Booleans must be rejected too — IacApprovalCta passes `prNumber` straight
    // through, so a stray `true`/`false` must never yield `/iac-approvals/1`.
    expect(iacApprovalHref(true)).toBeNull();
    expect(iacApprovalHref(false)).toBeNull();
  });

  it('locale "ja" appends ?lang=ja', () => {
    expect(iacApprovalHref(5, 'ja')).toBe('/iac-approvals/5?lang=ja');
  });

  it('no locale or locale "en" leaves the path unchanged', () => {
    expect(iacApprovalHref(5)).toBe('/iac-approvals/5');
    expect(iacApprovalHref(5, 'en')).toBe('/iac-approvals/5');
  });

  it('an invalid prNumber with locale "ja" still returns null', () => {
    expect(iacApprovalHref('abc', 'ja')).toBeNull();
    expect(iacApprovalHref(-1, 'ja')).toBeNull();
  });
});

describe('safeGithubHref — canonical github.com artifact allowlist', () => {
  it('accepts a canonical github.com issue URL (returns absolute, unchanged)', () => {
    const u = 'https://github.com/acme/ops/issues/42';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('accepts a github.com PR URL', () => {
    const u = 'https://github.com/acme/ops/pull/7';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('accepts owner/repo names with dots/dashes', () => {
    const u = 'https://github.com/acme-co/ops.infra/issues/3';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('rejects http (non-TLS)', () => {
    expect(safeGithubHref('http://github.com/acme/ops/issues/42')).toBeNull();
  });
  it('rejects a look-alike / off-allowlist host', () => {
    expect(safeGithubHref('https://github.com.evil.example/acme/ops/issues/42')).toBeNull();
    expect(safeGithubHref('https://raw.githubusercontent.com/x/y/issues/1')).toBeNull();
    expect(safeGithubHref('https://gitlab.com/acme/ops/issues/42')).toBeNull();
  });
  it('rejects userinfo smuggling (user@host, user:pass@host)', () => {
    expect(safeGithubHref('https://evil@github.com/acme/ops/issues/1')).toBeNull();
    expect(safeGithubHref('https://github.com@evil.example/acme/ops/issues/1')).toBeNull();
    expect(safeGithubHref('https://u:p@github.com/acme/ops/issues/1')).toBeNull();
  });
  it('rejects a non-default port', () => {
    expect(safeGithubHref('https://github.com:444/acme/ops/issues/1')).toBeNull();
  });
  it('rejects whitespace / control chars / backslashes in the raw string', () => {
    expect(safeGithubHref('https://github.com/acme/ops/issues/1\t')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops/iss\nues/1')).toBeNull();
    expect(safeGithubHref('https://github.com\\acme/ops/issues/1')).toBeNull();
  });
  it('rejects a non-whitespace C0 control char in the raw string', () => {
    expect(safeGithubHref('https://github.com/acme/ops/issues/1\u0001')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops/issues/1\u0007')).toBeNull();
  });
  it('rejects a non-artifact github.com path (settings, bare repo, root)', () => {
    expect(safeGithubHref('https://github.com/settings/profile')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops')).toBeNull();
    expect(safeGithubHref('https://github.com/')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops/issues/notanumber')).toBeNull();
  });
  it('rejects javascript: / data: smuggling', () => {
    expect(safeGithubHref('javascript:alert(1)')).toBeNull();
    expect(safeGithubHref('data:text/html,<script>1</script>')).toBeNull();
  });
  it('rejects null / non-string / empty / unparseable', () => {
    expect(safeGithubHref(null)).toBeNull();
    expect(safeGithubHref(undefined)).toBeNull();
    expect(safeGithubHref(123 as unknown)).toBeNull();
    expect(safeGithubHref('')).toBeNull();
    expect(safeGithubHref('not a url')).toBeNull();
  });
});

describe('iacPrHref — the rail title link for an iac_apply row', () => {
  it('returns the safe github href for an iac_apply decision', () => {
    const d = { action: 'iac_apply', github: { url: 'https://github.com/adi-prasetyo/driftscribe/pull/68' } };
    expect(iacPrHref(d)).toBe('https://github.com/adi-prasetyo/driftscribe/pull/68');
  });

  it('is null for a non-iac_apply action even if it carries a github.url', () => {
    // Gate on the allowlisted action: never read github.url off an unrelated row.
    const d = { action: 'drift_issue', github: { url: 'https://github.com/acme/ops/pull/9' } };
    expect(iacPrHref(d)).toBeNull();
  });

  it('is null when the github.url fails the host allowlist (off-origin / smuggling)', () => {
    expect(iacPrHref({ action: 'iac_apply', github: { url: 'https://evil.example/x/y/pull/1' } })).toBeNull();
    expect(iacPrHref({ action: 'iac_apply', github: { url: 'javascript:alert(1)' } })).toBeNull();
  });

  it('is null when there is no github field', () => {
    expect(iacPrHref({ action: 'iac_apply' })).toBeNull();
    expect(iacPrHref({ action: 'iac_apply', github: null })).toBeNull();
  });
});

// ds-dzd. The question resolvedIacPrNumbers CANNOT answer, because approval state
// is not PR-wide: agent/main.py's _iac_event_key keys an apply on
// {repo, pr_number, head_sha, generation_metadata}, and
// docs/runbooks/iac-apply-failure-recovery.md tells the operator to rebuild the
// C2 plan on the SAME PR after a failure and approve the newest generation. So
// one PR can hold a dead generation and a live one at once.
describe('supersededWaitingIds — per-GENERATION supersession', () => {
  const row = (o: Record<string, unknown>) => ({ action: 'iac_apply', ...o });
  const A = 'iac-apply-95-312b3cac8ba677d62a138f8050de2a34';
  const B = 'iac-apply-95-ffffffffffffffffffffffffffffffff';

  // The prod shape: PR #95's failure and BOTH of its waiting rows share ONE
  // event_key, so the failure really is those rows' own outcome. This is the
  // phantom "2" the operator saw.
  it('retires waiting rows whose OWN generation reached a terminal state (PR #95)', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'failed_state_suspect', event_key: A, decision_id: 'term', created_at: '2026-06-11T05:50:36Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'w1', created_at: '2026-06-11T05:39:09Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'w2', created_at: '2026-06-11T05:39:03Z' }),
    ]);
    expect(ids.has('w1')).toBe(true);
    expect(ids.has('w2')).toBe(true);
    expect(ids.size).toBe(2);
  });

  // THE INTERLEAVING. Generations are NOT serialized against each other — the
  // claim is per event_key — so two can be in flight and finish out of order.
  // terminal(A) is newer than waiting(B) but says nothing about generation B. A
  // per-PR "newest terminal" rule retires the live B row; matching on event_key
  // cannot, because a terminal only ever speaks for its own artifact.
  it('a terminal in generation A never retires a live waiting row in generation B', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'wA', created_at: '2026-07-30T10:00:00Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: B, decision_id: 'wB', created_at: '2026-07-30T11:00:00Z' }),
      row({ apply_status: 'failed_state_suspect', event_key: A, decision_id: 'tA', created_at: '2026-07-30T12:00:00Z' }),
    ]);
    expect(ids.has('wA')).toBe(true); // its own generation finished
    expect(ids.has('wB')).toBe(false); // nothing has run generation B
    expect(ids.size).toBe(1);
  });

  // The APPLIED variant of the interleaving, which is the one that survived two
  // rounds of this fix. Generation A applies, its merge fails, the PR stays open,
  // the head advances, generation B is built and records its own waiting row.
  // Reading a PR-wide "PR 95 has an applied row" here would delete B from the
  // desk, the count, the ledger and the rail at once.
  it('an APPLIED terminal in generation A never retires a live waiting row in B', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'applied', event_key: A, decision_id: 'appliedA', created_at: '2026-07-30T10:00:00Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: B, decision_id: 'wB', created_at: '2026-07-30T11:00:00Z' }),
    ]);
    expect(ids.has('wB')).toBe(false);
    expect(ids.size).toBe(0);
  });

  // Prod carries this today: PR #32 has one `applied` event_key and a separate
  // `failed` one. The failed generation must not speak for the applied one.
  it('separate event_keys on the SAME pr_number stay independent', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'failed', event_key: 'iac-apply-32-90e163e5', decision_id: 'tFail', created_at: '2026-05-30T10:52:27Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: 'iac-apply-32-a1473c8a', decision_id: 'wOther', created_at: '2026-05-30T11:00:00Z' }),
    ]);
    expect(ids.size).toBe(0);
  });

  // The runbook rebuild, expressed the way it actually happens: recovery builds a
  // NEW artifact, so the new waiting row carries a NEW event_key and the old
  // failure cannot touch it.
  it('leaves a rebuilt generation actionable after an older failure (runbook)', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'waiting_for_rebake', event_key: B, decision_id: 'rebuilt', created_at: '2026-07-30T12:00:00Z' }),
      row({ apply_status: 'failed_state_suspect', event_key: A, decision_id: 'dead', created_at: '2026-07-30T10:00:00Z' }),
    ]);
    expect(ids.has('rebuilt')).toBe(false);
    expect(ids.size).toBe(0);
  });

  it('an applied row also retires its own generation’s waiting row', () => {
    const ids = supersededWaitingIds([
      row({ apply_status: 'applied', event_key: A, decision_id: 'ok', created_at: '2026-07-08T08:58:00Z' }),
      row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'old', created_at: '2026-07-08T08:49:00Z' }),
    ]);
    expect(ids.has('old')).toBe(true);
  });

  // Fails SAFE toward showing work in every degenerate case.
  it('does not retire a row with a missing or empty event_key', () => {
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: A, decision_id: 't', created_at: '2026-07-30T12:00:00Z' }),
        row({ apply_status: 'waiting_for_rebake', decision_id: 'noKey', created_at: '2026-07-30T10:00:00Z' }),
      ]).size,
    ).toBe(0);
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: '', decision_id: 't', created_at: '2026-07-30T12:00:00Z' }),
        row({ apply_status: 'waiting_for_rebake', event_key: '', decision_id: 'w', created_at: '2026-07-30T10:00:00Z' }),
      ]).size,
    ).toBe(0);
  });

  it('does not retire a row when either timestamp is missing or unparseable', () => {
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: A, decision_id: 't', created_at: '2026-07-30T12:00:00Z' }),
        row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'no-ts' }),
      ]).size,
    ).toBe(0);
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: A, decision_id: 't', created_at: 'not-a-date' }),
        row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'w', created_at: '2026-07-30T10:00:00Z' }),
      ]).size,
    ).toBe(0);
  });

  it('equal timestamps do not retire the row (strictly newer only)', () => {
    const at = '2026-07-30T12:00:00Z';
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: A, decision_id: 't', created_at: at }),
        row({ apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'w', created_at: at }),
      ]).size,
    ).toBe(0);
  });

  it('ignores non-iac_apply rows even when the event_key matches', () => {
    expect(
      supersededWaitingIds([
        row({ apply_status: 'failed', event_key: A, decision_id: 't', created_at: '2026-07-30T12:00:00Z' }),
        { action: 'rollback', apply_status: 'waiting_for_rebake', event_key: A, decision_id: 'rb', created_at: '2026-07-30T10:00:00Z' },
      ]).size,
    ).toBe(0);
  });

  it('tolerates a null/undefined list and null entries', () => {
    expect(supersededWaitingIds(null).size).toBe(0);
    expect(supersededWaitingIds(undefined).size).toBe(0);
    expect(supersededWaitingIds([null, undefined]).size).toBe(0);
  });
});

// The predicate and the rail label must BOTH honour generation supersession, or
// the band and the rail disagree again — the drift this whole change closes.
describe('isIacAwaitingOperator / iacApproveLabel honour supersededIds', () => {
  // ds-dzd: neither predicate takes the PR-wide resolved set any more. A live
  // waiting row on generation B must stay actionable even when its PR has an
  // `applied` row from generation A — which is exactly what a PR-wide term did.
  it('a waiting row is judged by its OWN generation, never by its PR', () => {
    const live = {
      action: 'iac_apply',
      apply_status: 'waiting_for_rebake',
      pr_number: 95,
      decision_id: 'wB',
      event_key: 'iac-apply-95-ffffffffffffffffffffffffffffffff',
    };
    // Nothing has superseded generation B, so it is awaiting the operator even
    // though PR 95 also carries a finished generation A.
    expect(isIacAwaitingOperator(live, new Set())).toBe(true);
    expect(iacApproveLabel(live, new Set(), t)).toBe('Review & approve →');
    // Only its own decision_id landing in the set retires it.
    expect(isIacAwaitingOperator(live, new Set(['wB']))).toBe(false);
    expect(iacApproveLabel(live, new Set(['wB']), t)).toBe('Go to approval page →');
  });

  const stale = { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 95, decision_id: 'w1' };

  it('a superseded waiting row is NOT awaiting the operator', () => {
    expect(isIacAwaitingOperator(stale, new Set(['w1']))).toBe(false);
    expect(isIacAwaitingOperator(stale, new Set())).toBe(true);
  });

  it('a superseded waiting row stops advertising "Review & approve →"', () => {
    expect(iacApproveLabel(stale, new Set(['w1']), t)).toBe('Go to approval page →');
    expect(iacApproveLabel(stale, new Set(), t)).toBe('Review & approve →');
  });
});

describe('resolvedIacPrNumbers — PRs provably applied AND merged', () => {
  it('collects the pr_number of every applied+merged iac_apply row', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'applied', merge_state: 'merged', pr_number: 68 },
      { action: 'iac_apply', apply_status: 'applied', merge_state: 'merged', pr_number: 71 },
    ]);
    expect(set.has(68)).toBe(true);
    expect(set.has(71)).toBe(true);
    expect(set.size).toBe(2);
  });

  // ds-dzd. `applied` alone is not proof a PR is finished with: an apply can
  // succeed while its MERGE fails, leaving the PR OPEN with real work on it. A
  // later generation of that PR shows up in the open-PR listing BEFORE it has any
  // decision row — it only gets one on the operator's first approval click — so
  // suppressing on `applied` alone made live work vanish from the desk, the count,
  // the estate section and the tour at once, with nothing anywhere for the
  // decision-row fallback to find.
  it('does NOT resolve a PR whose apply succeeded but whose MERGE failed', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'applied', merge_state: 'failed', pr_number: 68 },
    ]);
    expect(set.size).toBe(0);
  });

  it('does NOT resolve a PR with no merge_state at all — fails open', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'applied', pr_number: 68 },
      { action: 'iac_apply', apply_status: 'applied', merge_state: 'pending', pr_number: 71 },
    ]);
    expect(set.size).toBe(0);
  });

  it('ignores an applied row whose action is NOT iac_apply', () => {
    // A rollback/other decision that happens to carry apply_status + pr_number
    // must never mark an iac PR resolved.
    const set = resolvedIacPrNumbers([
      { action: 'rollback', apply_status: 'applied', pr_number: 99 },
      { action: 'drift_issue', apply_status: 'applied', pr_number: 12 },
    ]);
    expect(set.size).toBe(0);
  });

  // ds-dzd — a terminal FAILURE must NOT resolve a PR here. Three callers read
  // this set as "this PR is provably closed", and estate.ts's reconcileApprovals
  // states the invariant outright ("an in-progress or failed apply leaves the
  // entry standing") because it DROPS a pending listing entry. Widening it to
  // failures was tried and reverted — it would have hidden the recovery work the
  // runbook prescribes. (Nor is a SUCCESSFUL apply sufficient on its own: see the
  // merge_state cases above.) The generation-scoped
  // question lives in supersededWaitingIds instead.
  it('does NOT resolve a PR whose apply ended in terminal failure', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'failed', pr_number: 70 },
      { action: 'iac_apply', apply_status: 'failed_state_suspect', pr_number: 95 },
      { action: 'iac_apply', apply_status: 'ambiguous', pr_number: 72 },
      { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 68 },
    ]);
    expect(set.size).toBe(0);
  });

  it('ignores applied iac rows with a missing / zero / negative / non-integer pr_number', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'applied' },
      { action: 'iac_apply', apply_status: 'applied', pr_number: 0 },
      { action: 'iac_apply', apply_status: 'applied', pr_number: -5 },
      { action: 'iac_apply', apply_status: 'applied', pr_number: 4.5 },
    ]);
    expect(set.size).toBe(0);
  });

  it('returns an empty set for an empty list', () => {
    expect(resolvedIacPrNumbers([]).size).toBe(0);
  });

  it('tolerates a null/undefined list (returns an empty set)', () => {
    expect(resolvedIacPrNumbers(null).size).toBe(0);
    expect(resolvedIacPrNumbers(undefined).size).toBe(0);
  });

  it('tolerates null/undefined entries in the list', () => {
    const set = resolvedIacPrNumbers([
      null,
      undefined,
      { action: 'iac_apply', apply_status: 'applied', merge_state: 'merged', pr_number: 68 },
    ]);
    expect(set.has(68)).toBe(true);
    expect(set.size).toBe(1);
  });
});

describe('isIacAwaitingOperator — shared "awaiting the operator" predicate, iac lane', () => {
  it('true: iac_apply + waiting_for_rebake + not superseded + not resolved', () => {
    const d = { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 68 };
    expect(isIacAwaitingOperator(d, new Set())).toBe(true);
  });

  it('false: action is not iac_apply', () => {
    const d = { action: 'rollback', apply_status: 'waiting_for_rebake', pr_number: 68 };
    expect(isIacAwaitingOperator(d, new Set())).toBe(false);
  });

  it('false: apply_status is not waiting_for_rebake', () => {
    expect(isIacAwaitingOperator({ action: 'iac_apply', apply_status: 'applied', pr_number: 68 }, new Set())).toBe(
      false,
    );
    expect(isIacAwaitingOperator({ action: 'iac_apply', apply_status: 'failed', pr_number: 68 }, new Set())).toBe(
      false,
    );
  });

  it('false: explicit positive-integer superseded_by_pr', () => {
    const d = { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 216, superseded_by_pr: 221 };
    expect(isIacAwaitingOperator(d, new Set())).toBe(false);
  });

  it('a 0 / negative / non-integer superseded_by_pr is NOT treated as superseded (mirrors iacApproveLabel)', () => {
    const base = { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 216 };
    expect(isIacAwaitingOperator({ ...base, superseded_by_pr: 0 }, new Set())).toBe(true);
    expect(isIacAwaitingOperator({ ...base, superseded_by_pr: -1 }, new Set())).toBe(true);
    expect(isIacAwaitingOperator({ ...base, superseded_by_pr: 1.5 }, new Set())).toBe(true);
  });

  // ds-dzd: what retires a waiting row is a terminal row for its OWN GENERATION,
  // identified by event_key — not any terminal row sharing its PR number. The old
  // pin here asserted the PR-wide rule, which deleted live work (see
  // supersededWaitingIds).
  it('false: this row’s own generation was superseded (event_key match)', () => {
    const d = {
      action: 'iac_apply',
      apply_status: 'waiting_for_rebake',
      pr_number: 68,
      decision_id: 'w1',
      event_key: 'iac-apply-68-aaaa',
    };
    expect(isIacAwaitingOperator(d, new Set(['w1']))).toBe(false);
  });

  it('true: a DIFFERENT generation of the same PR finished — this row still stands', () => {
    const d = {
      action: 'iac_apply',
      apply_status: 'waiting_for_rebake',
      pr_number: 68,
      decision_id: 'w2',
      event_key: 'iac-apply-68-bbbb',
    };
    // 'w1' (generation aaaa) is superseded; this row is generation bbbb.
    expect(isIacAwaitingOperator(d, new Set(['w1']))).toBe(true);
  });

  it('a PR NOT in resolvedPrs still keeps its live status (only the matching PR downgrades)', () => {
    const resolved = new Set([68]);
    expect(
      isIacAwaitingOperator({ action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 71 }, new Set()),
    ).toBe(true);
  });

  it('a missing/invalid pr_number can never be "resolved" — stays awaiting even against a non-empty resolvedPrs', () => {
    const resolved = new Set([68]);
    expect(isIacAwaitingOperator({ action: 'iac_apply', apply_status: 'waiting_for_rebake' }, new Set())).toBe(true);
    expect(
      isIacAwaitingOperator(
        { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 0 },
        new Set(),
      ),
    ).toBe(true);
  });

  it('tolerates null/undefined decision (never throws)', () => {
    expect(isIacAwaitingOperator(null, new Set())).toBe(false);
    expect(isIacAwaitingOperator(undefined, new Set())).toBe(false);
  });
});

describe('iacApproveLabel — retire the stale CTA on superseded rows', () => {
  it('waiting_for_rebake + PR NOT resolved → "Review & approve →"', () => {
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 68 }, new Set(), t),
    ).toBe('Review & approve →');
  });

  it('waiting_for_rebake whose own generation was superseded → "Go to approval page →"', () => {
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 68, decision_id: 'w1' },
        new Set(['w1']),
        t,
      ),
    ).toBe('Go to approval page →');
  });

  it('applied + merged (done) → "View approval history →"', () => {
    expect(
      iacApproveLabel(
        { apply_status: 'applied', merge_state: 'merged', pr_number: 68 },
        new Set(),
        t,
      ),
    ).toBe('View approval history →');
  });

  it('applied + merge pending / undefined apply_status → "Go to approval page →"', () => {
    // applied but merge not confirmed → still actionable (merge-only reconcile) → neutral wording.
    expect(
      iacApproveLabel(
        { apply_status: 'applied', merge_state: 'failed', pr_number: 68 },
        new Set(),
        t,
      ),
    ).toBe('Go to approval page →');
    // applied with no merge_state → not provably done → neutral wording.
    expect(iacApproveLabel({ apply_status: 'applied', pr_number: 68 }, new Set(), t)).toBe(
      'Go to approval page →',
    );
    expect(iacApproveLabel({ pr_number: 68 }, new Set(), t)).toBe('Go to approval page →');
  });

  it('terminal-failed apply_status → "View failure details →" (no approval action on the page)', () => {
    // The approval page renders these as a terminal no-action banner (agent/main.py
    // suppresses the form for failed/failed_state_suspect/ambiguous), so the rail must
    // not promise approval work the page can't offer. Merge state is irrelevant — a
    // terminal failure is terminal whether or not the PR later merged (PR #95: a
    // failed_state_suspect + merged row that read "Go to approval page →" but had no button).
    expect(iacApproveLabel({ apply_status: 'failed', pr_number: 70 }, new Set(), t)).toBe(
      'View failure details →',
    );
    expect(
      iacApproveLabel(
        { apply_status: 'failed_state_suspect', merge_state: 'merged', pr_number: 95 },
        new Set(),
        t,
      ),
    ).toBe('View failure details →');
    expect(iacApproveLabel({ apply_status: 'ambiguous', pr_number: 70 }, new Set(), t)).toBe(
      'View failure details →',
    );
  });

  it('waiting_for_rebake with an invalid/missing pr_number against a non-empty set → still "Review & approve →"', () => {
    // A row that can't be matched to a PR can't be superseded → keep the live CTA.
    expect(iacApproveLabel({ apply_status: 'waiting_for_rebake' }, new Set(), t)).toBe(
      'Review & approve →',
    );
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 0 }, new Set(), t),
    ).toBe('Review & approve →');
  });

  // Two generations of the SAME PR: one finished, one still awaiting. The whole
  // point of event_key scoping is that these two rows get different labels.
  it('generation A superseded, generation B waiting → only A downgrades', () => {
    const superseded = new Set(['wA']);
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 68, decision_id: 'wA' },
        superseded,
        t,
      ),
    ).toBe('Go to approval page →');
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 68, decision_id: 'wB' },
        superseded,
        t,
      ),
    ).toBe('Review & approve →');
  });

  it('waiting_for_rebake + superseded_by_pr → "superseded by #N →" (wins even when the PR is NOT in resolvedPrs)', () => {
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 216, superseded_by_pr: 221 },
        new Set(),
        t,
      ),
    ).toBe('superseded by #221 →');
  });

  it('superseded_by_pr is gated to waiting_for_rebake — does not mask a failed row', () => {
    expect(
      iacApproveLabel(
        { apply_status: 'failed', superseded_by_pr: 221, pr_number: 216 },
        new Set(),
        t,
      ),
    ).toBe('View failure details →');
  });

  it('superseded_by_pr ignores a 0 / negative / non-integer value — falls through to the normal ladder', () => {
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 216, superseded_by_pr: 0 },
        new Set(),
        t,
      ),
    ).toBe('Review & approve →');
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 216, superseded_by_pr: -1 },
        new Set(),
        t,
      ),
    ).toBe('Review & approve →');
    expect(
      iacApproveLabel(
        { apply_status: 'waiting_for_rebake', pr_number: 216, superseded_by_pr: 1.5 },
        new Set(),
        t,
      ),
    ).toBe('Review & approve →');
  });
});

// ds-mml: "the server could not read this" vs "this row predates the field".
// Collapsing them re-offered a live Approve button on a burned approval after a
// transient Firestore blip — the click dead-ends at the worker's refusal, which
// reads as the product being broken rather than as a read having failed.
describe('isRollbackAwaitingOperator — unreadable approval status', () => {
  const base = {
    approval_url: 'https://desk.example.com/approvals/rb-1?t=abc',
    expires_at: '2099-01-01T00:00:00Z',
  };
  const opts = { origin: 'https://desk.example.com' };

  it('still treats an ABSENT status as awaiting (pre-enrichment compat)', () => {
    expect(isRollbackAwaitingOperator({ approval: { ...base } }, opts)).toBe(true);
  });

  it('refuses when the backend flags the status as unreadable', () => {
    expect(
      isRollbackAwaitingOperator({ approval: { ...base, status_unavailable: true } }, opts),
    ).toBe(false);
  });
});

describe('notifyFailed — ds-hdt', () => {
  it('is true ONLY for a positively-recorded failure', () => {
    expect(notifyFailed({ notify: { state: 'failed' } })).toBe(true);
    expect(notifyFailed({ notify: { state: 'failed', error_code: 'worker_error', status_code: 503 } })).toBe(
      true,
    );
  });

  it('is false for delivered', () => {
    expect(notifyFailed({ notify: { state: 'delivered' } })).toBe(false);
  });

  it('is false for "pending" — in flight, or the outcome patch was lost. NOT KNOWN', () => {
    // The distinction that matters: `state !== 'delivered'` would light the
    // warning here, telling the operator no notification was sent when one may
    // well have been.
    expect(notifyFailed({ notify: { state: 'pending' } })).toBe(false);
  });

  it('is false when notify is absent — every row written before ds-hdt', () => {
    // Historical rows carry no `notify` key at all. Warning on them would cry
    // wolf across the entire decisions log.
    expect(notifyFailed({})).toBe(false);
    expect(notifyFailed({ notify: null })).toBe(false);
    expect(notifyFailed({ notify: {} })).toBe(false);
  });

  it('is false for null/undefined decisions', () => {
    expect(notifyFailed(null)).toBe(false);
    expect(notifyFailed(undefined)).toBe(false);
  });

  it('is false for an unrecognised state rather than defaulting to alarm', () => {
    expect(notifyFailed({ notify: { state: 'something_new' as never } })).toBe(false);
  });
});
