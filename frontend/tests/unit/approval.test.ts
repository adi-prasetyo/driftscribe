import { describe, it, expect } from 'vitest';
import { safeApprovalHref, isExpired } from '../../src/lib/approval';

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
