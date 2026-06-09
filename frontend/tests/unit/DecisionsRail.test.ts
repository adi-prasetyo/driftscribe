import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import DecisionsRail from '../../src/components/DecisionsRail.svelte';
import type { Decision } from '../../src/lib/types';

// First component-render test in the repo. Uses @testing-library/svelte (v5,
// Svelte-5-native render) on the jsdom environment configured in
// vitest.config.ts. We assert the supersession behaviour (a later `applied`
// iac_apply row retires the stale "Review & approve →" CTA on its
// `waiting_for_rebake` siblings) and the new meta-line status token end-to-end
// through the component, not just via the pure helpers.

afterEach(cleanup);

const noop = () => {};

/** Build an iac_apply decision row with sane defaults. */
function iacRow(over: Partial<Decision>): Decision {
  return {
    decision_id: `d-${Math.random().toString(36).slice(2)}`,
    action: 'iac_apply',
    pr_number: 68,
    github: { url: 'https://github.com/adi-prasetyo/driftscribe/pull/68' },
    ...over,
  } as Decision;
}

describe('DecisionsRail — iac_apply CTA supersession + status token', () => {
  it('downgrades both waiting rows to "Open approval page →" when an applied sibling exists for the same PR', () => {
    // Two waiting_for_rebake rows + one applied row, all PR #68. The applied row
    // supersedes the two waiting ones → all three read the neutral view-only label.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68 }),
      iacRow({ decision_id: 'wait-68-a', apply_status: 'waiting_for_rebake', pr_number: 68 }),
      iacRow({ decision_id: 'wait-68-b', apply_status: 'waiting_for_rebake', pr_number: 68 }),
    ];

    const { getAllByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });

    const links = getAllByTestId('iac-approve-link');
    expect(links).toHaveLength(3);
    for (const a of links) {
      expect(a.textContent?.trim()).toBe('Open approval page →');
      // Downgrade is label-only: the href stays /iac-approvals/68 for every row.
      expect(a.getAttribute('href')).toBe('/iac-approvals/68');
    }
    // And NONE of them advertises the live CTA.
    expect(links.some((a) => a.textContent?.includes('Review & approve'))).toBe(false);
  });

  it('keeps "Review & approve →" on a lone waiting row with no applied sibling', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'wait-71', apply_status: 'waiting_for_rebake', pr_number: 71 }),
    ];

    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });

    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Review & approve →');
    expect(link.getAttribute('href')).toBe('/iac-approvals/71');
  });

  it('renders the apply_status token on the meta line (applied + awaiting re-bake)', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68, head_sha: '0496b305deadbeef' }),
      iacRow({ decision_id: 'wait-71', apply_status: 'waiting_for_rebake', pr_number: 71, head_sha: '0496b305deadbeef' }),
    ];

    const { container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });

    // Svelte trims the whitespace at `{#if}` boundaries, so the literal
    // ` · {st}` markup renders the separator without a leading space — the
    // SAME established convention the SHA token already uses
    // ("iac_apply· ⎇ <sha>"). We assert the actual rendered token order:
    // action tag · status · ⎇ sha.
    const metas = Array.from(container.querySelectorAll('.row-meta')).map((n) =>
      n.textContent?.trim(),
    );
    expect(metas).toContain('iac_apply· applied· ⎇ 0496b30');
    expect(metas).toContain('iac_apply· awaiting re-bake· ⎇ 0496b30');
    // The status token sits BETWEEN the action tag and the SHA.
    const applied = metas.find((t) => t?.includes('applied'))!;
    expect(applied.indexOf('applied')).toBeGreaterThan(applied.indexOf('iac_apply'));
    expect(applied.indexOf('applied')).toBeLessThan(applied.indexOf('⎇'));
  });
});
