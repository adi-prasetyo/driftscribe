import { describe, it, expect } from 'vitest';
import { reasoningTraceFromSearch } from '../../src/lib/deeplink';

const HEX32 = 'eba334f9211d46cabc79e50ed200a5a1'; // 32 lowercase hex

describe('reasoningTraceFromSearch', () => {
  it('returns a well-formed 32-char lowercase-hex trace id', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32}`)).toBe(HEX32);
  });

  it('ignores other params and reads only reasoning', () => {
    expect(reasoningTraceFromSearch(`?preview_pr=12&reasoning=${HEX32}&x=1`)).toBe(HEX32);
  });

  it('is null when the param is absent', () => {
    expect(reasoningTraceFromSearch('?preview_pr=12')).toBeNull();
    expect(reasoningTraceFromSearch('')).toBeNull();
  });

  it('is null on the wrong length (backend 400s these)', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32.slice(0, 31)}`)).toBeNull();
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32}a`)).toBeNull();
  });

  it('is null on uppercase hex (canonical id is lowercase only)', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32.toUpperCase()}`)).toBeNull();
  });

  it('is null on non-hex / path-y / empty junk', () => {
    expect(reasoningTraceFromSearch('?reasoning=not-a-trace-id')).toBeNull();
    expect(reasoningTraceFromSearch('?reasoning=../../etc/passwd')).toBeNull();
    expect(reasoningTraceFromSearch('?reasoning=')).toBeNull();
  });
});
