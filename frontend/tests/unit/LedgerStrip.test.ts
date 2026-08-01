import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import LedgerStrip from '../../src/components/LedgerStrip.svelte';
import { setLocale } from '../../src/lib/i18n';
import { createTraceCache } from '../../src/lib/traceCache';
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

  // ds-db0: an IaC row reaching the strip is in waiting_for_rebake, which the
  // backend records at merge — the operator already approved it. Borrowing the
  // rollback lane's solicitation copy told them otherwise.
  it('a merged iac row awaits the apply, never the approval already given', () => {
    const d = decision({
      decision_id: 'r1',
      action: 'iac_apply',
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      pr_number: 168,
    });
    const { getByText, container } = render(LedgerStrip, { props: { decisions: [d] } });
    expect(getByText('Approved · awaiting apply')).toBeTruthy();
    expect(container.textContent).not.toContain('Awaiting your approval');
    // Codex r3: name the APPLY, not the re-bake. The coordinator never observes
    // the external build — it writes waiting_for_rebake at merge and leaves it
    // until the operator's second submit — so a row claiming the re-bake is
    // outstanding goes stale the moment the build finishes, and this surface has
    // no way to notice. The apply is genuinely outstanding for the whole window.
    expect(container.textContent).not.toMatch(/re-?bake/i);
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

// ---------------------------------------------------------------------------
// ds-jns — the strip became an accordion. A row opens the decision record for
// its own trace, in place, on the page that lists it.
//
// Exclusivity is NOT tested as bookkeeping this component performs: there is
// one `recordTraceId` prop and App owns it, so "at most one open" is a property
// of the shape. What IS tested is that the component asks for the right
// transitions and renders exactly the row that id names.
// ---------------------------------------------------------------------------
describe('LedgerStrip — decision records', () => {
  const T1 = 'a'.repeat(32);
  const T2 = 'b'.repeat(32);

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  function cache() {
    return createTraceCache(
      async () =>
        new Response(JSON.stringify({ trace_id: T1, events: [], decision: null, complete: true }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
    );
  }

  function two(): Decision[] {
    return [
      decision({ decision_id: 'x1', trace_id: T1, created_at: '2026-07-28T10:00:00Z' }),
      decision({ decision_id: 'x2', trace_id: T2, created_at: '2026-07-28T09:00:00Z' }),
    ];
  }

  it('makes a row with a well-formed trace a button, and asks to open it', async () => {
    const onRecordChange = vi.fn();
    const { getAllByTestId } = render(LedgerStrip, {
      props: { decisions: two(), cache: cache(), recordTraceId: null, onRecordChange },
    });
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows[0].tagName).toBe('BUTTON');
    expect(rows[0].getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(rows[0]);
    expect(onRecordChange).toHaveBeenCalledWith(T1);
  });

  it('clicking the OPEN row asks to close it, not to reopen it', async () => {
    const onRecordChange = vi.fn();
    const { getAllByTestId } = render(LedgerStrip, {
      props: { decisions: two(), cache: cache(), recordTraceId: T1, onRecordChange },
    });
    await fireEvent.click(getAllByTestId('ledger-strip-row')[0]);
    expect(onRecordChange).toHaveBeenCalledWith(null);
  });

  it('renders the record under the named row, and under no other', async () => {
    const { getAllByTestId, getByTestId } = render(LedgerStrip, {
      props: { decisions: two(), cache: cache(), recordTraceId: T2, onRecordChange: vi.fn() },
    });
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
    expect(getAllByTestId('decision-record')).toHaveLength(1);
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows[0].getAttribute('aria-expanded')).toBe('false');
    expect(rows[1].getAttribute('aria-expanded')).toBe('true');
  });

  it('points each row at the panel it controls', async () => {
    // aria-expanded alone says "something opened" without saying WHAT. The
    // panel is tall (prose, field grid, trace timeline), so the readers that
    // offer a jump-to-controlled-element command save a long arrow trip.
    const { getAllByTestId, getByTestId } = render(LedgerStrip, {
      props: { decisions: two(), cache: cache(), recordTraceId: T2, onRecordChange: vi.fn() },
    });
    await waitFor(() => expect(getByTestId('decision-record')).toBeTruthy());
    const rows = getAllByTestId('ledger-strip-row');
    const target = rows[1].getAttribute('aria-controls');
    expect(target).toBeTruthy();
    // The id must RESOLVE — an aria-controls pointing at nothing is worse than
    // none, so assert the document actually contains that element and that it
    // is the record's container, not merely some node.
    const panel = document.getElementById(target as string);
    expect(panel).not.toBeNull();
    expect(panel?.contains(getByTestId('decision-record'))).toBe(true);
    // Each openable row names its OWN panel: a shared constant would collapse
    // every row onto one id and make the association meaningless.
    expect(rows[0].getAttribute('aria-controls')).not.toBe(target);
  });

  describe('affordance gating — a row that cannot open one gets no control', () => {
    // Decision.trace_id is optional on an open shape, and the record's URL is a
    // `?reasoning=` param — so the gate is isReplayableTraceId, the very rule
    // the deep-link parser applies. A laxer one here produces a row that opens
    // once and then fails to restore on reload (lib/deeplink's own warning).
    for (const [name, trace_id] of [
      ['no trace_id at all', undefined],
      ['an empty trace_id', ''],
      ['a short trace_id', 'abc123'],
      ['uppercase hex (the parser is case-sensitive)', 'A'.repeat(32)],
      ['33 hex chars', 'a'.repeat(33)],
    ] as const) {
      it(`renders plain text for ${name}`, () => {
        const d = decision({ decision_id: 'g1', ...(trace_id === undefined ? {} : { trace_id }) });
        const { getByTestId } = render(LedgerStrip, {
          props: { decisions: [d], cache: cache(), recordTraceId: null, onRecordChange: vi.fn() },
        });
        const row = getByTestId('ledger-strip-row');
        expect(row.tagName).not.toBe('BUTTON');
        expect(row.getAttribute('aria-expanded')).toBeNull();
      });
    }

    it('renders plain rows when no handler is wired, rather than dead buttons', () => {
      const { getAllByTestId } = render(LedgerStrip, { props: { decisions: two() } });
      for (const row of getAllByTestId('ledger-strip-row')) expect(row.tagName).not.toBe('BUTTON');
    });

    it('renders plain rows when a handler exists but no cache does', () => {
      // A button here would open a record with nothing to load it.
      const { getAllByTestId } = render(LedgerStrip, {
        props: { decisions: two(), onRecordChange: vi.fn() },
      });
      for (const row of getAllByTestId('ledger-strip-row')) expect(row.tagName).not.toBe('BUTTON');
    });
  });

  describe('show more', () => {
    const six = () =>
      Array.from({ length: 6 }, (_, i) =>
        decision({ decision_id: `s${i}`, created_at: `2026-07-28T0${i}:00:00Z` }),
      );

    it('names the whole total, and reveals every row when pressed', async () => {
      const { getByTestId, getAllByTestId, queryByTestId } = render(LedgerStrip, {
        props: { decisions: six() },
      });
      expect(getAllByTestId('ledger-strip-row')).toHaveLength(4);
      expect(getByTestId('ledger-show-more').textContent).toContain('6');
      await fireEvent.click(getByTestId('ledger-show-more'));
      expect(getAllByTestId('ledger-strip-row')).toHaveLength(6);
      // One-way: nothing offers to re-hide them.
      expect(queryByTestId('ledger-show-more')).toBeNull();
    });

    it('is absent when the cap already shows everything', () => {
      const { queryByTestId } = render(LedgerStrip, { props: { decisions: two() } });
      expect(queryByTestId('ledger-show-more')).toBeNull();
    });

    it('is absent when an explicit max already shows everything', () => {
      const { queryByTestId } = render(LedgerStrip, { props: { decisions: six(), max: 6 } });
      expect(queryByTestId('ledger-show-more')).toBeNull();
    });

    it('does not count a kept out-of-cap row as more to show', () => {
      // The open record's row is appended past the cap (ledgerRows keepTraceId),
      // so 5 rows render out of 6. The control must still offer the sixth.
      // s0 is the OLDEST (00:00) and the strip is newest-first, so it is the
      // one the 4-row cap drops — the premise this test needs. Pinning the
      // newest instead would leave the cap doing nothing and the assertion
      // below passing for the wrong reason.
      const decisions = six();
      decisions[0].trace_id = T1;
      const { getAllByTestId, getByTestId } = render(LedgerStrip, {
        props: { decisions, cache: cache(), recordTraceId: T1, onRecordChange: vi.fn() },
      });
      const ids = getAllByTestId('ledger-strip-row').map((r) => r.textContent);
      expect(ids).toHaveLength(5);
      expect(getByTestId('ledger-show-more')).toBeTruthy();
    });
  });
});

// One request can record MORE THAN ONE decision under a single trace id — the
// create-class IaC pair, a `waiting_for_rebake` pending and its merged
// successor (agent/state_store.py's find_decision_by_trace_id documents this,
// and answers with the NEWEST). Keying the accordion on the trace alone
// therefore opened a record under every matching row at once.
describe('LedgerStrip — a trace shared by several decisions', () => {
  const SHARED = 'e'.repeat(32);

  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  function cache() {
    return createTraceCache(
      async () =>
        new Response(
          JSON.stringify({ trace_id: SHARED, events: [], decision: null, complete: true }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
    );
  }

  // Newer first by created_at — the same order the backend picks its answer by.
  const pair = (): Decision[] => [
    decision({
      decision_id: 'merged',
      trace_id: SHARED,
      action: 'iac_apply',
      apply_status: 'applied',
      created_at: '2026-07-28T10:00:00Z',
    }),
    decision({
      decision_id: 'pending',
      trace_id: SHARED,
      action: 'iac_apply',
      apply_status: 'waiting_for_rebake',
      created_at: '2026-07-28T09:00:00Z',
    }),
  ];

  it('opens exactly ONE record, not one per matching row', async () => {
    const { getAllByTestId, queryAllByTestId } = render(LedgerStrip, {
      props: { decisions: pair(), cache: cache(), recordTraceId: SHARED, onRecordChange: vi.fn() },
    });
    await waitFor(() => expect(queryAllByTestId('decision-record')).toHaveLength(1));
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows).toHaveLength(2);
    // ...under the NEWEST row, which is the decision GET /trace will answer
    // with. Both this strip and that lookup order by created_at descending, so
    // "first here" and "what the backend returns" agree by construction.
    expect(rows[0].getAttribute('aria-expanded')).toBe('true');
    expect(rows[1].getAttribute('aria-expanded')).toBeNull();
  });

  it('picks the newest even when the two share a timestamp to the millisecond', () => {
    // The pair is written by ONE request, and Date.parse truncates the
    // sub-millisecond precision that separates the two docs on the server — so
    // `created_at` ties are the expected case, not a corner. ledgerRows' sort
    // returns 0 for a tie and relies on Array.prototype.sort's stability to
    // keep the input order, and the input is GET /decisions, which the backend
    // already returns newest-first. Supplied in that order here on purpose:
    // this asserts the whole chain, not just the comparator.
    const tied = pair().map((d) => ({ ...d, created_at: '2026-07-28T10:00:00Z' }));
    const { getAllByTestId } = render(LedgerStrip, {
      props: { decisions: tied, cache: cache(), recordTraceId: null, onRecordChange: vi.fn() },
    });
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows[0].tagName).toBe('BUTTON');
    expect(rows[1].tagName).not.toBe('BUTTON');
    // ...and it is the row the backend would answer with — the merged one,
    // identifiable by its seal.
    expect(rows[0].querySelector('[role="img"]')).toBeTruthy();
  });

  it('gives the older sibling no affordance at all, rather than a duplicate one', () => {
    const { getAllByTestId } = render(LedgerStrip, {
      props: { decisions: pair(), cache: cache(), recordTraceId: null, onRecordChange: vi.fn() },
    });
    const rows = getAllByTestId('ledger-strip-row');
    expect(rows[0].tagName).toBe('BUTTON');
    // The siblings are lifecycle stages of ONE reasoning run; the older row's
    // own status is what the row already says, and a second button opening the
    // same record would be two doors to one place.
    expect(rows[1].tagName).not.toBe('BUTTON');
  });
});
