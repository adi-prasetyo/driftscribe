import { describe, it, expect } from 'vitest';
import { WORKLOADS, type Workload, type WorkloadOption, askAboutPrPrefill, askPrFromSearch, initialChatPrefill } from '../../src/lib/workloads';

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

describe('askPrFromSearch', () => {
  it('parses a positive integer', () => {
    expect(askPrFromSearch('?ask_pr=18')).toBe(18);
    expect(askPrFromSearch('?preview_pr=3&ask_pr=00012')).toBe(12);
  });
  it('rejects junk, zero, negatives, floats, absence', () => {
    for (const s of ['', '?ask_pr=', '?ask_pr=abc', '?ask_pr=0', '?ask_pr=-3',
                     '?ask_pr=1.5', '?other=1']) {
      expect(askPrFromSearch(s)).toBeNull();
    }
  });
});

describe('askAboutPrPrefill', () => {
  it('names the PR and asks for a plain-language explanation', () => {
    const text = askAboutPrPrefill(18);
    expect(text).toContain('PR #18');
    expect(text.toLowerCase()).toContain('plain language');
  });
});

describe('initialChatPrefill', () => {
  it('seeds an explore-workload prefill at epoch 1 from ask_pr', () => {
    const p = initialChatPrefill('?ask_pr=18');
    expect(p).toEqual({ text: askAboutPrPrefill(18), workload: 'explore', epoch: 1 });
  });
  it('is null without a valid ask_pr', () => {
    expect(initialChatPrefill('')).toBeNull();
    expect(initialChatPrefill('?ask_pr=junk')).toBeNull();
    expect(initialChatPrefill('?preview_pr=18')).toBeNull();
  });
});
