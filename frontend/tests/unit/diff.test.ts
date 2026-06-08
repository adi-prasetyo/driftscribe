import { describe, it, expect } from 'vitest';
import { displayDiffValue, diffRows } from '../../src/lib/diff';
import type { Decision } from '../../src/lib/types';

const REDACTED = '(value redacted: secret-like)';

describe('displayDiffValue — mirrors agent/renderer.py:_format_value_cell', () => {
  it('shows a plain value for a non-secret var', () =>
    expect(displayDiffValue('LOG_LEVEL', 'debug')).toBe('debug'));
  it('redacts when the name is secret-like (value present)', () =>
    expect(displayDiffValue('API_TOKEN', 'sk-live-abc')).toBe(REDACTED));
  it('redacts when the value is a credentialed URL (non-secret name)', () =>
    expect(displayDiffValue('ENDPOINT', 'https://a:b@h/x')).toBe(REDACTED));
  it('renders em-dash for a null value (not redacted)', () =>
    expect(displayDiffValue('LOG_LEVEL', null)).toBe('—'));
  it('renders em-dash for an undefined value', () =>
    expect(displayDiffValue('LOG_LEVEL', undefined)).toBe('—'));
  it('renders em-dash for a secret-named null value', () =>
    expect(displayDiffValue('API_TOKEN', null)).toBe('—'));
  it('preserves empty string (an explicitly-unset var is real drift)', () =>
    expect(displayDiffValue('LOG_LEVEL', '')).toBe(''));
  it('redacts BEFORE clamping — a credentialed URL whose :pass@ is past 256 chars still redacts', () => {
    const pad = 'a'.repeat(300);
    const url = `https://user:secretpw@host.example/${pad}`; // userinfo within 256, but value > 256
    expect(displayDiffValue('ENDPOINT', url)).toBe(REDACTED);
  });
  it('clamps a long NON-secret value to 256 chars + ellipsis', () => {
    const long = 'x'.repeat(300);
    const out = displayDiffValue('LOG_LEVEL', long);
    expect(out.endsWith('…')).toBe(true);
    expect(out.length).toBe(257);
  });
});

describe('diffRows — safe rows from a decision', () => {
  it('returns [] for a decision with no diffs', () =>
    expect(diffRows({ decision_id: 'd', action: 'drift_issue' } as Decision)).toEqual([]));

  it('returns [] for null', () => expect(diffRows(null)).toEqual([]));

  it('maps each diff to a display row, redacting secrets', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [
        { name: 'LOG_LEVEL', expected: 'info', live: 'debug', contract_status: 'present_allow_manual' },
        { name: 'API_TOKEN', expected: 'sk-old', live: 'sk-new', contract_status: 'present_disallow_manual' },
      ],
    } as unknown as Decision;
    expect(diffRows(d)).toEqual([
      { name: 'LOG_LEVEL', expected: 'info', live: 'debug', status: 'present_allow_manual', badge: 'ok' },
      { name: 'API_TOKEN', expected: REDACTED, live: REDACTED, status: 'present_disallow_manual', badge: 'danger' },
    ]);
  });

  it('skips a malformed diff (no string name) but keeps the rest', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [{ expected: 'x' }, null, 'nope', { name: 'OK', live: 'v', contract_status: 'absent' }],
    } as unknown as Decision;
    const rows = diffRows(d);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({ name: 'OK', expected: '—', live: 'v', status: 'absent', badge: 'warn' });
  });

  it('gives an ok badge for a match (no-drift) contract_status', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [{ name: 'X', expected: 'a', live: 'a', contract_status: 'match' }],
    } as unknown as Decision;
    expect(diffRows(d)[0].badge).toBe('ok');
  });

  it('falls back to muted badge for an unknown contract_status', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [{ name: 'X', expected: 'a', live: 'b', contract_status: 'bogus' }],
    } as unknown as Decision;
    expect(diffRows(d)[0].badge).toBe('muted');
  });
});
