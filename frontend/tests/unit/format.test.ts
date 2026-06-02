import { describe, it, expect } from 'vitest';
import { fmtTokens, shortTrace, fmtPreview, fmtWhen } from '../../src/lib/format';

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
