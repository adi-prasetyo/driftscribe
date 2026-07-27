import { describe, it, expect } from 'vitest';
import { groupRules, categoryHeading } from '../../src/lib/capabilities';
import type { CapRule } from '../../src/lib/capabilities';
import { translate, type TranslateFn } from '../../src/lib/i18n';

// The whole suite asserts English (setup.ts pins the suite to the EN catalog);
// categoryHeading now takes a TranslateFn, so tests thread an EN-bound one.
const t: TranslateFn = (k, p) => translate('en', k, p);

// Representative subset of the real DTO — exercises the known categories
// (control-plane, service-managed, iam, global-v1, structural) plus the
// unknown-category fallback. Server ordering preserved.

const controlPlaneRule: CapRule = {
  id: 'control-plane-service',
  description: 'The Cloud Run services.',
  category: 'control-plane',
};
const iamRule: CapRule = {
  id: 'iam-change-forbidden-v1',
  description: 'Any IAM change at all, even on unrelated resources (v1 floor).',
  category: 'iam',
};
const globalRule: CapRule = {
  id: 'delete-action-forbidden-v1',
  description: 'Deleting any resource (v1 floor).',
  category: 'global-v1',
};
const structuralRule: CapRule = {
  id: 'plan-json-unparseable',
  description: 'The plan file isn\'t valid JSON (fail-closed).',
  category: 'structural',
};
const serviceManagedRule: CapRule = {
  id: 'service-managed-bucket',
  description: 'Cloud Build, App Engine, Cloud Functions, and Cloud Run source-deploy each auto-create their own buckets. Google\'s to manage, not DriftScribe\'s to track.',
  category: 'service-managed',
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
    expect(categoryHeading(cpGroup!.category, t)).toBe('Its own control plane is off-limits');
  });

  it('renders the service-managed category under its human heading (not the raw key)', () => {
    const groups = groupRules([serviceManagedRule]);
    expect(groups).toHaveLength(1);
    expect(groups[0].category).toBe('service-managed');
    expect(categoryHeading(groups[0].category, t)).toBe('It leaves Google-created buckets alone');
    expect(categoryHeading(groups[0].category, t)).not.toBe('service-managed');
  });

  it('produces the four originally-known categories as proper headings', () => {
    const rules: CapRule[] = [controlPlaneRule, iamRule, globalRule, structuralRule];
    const groups = groupRules(rules);
    const categories = groups.map(g => g.category);
    expect(categories).toContain('control-plane');
    expect(categories).toContain('iam');
    expect(categories).toContain('global-v1');
    expect(categories).toContain('structural');
    // every known category resolves to a heading DIFFERENT from its raw category string
    groups.forEach(g => {
      expect(categoryHeading(g.category, t)).not.toBe(g.category);
    });
  });

  it('unknown category → trailing group with raw category string as heading and rules not dropped', () => {
    const rules: CapRule[] = [controlPlaneRule, unknownRule];
    const groups = groupRules(rules);
    // unknown category comes last
    const lastGroup = groups[groups.length - 1];
    expect(lastGroup.category).toBe('experimental');
    // heading falls back to the raw category string (categoryHeading never throws on an unknown id)
    expect(categoryHeading(lastGroup.category, t)).toBe('experimental');
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
