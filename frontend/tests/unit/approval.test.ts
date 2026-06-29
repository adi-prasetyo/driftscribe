import { describe, it, expect } from 'vitest';
import {
  safeApprovalHref,
  iacApprovalHref,
  isExpired,
  safeGithubHref,
  iacPrHref,
  resolvedIacPrNumbers,
  iacApproveLabel,
} from '../../src/lib/approval';

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

describe('resolvedIacPrNumbers — PRs with a terminal applied iac_apply row', () => {
  it('collects the pr_number of every applied iac_apply row', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'applied', pr_number: 68 },
      { action: 'iac_apply', apply_status: 'applied', pr_number: 71 },
    ]);
    expect(set.has(68)).toBe(true);
    expect(set.has(71)).toBe(true);
    expect(set.size).toBe(2);
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

  it('ignores non-applied iac_apply rows (waiting_for_rebake / failed / ambiguous)', () => {
    const set = resolvedIacPrNumbers([
      { action: 'iac_apply', apply_status: 'waiting_for_rebake', pr_number: 68 },
      { action: 'iac_apply', apply_status: 'failed', pr_number: 70 },
      { action: 'iac_apply', apply_status: 'ambiguous', pr_number: 72 },
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
      { action: 'iac_apply', apply_status: 'applied', pr_number: 68 },
    ]);
    expect(set.has(68)).toBe(true);
    expect(set.size).toBe(1);
  });
});

describe('iacApproveLabel — retire the stale CTA on superseded rows', () => {
  it('waiting_for_rebake + PR NOT resolved → "Review & approve →"', () => {
    expect(iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 68 }, new Set())).toBe(
      'Review & approve →',
    );
  });

  it('waiting_for_rebake + PR resolved → "Go to approval page →" (superseded)', () => {
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 68 }, new Set([68])),
    ).toBe('Go to approval page →');
  });

  it('applied + merged (done) → "View approval history →"', () => {
    expect(
      iacApproveLabel({ apply_status: 'applied', merge_state: 'merged', pr_number: 68 }, new Set()),
    ).toBe('View approval history →');
  });

  it('applied + merge pending / undefined apply_status → "Go to approval page →"', () => {
    // applied but merge not confirmed → still actionable (merge-only reconcile) → neutral wording.
    expect(
      iacApproveLabel({ apply_status: 'applied', merge_state: 'failed', pr_number: 68 }, new Set()),
    ).toBe('Go to approval page →');
    // applied with no merge_state → not provably done → neutral wording.
    expect(iacApproveLabel({ apply_status: 'applied', pr_number: 68 }, new Set())).toBe(
      'Go to approval page →',
    );
    expect(iacApproveLabel({ pr_number: 68 }, new Set())).toBe('Go to approval page →');
  });

  it('terminal-failed apply_status → "View failure details →" (no approval action on the page)', () => {
    // The approval page renders these as a terminal no-action banner (agent/main.py
    // suppresses the form for failed/failed_state_suspect/ambiguous), so the rail must
    // not promise approval work the page can't offer. Merge state is irrelevant — a
    // terminal failure is terminal whether or not the PR later merged (PR #95: a
    // failed_state_suspect + merged row that read "Go to approval page →" but had no button).
    expect(iacApproveLabel({ apply_status: 'failed', pr_number: 70 }, new Set())).toBe(
      'View failure details →',
    );
    expect(
      iacApproveLabel(
        { apply_status: 'failed_state_suspect', merge_state: 'merged', pr_number: 95 },
        new Set(),
      ),
    ).toBe('View failure details →');
    expect(iacApproveLabel({ apply_status: 'ambiguous', pr_number: 70 }, new Set())).toBe(
      'View failure details →',
    );
  });

  it('waiting_for_rebake with an invalid/missing pr_number against a non-empty set → still "Review & approve →"', () => {
    // A row that can't be matched to a PR can't be superseded → keep the live CTA.
    expect(iacApproveLabel({ apply_status: 'waiting_for_rebake' }, new Set([68]))).toBe(
      'Review & approve →',
    );
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 0 }, new Set([68])),
    ).toBe('Review & approve →');
  });

  it('PR A resolved, PR B waiting → only A downgrades, B keeps the live CTA', () => {
    const resolved = new Set([68]); // PR A (68) is resolved; PR B (71) is not
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 68 }, resolved),
    ).toBe('Go to approval page →');
    expect(
      iacApproveLabel({ apply_status: 'waiting_for_rebake', pr_number: 71 }, resolved),
    ).toBe('Review & approve →');
  });
});
