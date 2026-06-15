import { describe, it, expect } from 'vitest';
import { WORKLOADS, type Workload, type WorkloadOption, type WorkloadGroup, askAboutPrPrefill, askPrFromSearch, initialChatPrefill } from '../../src/lib/workloads';

// The crew picker contract, derived from frontend/src/lib/workloads.catalog.json
// (the single source the backend cross-surface test also reads). The option
// VALUES (drift/upgrade/explore/provision) are the /chat API contract and are
// FROZEN; the name/descriptor/group are operator-facing. Pin the full records
// + ORDER so a rename, a regrouping, or a dropped/reordered option is a
// reviewed change. The autonomy GROUP is the honest 1/3 split: only Anchor
// (`drift`) has a live trigger.

const EXPECTED: ReadonlyArray<{
  value: Workload;
  name: string;
  descriptor: string;
  group: WorkloadGroup;
  label: string;
}> = [
  { value: 'drift', name: 'Anchor', descriptor: 'Cloud Run config', group: 'autonomous', label: 'Anchor — Cloud Run config' },
  { value: 'upgrade', name: 'Patch', descriptor: 'dependencies', group: 'on-demand', label: 'Patch — dependencies' },
  { value: 'explore', name: 'Explore', descriptor: 'read-only', group: 'on-demand', label: 'Explore — read-only' },
  { value: 'provision', name: 'Provision', descriptor: 'infra edits', group: 'on-demand', label: 'Provision — infra edits' },
];

describe('WORKLOADS contract', () => {
  it('has exactly four options', () => {
    expect(WORKLOADS).toHaveLength(4);
  });

  it('matches the exact catalog records in order', () => {
    expect(WORKLOADS).toEqual(EXPECTED);
  });

  it('preserves the value order (drift, upgrade, explore, provision)', () => {
    expect(WORKLOADS.map((o) => o.value)).toEqual([
      'drift',
      'upgrade',
      'explore',
      'provision',
    ]);
  });

  it('renders the label as "Name — descriptor"', () => {
    for (const opt of WORKLOADS) {
      expect(opt.label).toBe(`${opt.name} — ${opt.descriptor}`);
    }
  });

  it('puts ONLY Anchor (drift) in the autonomous camp (honest 1/3 split)', () => {
    const autonomous = WORKLOADS.filter((o) => o.group === 'autonomous');
    expect(autonomous.map((o) => o.value)).toEqual(['drift']);
    expect(WORKLOADS.filter((o) => o.group === 'on-demand').map((o) => o.value)).toEqual([
      'upgrade',
      'explore',
      'provision',
    ]);
  });

  it.each(EXPECTED)('maps $value -> "$label" in group $group', ({ value, name, descriptor, group, label }) => {
    const opt = WORKLOADS.find((o) => o.value === value);
    expect(opt).toBeDefined();
    expect(opt?.name).toBe(name);
    expect(opt?.descriptor).toBe(descriptor);
    expect(opt?.group).toBe(group);
    expect(opt?.label).toBe(label);
  });

  it('every option satisfies the WorkloadOption shape', () => {
    for (const opt of WORKLOADS) {
      const shaped: WorkloadOption = opt;
      expect(typeof shaped.value).toBe('string');
      expect(typeof shaped.name).toBe('string');
      expect(typeof shaped.descriptor).toBe('string');
      expect(['autonomous', 'on-demand']).toContain(shaped.group);
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
