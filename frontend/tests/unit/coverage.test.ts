import { describe, it, expect } from 'vitest';
import { coveragePercent } from '../../src/lib/coverage';

// Display-percentage contract (docs/plans/2026-06-10-migration-coverage-meter.md
// §Display-percentage rules): the number is a trust number — exact 0/100 only
// when literally true, [1,99] otherwise, null when there is nothing to measure.
describe('coveragePercent', () => {
  it('computes a plain rounded percentage', () => {
    expect(coveragePercent(13, 50)).toBe(26);
    expect(coveragePercent(1, 3)).toBe(33);
    expect(coveragePercent(2, 3)).toBe(67);
  });

  it('returns null when there is nothing to measure', () => {
    expect(coveragePercent(0, 0)).toBeNull();
    expect(coveragePercent(5, 0)).toBeNull();
    expect(coveragePercent(5, -1)).toBeNull();
  });

  it('returns null on non-finite input', () => {
    expect(coveragePercent(Number.NaN, 10)).toBeNull();
    expect(coveragePercent(3, Number.NaN)).toBeNull();
    expect(coveragePercent(3, Number.POSITIVE_INFINITY)).toBeNull();
    expect(coveragePercent(Number.POSITIVE_INFINITY, 10)).toBeNull();
    expect(coveragePercent(Number.NEGATIVE_INFINITY, 10)).toBeNull();
  });

  it('is exact at the endpoints', () => {
    expect(coveragePercent(10, 10)).toBe(100);
    expect(coveragePercent(0, 10)).toBe(0);
  });

  it('never rounds up to 100 or down to 0', () => {
    expect(coveragePercent(199, 200)).toBe(99); // 99.5% — not done ⇒ not 100
    expect(coveragePercent(1, 1000)).toBe(1); // 0.1% — first adoption moves the needle
  });

  it('clamps out-of-range managed counts instead of lying', () => {
    expect(coveragePercent(-3, 10)).toBe(0);
    expect(coveragePercent(15, 10)).toBe(100);
  });
});
