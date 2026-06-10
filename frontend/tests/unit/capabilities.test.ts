import { describe, it, expect } from 'vitest';
import { groupRules, CATEGORY_HEADINGS } from '../../src/lib/capabilities';
import type { CapRule } from '../../src/lib/capabilities';

// Representative subset of the real DTO — keeps all four categories and both
// known + unknown category scenarios. Server ordering preserved.

const controlPlaneRule: CapRule = {
  id: 'control-plane-service',
  description: 'No change may touch DriftScribe\'s own Cloud Run services.',
  category: 'control-plane',
};
const iamRule: CapRule = {
  id: 'iam-change-forbidden-v1',
  description: 'All IAM changes are refused — even on unrelated resources (v1 floor).',
  category: 'iam',
};
const globalRule: CapRule = {
  id: 'delete-action-forbidden-v1',
  description: 'All deletes are refused — the agent cannot destroy any resource (v1 floor).',
  category: 'global-v1',
};
const structuralRule: CapRule = {
  id: 'plan-json-unparseable',
  description: 'The plan file is not valid JSON — rejected outright (fail-closed).',
  category: 'structural',
};
const unknownRule: CapRule = {
  id: 'some-future-rule',
  description: 'A future rule that has no heading yet.',
  category: 'experimental',
};

describe('groupRules', () => {
  it('returns empty array for empty input', () => {
    expect(groupRules([])).toEqual([]);
  });

  it('groups rules by category preserving server order within each group', () => {
    const rules: CapRule[] = [controlPlaneRule, iamRule, globalRule, structuralRule];
    const groups = groupRules(rules);
    expect(groups).toHaveLength(4);
    // Each group should have its correct category
    const cpGroup = groups.find(g => g.category === 'control-plane');
    expect(cpGroup).toBeDefined();
    expect(cpGroup!.rules).toEqual([controlPlaneRule]);
    expect(cpGroup!.heading).toBe(CATEGORY_HEADINGS['control-plane']);
  });

  it('produces all four known categories as proper headings', () => {
    const rules: CapRule[] = [controlPlaneRule, iamRule, globalRule, structuralRule];
    const groups = groupRules(rules);
    const categories = groups.map(g => g.category);
    expect(categories).toContain('control-plane');
    expect(categories).toContain('iam');
    expect(categories).toContain('global-v1');
    expect(categories).toContain('structural');
    // headings come from CATEGORY_HEADINGS
    groups.forEach(g => {
      if (g.category in CATEGORY_HEADINGS) {
        expect(g.heading).toBe(CATEGORY_HEADINGS[g.category as keyof typeof CATEGORY_HEADINGS]);
      }
    });
  });

  it('unknown category → trailing group with raw category string as heading and rules not dropped', () => {
    const rules: CapRule[] = [controlPlaneRule, unknownRule];
    const groups = groupRules(rules);
    // unknown category comes last
    const lastGroup = groups[groups.length - 1];
    expect(lastGroup.category).toBe('experimental');
    // heading is the raw category string
    expect(lastGroup.heading).toBe('experimental');
    // rule is NOT dropped
    expect(lastGroup.rules).toContain(unknownRule);
  });

  it('preserves server order within a group', () => {
    const cp1: CapRule = { id: 'control-plane-bucket', description: 'bucket rule', category: 'control-plane' };
    const cp2: CapRule = { id: 'control-plane-kms', description: 'kms rule', category: 'control-plane' };
    const groups = groupRules([cp1, cp2]);
    const cpGroup = groups.find(g => g.category === 'control-plane')!;
    expect(cpGroup.rules[0]).toBe(cp1);
    expect(cpGroup.rules[1]).toBe(cp2);
  });

  it('places unknown categories after known categories as trailing groups', () => {
    const rules: CapRule[] = [controlPlaneRule, unknownRule, iamRule];
    const groups = groupRules(rules);
    const categoryOrder = groups.map(g => g.category);
    const cpIndex = categoryOrder.indexOf('control-plane');
    const iamIndex = categoryOrder.indexOf('iam');
    const unknownIndex = categoryOrder.indexOf('experimental');
    // known categories come before unknown
    expect(unknownIndex).toBeGreaterThan(cpIndex);
    expect(unknownIndex).toBeGreaterThan(iamIndex);
  });
});
