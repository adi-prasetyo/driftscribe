import { describe, it, expect } from 'vitest';
import { WORKLOADS, type Workload, type WorkloadOption } from '../../src/lib/workloads';

// Re-homes the workload-option contract previously guarded in
// tests/integration/test_ui_transparency.py:59-62. The option VALUES
// (drift/upgrade/explore/provision) are the /chat API contract; the visible
// labels are operator-facing. Pin BOTH value/label and ORDER so a label
// rename or a dropped/reordered option is a reviewed change.

const EXPECTED: ReadonlyArray<readonly [Workload, string]> = [
  ['drift', 'Cloud Run config'],
  ['upgrade', 'Dependencies'],
  ['explore', 'Explore (read-only)'],
  ['provision', 'Provision (infra edits)'],
];

describe('WORKLOADS contract', () => {
  it('has exactly four options', () => {
    expect(WORKLOADS).toHaveLength(4);
  });

  it('matches the exact value/label pairs in order', () => {
    expect(WORKLOADS).toEqual(
      EXPECTED.map(([value, label]) => ({ value, label })),
    );
  });

  it('preserves the value order (drift, upgrade, explore, provision)', () => {
    expect(WORKLOADS.map((o) => o.value)).toEqual([
      'drift',
      'upgrade',
      'explore',
      'provision',
    ]);
  });

  it('preserves the operator-facing labels in order', () => {
    expect(WORKLOADS.map((o) => o.label)).toEqual([
      'Cloud Run config',
      'Dependencies',
      'Explore (read-only)',
      'Provision (infra edits)',
    ]);
  });

  it.each(EXPECTED)('maps %s -> "%s"', (value, label) => {
    const opt = WORKLOADS.find((o) => o.value === value);
    expect(opt).toBeDefined();
    expect(opt?.label).toBe(label);
  });

  it('every option satisfies the WorkloadOption shape', () => {
    for (const opt of WORKLOADS) {
      const shaped: WorkloadOption = opt;
      expect(typeof shaped.value).toBe('string');
      expect(typeof shaped.label).toBe('string');
    }
  });
});
