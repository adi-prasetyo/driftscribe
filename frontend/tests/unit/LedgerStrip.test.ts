import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import LedgerStrip from '../../src/components/LedgerStrip.svelte';
import { setLocale } from '../../src/lib/i18n';
import * as format from '../../src/lib/format';
import type { Decision } from '../../src/lib/types';

// LedgerStrip renders whatever ledgerRows() (lib/ledger.ts) already decided —
// classification/ordering/capping are covered there. Here we exercise the
// component's own responsibilities: the mini stamp appearing only on applied
// rows, empty-list rendering nothing, and the subtitle's "omit, never
// placeholder" rule.

afterEach(cleanup);

const NOW_ISO = '2026-07-28T09:00:00Z';

function decision(overrides: Partial<Decision> & { decision_id: string }): Decision {
  return {
    action: 'no_op',
    created_at: NOW_ISO,
    ...overrides,
  } as Decision;
}

describe('LedgerStrip', () => {
  // Svelte 5 leaves a `<!---->` block anchor comment for a false {#if} even
  // when it renders no element — an implementation detail of the block, not
  // visible content — so "renders nothing" is asserted via queries a real
  // user/AT would see (no testid, no text), not raw innerHTML equality.
  it('renders nothing at all for an empty decisions list', () => {
    const { queryByTestId, container } = render(LedgerStrip, { props: { decisions: [] } });
    expect(queryByTestId('ledger-strip')).toBeNull();
    expect(container.textContent).toBe('');
  });

  it('renders nothing at all for a null/undefined decisions list', () => {
    const { queryByTestId: q1, container: c1 } = render(LedgerStrip, { props: { decisions: null } });
    expect(q1('ledger-strip')).toBeNull();
    expect(c1.textContent).toBe('');
    cleanup();
    const { queryByTestId: q2, container: c2 } = render(LedgerStrip, { props: { decisions: undefined } });
    expect(q2('ledger-strip')).toBeNull();
    expect(c2.textContent).toBe('');
  });

  it('renders the heading and one row per decision when non-empty', () => {
    const d = decision({ decision_id: 'r1', apply_status: 'applied', action: 'iac_apply' });
    const { getByTestId, getAllByTestId } = render(LedgerStrip, { props: { decisions: [d] } });
    expect(getByTestId('ledger-strip')).toBeTruthy();
    expect(getAllByTestId('ledger-strip-row')).toHaveLength(1);
  });

  it('the mini SealStamp appears ONLY on applied rows', () => {
    const applied = decision({ decision_id: 'a1', apply_status: 'applied', action: 'iac_apply', created_at: '2026-07-28T10:00:00Z' });
    const open = decision({
      decision_id: 'o1',
      approval: { approval_url: '/approvals/o1', status: 'pending' },
      created_at: '2026-07-28T09:00:00Z',
    });
    const noted = decision({ decision_id: 'n1', action: 'no_op', created_at: '2026-07-28T08:00:00Z' });
    const { getAllByTestId, queryAllByRole } = render(LedgerStrip, {
      props: { decisions: [applied, open, noted] },
    });
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows).toHaveLength(3);
    // exactly one seal stamp (role="img", SealStamp.svelte) across all rows
    const stamps = queryAllByRole('img');
    expect(stamps).toHaveLength(1);
    // and it lives in the applied row specifically
    const appliedRow = rows.find((r) => r.getAttribute('data-state') === 'applied');
    expect(appliedRow?.querySelector('[role="img"]')).toBeTruthy();
    const openRow = rows.find((r) => r.getAttribute('data-state') === 'open');
    const notedRow = rows.find((r) => r.getAttribute('data-state') === 'noted');
    expect(openRow?.querySelector('[role="img"]')).toBeFalsy();
    expect(notedRow?.querySelector('[role="img"]')).toBeFalsy();
  });

  it('applied row title reads "Approved · applied"', () => {
    const d = decision({ decision_id: 'a2', apply_status: 'applied', action: 'iac_apply' });
    const { getByText } = render(LedgerStrip, { props: { decisions: [d] } });
    expect(getByText('Approved · applied')).toBeTruthy();
  });

  // The settled rows must not name an actor: the approval doc carries
  // status/phase/resolved_at but no actor field, so second-person copy would
  // assert an identity the system never captured (an anonymous demo-window
  // click produces an identical record). Pins the claim, not just the string.
  it('settled row titles never claim who approved', () => {
    const rows = [
      decision({ decision_id: 's1', apply_status: 'applied', action: 'iac_apply' }),
      decision({ decision_id: 's2', apply_status: 'failed', action: 'rollback' }),
    ];
    const { container } = render(LedgerStrip, { props: { decisions: rows } });
    expect(container.textContent).not.toMatch(/You approved/);
  });

  it('open row title reads "Awaiting your approval"', () => {
    const d = decision({
      decision_id: 'o2',
      approval: { approval_url: '/approvals/o2', status: 'pending' },
    });
    const { getByText } = render(LedgerStrip, { props: { decisions: [d] } });
    expect(getByText('Awaiting your approval')).toBeTruthy();
  });

  it('noted row title falls back to decisionActionLabel (e.g. no_op → the friendly label)', () => {
    const d = decision({ decision_id: 'n2', action: 'no_op' });
    const { getByText, queryByText } = render(LedgerStrip, { props: { decisions: [d] } });
    // no_op must NOT render the raw enum text, and must render its friendly
    // decisionActionLabel translation instead (shared.decision.noOp).
    expect(queryByText('no_op')).toBeFalsy();
    expect(getByText('No action needed')).toBeTruthy();
  });

  describe('subtitle: best-effort identity, never a placeholder', () => {
    it('prefers pr_title when present', () => {
      const d = decision({
        decision_id: 's1',
        action: 'iac_apply',
        apply_status: 'applied',
        pr_number: 42,
        pr_title: 'Adopt orders-sub into IaC',
      });
      const { getByText, queryByText } = render(LedgerStrip, { props: { decisions: [d] } });
      expect(getByText('Adopt orders-sub into IaC')).toBeTruthy();
      expect(queryByText('#42')).toBeFalsy();
    });

    it('falls back to #{pr_number} when pr_title is absent', () => {
      const d = decision({
        decision_id: 's2',
        action: 'iac_apply',
        apply_status: 'applied',
        pr_number: 43,
      });
      const { getByText } = render(LedgerStrip, { props: { decisions: [d] } });
      expect(getByText('#43')).toBeTruthy();
    });

    it('omits the <small> entirely when neither pr_title nor pr_number is present (no placeholder text)', () => {
      const d = decision({ decision_id: 's3', action: 'no_op' });
      const { getByTestId } = render(LedgerStrip, { props: { decisions: [d] } });
      const row = getByTestId('ledger-strip-row');
      expect(row.querySelector('small')).toBeNull();
      expect(row.textContent).not.toMatch(/—|unknown/i);
    });
  });

  it('respects an explicit max prop (fewer rows rendered than decisions supplied)', () => {
    const decisions = Array.from({ length: 6 }, (_, i) =>
      decision({ decision_id: `m${i}`, created_at: `2026-07-28T0${i}:00:00Z` }),
    );
    const { getAllByTestId } = render(LedgerStrip, { props: { decisions, max: 2 } });
    expect(getAllByTestId('ledger-strip-row')).toHaveLength(2);
  });

  describe('the per-row clock follows the app locale toggle', () => {
    afterEach(() => setLocale('en'));

    // With `hourCycle: 'h23'` pinned in fmtClock, EN and JA render IDENTICAL
    // digits for the same instant (both use Arabic numerals) — so diffing the
    // rendered DOM text between locales can never distinguish "the component
    // threaded $locale into fmtClock" from "it silently dropped the arg and
    // fmtClock fell back to the host locale" (the h23 pin masks that
    // difference by design). A call-site spy is the only way to actually
    // observe which locale argument the component passed, so that's what
    // this asserts, instead of DOM-diffing the two locales against each other.
    it('calls fmtClock with the active app locale ($locale), for both EN and JA', () => {
      const spy = vi.spyOn(format, 'fmtClock');
      const d = decision({ decision_id: 't1', action: 'no_op', created_at: '2026-07-28T09:15:00Z' });

      setLocale('en');
      const { unmount } = render(LedgerStrip, { props: { decisions: [d] } });
      expect(spy).toHaveBeenCalledWith('2026-07-28T09:15:00Z', 'en');
      unmount();
      spy.mockClear();

      setLocale('ja');
      render(LedgerStrip, { props: { decisions: [d] } });
      expect(spy).toHaveBeenCalledWith('2026-07-28T09:15:00Z', 'ja');

      spy.mockRestore();
    });
  });
});
