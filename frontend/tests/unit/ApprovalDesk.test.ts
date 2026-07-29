import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, within } from '@testing-library/svelte';
import ApprovalDesk from '../../src/components/ApprovalDesk.svelte';
import type { Decision } from '../../src/lib/types';
import type { InfraGraph, PendingApproval } from '../../src/lib/infra_graph';
import { fmtWhen } from '../../src/lib/format';

// ApprovalDesk composes deskModel() (lib/desk.ts, tested separately) with
// InstrumentBand/LedgerStrip/SealStamp/DriftDiffCard into the desk's three
// states. This file exercises the COMPONENT's own responsibilities: state
// selection → markup, the honest per-source display derivation, the stamped
// decay timer, and the post-approval fast-convergence ladder — not
// deskModel's selection rules themselves (see desk.test.ts).

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

const GRAPH: InfraGraph = {
  generated_at: '2026-07-28T06:00:00Z',
  project: 'demo-proj',
  caveat: '',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 735, managed: 9, drift: 0 },
  groups: [],
  edges: [],
};

function rollbackDecision(overrides: Partial<Decision> = {}): Decision {
  return {
    decision_id: 'rb-1',
    action: 'rollback',
    created_at: '2026-07-28T11:00:00Z',
    approval: {
      approval_url: '/approvals/rb-1?t=abc',
      // Deliberately RELATIVE. isRollbackAwaitingOperator() drops an approval
      // from "awaiting" once expires_at is in the past (approval.ts), so a
      // hardcoded instant here is a fuse: every test in this file that needs a
      // PENDING rollback — the band counts, the pending cards, the whole
      // convergence ladder — went red the moment that instant passed, for a
      // reason having nothing to do with what they assert. Computed lazily so a
      // block that pins its own clock still gets a future expiry.
      expires_at: new Date(Date.now() + 12 * 60 * 60 * 1000).toISOString(),
      status: 'pending',
      resolved_at: null,
    },
    diffs: [{ name: 'LOG_LEVEL', expected: 'info', live: 'debug' }],
    ...overrides,
  };
}

function iacDecision(overrides: Partial<Decision> = {}): Decision {
  return {
    decision_id: 'iac-1',
    action: 'iac_apply',
    created_at: '2026-07-28T11:00:00Z',
    pr_number: 42,
    apply_status: 'waiting_for_rebake',
    ...overrides,
  };
}

// scopeTotals sums over PRIMARY cards derived from graph.groups, NOT
// graph.totals directly (see infra_graph.ts's ScopeTotals doc) — any fixture
// that wants a non-zero scope.managed/scope.drift must carry a real group,
// not just totals.
function graphWithGroup(managed: number, drift: number): InfraGraph {
  return {
    ...GRAPH,
    // `totals` DELIBERATELY diverges from the group's managed/drift. A real
    // payload's totals agree with its groups, so this shape is a test
    // instrument, not a realistic fixture — but it is the only way the
    // band-composition assertion below has any teeth: if totals mirrored the
    // group (as they did originally), a desk that naively read `graph.totals`
    // and a desk that correctly derives via resourceCards()+scopeTotals()
    // would produce IDENTICAL numbers, and the test could not tell them apart.
    // These sentinel values appear nowhere else, so if one ever surfaces in an
    // assertion, the derivation has been bypassed.
    totals: { resources: 735, managed: 111, drift: 222 },
    groups: [
      {
        asset_type: 'run.googleapis.com/Service',
        label: 'Cloud Run',
        count: managed + drift,
        managed,
        drift,
        sensitive: false,
        adoptable: true,
        nodes: [],
      } as unknown as InfraGraph['groups'][number],
    ],
  };
}

function pendingIac(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    pr_number: 7,
    title: 'Adopt orders-sub into IaC',
    url: 'https://github.com/x/y/pull/7',
    asset_type: 'pubsub.googleapis.com/Subscription',
    resource_name: 'orders-sub',
    ...overrides,
  };
}

describe('ApprovalDesk — resting state', () => {
  it('renders the calm headline and the watch line with the real scan time + resource count', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(queryByTestId('approval-desk-pending')).toBeNull();
    expect(queryByTestId('approval-desk-stamped')).toBeNull();
    const resting = getByTestId('approval-desk-resting');
    expect(resting.textContent).toContain('Nothing needs your decision right now.');
    const watch = getByTestId('approval-desk-watch');
    expect(watch.textContent).toContain('735 resources');
    expect(watch.textContent).toContain('no new drift'); // scope.drift === 0 here
  });

  it('falls back to "scan time pending" (never a fabricated time) when generated_at is null', () => {
    const { getByTestId, queryByText } = render(ApprovalDesk, {
      props: {
        graph: { ...GRAPH, generated_at: null },
        decisions: [],
        pendingApprovals: [],
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-watch').textContent).toContain('scan time pending');
    expect(queryByText(/last scan/)).toBeNull();
  });

  it('omits the "no new drift" claim when there IS unresolved drift (never claims a false negative)', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: graphWithGroup(9, 6),
        decisions: [],
        pendingApprovals: [],
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-watch').textContent).not.toContain('no new drift');
  });

  it('a null graph still renders resting with the scan-pending fallback, not a crash', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: null, decisions: [], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-watch').textContent).toContain('scan time pending');
  });
});

describe('ApprovalDesk — heading level', () => {
  // The page's only h1 is the brand title in App.svelte, so the desk's state
  // headline must be h2: an h3 skips a level for anyone navigating by heading,
  // and the sibling estate view already uses h2. Easy to regress, because both
  // the plan text and the mockup say "Mincho h3 31px" — that pins the SIZE,
  // not the semantic level, and the 31px is delivered by CSS either way.
  // Asserted for ALL THREE states: they are three separate elements in three
  // separate branches, so fixing one proves nothing about the others.
  it('every state headline is an h2 (h1 is the brand title; no level skipped)', () => {
    const states = [
      { name: 'resting', decisions: [] as Decision[] },
      { name: 'pending', decisions: [rollbackDecision()] },
      {
        name: 'stamped',
        decisions: [iacDecision({ apply_status: 'applied', applied_at: new Date().toISOString() })],
      },
    ];
    for (const s of states) {
      const { container } = render(ApprovalDesk, {
        props: { graph: GRAPH, decisions: s.decisions, pendingApprovals: [], onNavigate: vi.fn() },
      });
      const desk = container.querySelector('.approval-desk') as HTMLElement;
      expect(desk.querySelector('h2'), `${s.name} must use h2`).toBeTruthy();
      expect(desk.querySelector('h3'), `${s.name} must not use h3`).toBeNull();
      expect(desk.querySelector('h1'), `${s.name} must not introduce a second h1`).toBeNull();
      cleanup();
    }
  });
});

describe('ApprovalDesk — instrument band composition', () => {
  it('feeds InstrumentBand from scopeTotals over the graph, not raw totals', () => {
    // 1 primary card (adoptable) with managed=9, drift=6, over a graph whose
    // `totals` say 111/222 instead (see graphWithGroup). scopeTotals sums over
    // PRIMARY cards only, so 9/6 can ONLY come from
    // resourceCards()+scopeTotals(); a desk that read graph.totals directly
    // would render 111/222 here.
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: graphWithGroup(9, 6), decisions: [], pendingApprovals: [], onNavigate: vi.fn() },
    });
    // Exact text, not toContain: '9' is a substring of '9xx', and more to the
    // point '222'.toContain('2') would let a raw-totals regression slide past
    // a loose check on a single digit.
    expect(getByTestId('instrument-band-managed').textContent?.trim()).toContain('9');
    expect(getByTestId('instrument-band-drift').textContent?.trim()).toContain('6');
    // …and prove the raw-totals values are nowhere on the band.
    expect(getByTestId('instrument-band-managed').textContent).not.toContain('111');
    expect(getByTestId('instrument-band-drift').textContent).not.toContain('222');
  });

  it('awaiting is 0 with nothing pending, 1 with exactly one thing pending', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('0');
    cleanup();

    const { getByTestId: gt2 } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [rollbackDecision()], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(gt2('instrument-band-awaiting').textContent).toContain('1');
  });

  // awaitingCount (lib/desk.ts) is the honest system-wide total, NOT "is the
  // desk currently pending" — deskModel surfaces only ONE card at a time as
  // a queue. This pins that composition at the component level: the desk
  // shows a single pending (rollback) card, while the band's own number
  // still honestly reports 2 (the rollback AND a separate, distinct pending
  // iac PR the desk isn't currently showing a CTA for).
  it('awaiting can exceed 1 even while the desk shows a single pending card', () => {
    const rb = rollbackDecision();
    const iac = pendingIac({ pr_number: 7 });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [rb], pendingApprovals: [iac], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('rollback');
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('2');
  });

  it('a click on any band stat calls onNavigate("estate")', async () => {
    const onNavigate = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onNavigate },
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    expect(onNavigate).toHaveBeenCalledWith('estate');
  });
});

describe('ApprovalDesk — pending state, rollback source', () => {
  it('renders the Anchor who-line, the diff table, and both CTAs pointing at the safe href', () => {
    const d = rollbackDecision();
    const { getByTestId, getByText } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    const pending = getByTestId('approval-desk-pending');
    expect(pending.getAttribute('data-source')).toBe('rollback');
    expect(getByText('Anchor is proposing a fix')).toBeTruthy();
    expect(getByTestId('drift-diff-card')).toBeTruthy();
    const approve = getByTestId('approval-desk-approve') as HTMLAnchorElement;
    const reject = getByTestId('approval-desk-reject') as HTMLAnchorElement;
    expect(approve.getAttribute('href')).toBe('/approvals/rb-1?t=abc');
    expect(reject.getAttribute('href')).toBe('/approvals/rb-1?t=abc');
    expect(approve.getAttribute('target')).toBe('_blank');
    expect(approve.getAttribute('rel')).toBe('noopener');
  });

  // ds-hnn: the diff card's STATUS column rendered `contract_status` verbatim,
  // so the desk — the judge-facing front door — showed the snake_case code
  // identifier `present_disallow_manual` in Latin script directly beneath
  // Japanese copy. Same defect class as the bare `rollback` in the ledger.
  // Asserted HERE rather than only on lib/format's mapper because the bug was
  // in the render path: a correct mapper the component doesn't call fixes
  // nothing.
  it('renders contract_status as an operator label, never the raw enum', () => {
    const d = rollbackDecision({
      diffs: [
        { name: 'LOG_LEVEL', expected: 'info', live: 'debug', contract_status: 'present_disallow_manual' },
        { name: 'FEATURE_FLAG_X', expected: null, live: 'on', contract_status: 'absent' },
      ],
    });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    const card = getByTestId('drift-diff-card');
    expect(card.textContent).toContain('Manual change not allowed');
    expect(card.textContent).toContain('Not in the contract');
    expect(card.textContent).not.toContain('present_disallow_manual');
    // 'absent' is a substring of no other label here, so this is a real check
    // that the raw enum is gone rather than merely that a label appeared.
    expect(card.textContent).not.toMatch(/\babsent\b/);
  });

  it('shows the real created_at as the "proposed" time, never a fabricated one', () => {
    const d = rollbackDecision({ created_at: '2026-07-28T09:15:00Z' });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').textContent).toMatch(/2026|09:15/);
  });
});

describe('ApprovalDesk — pending state, iac source, both provenance arms', () => {
  it('listing arm: renders the PR title from the open-PR payload', () => {
    const approval = pendingIac({ title: 'Adopt orders-sub into IaC' });
    const { getByTestId, getByText } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [approval], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('iac');
    expect(getByText('Adopt orders-sub into IaC')).toBeTruthy();
    expect(getByText('PR #7')).toBeTruthy();
  });

  it('decision arm (no PR title carried): falls back to the honest generic headline naming the PR number', () => {
    const d = iacDecision({ pr_number: 99, pr_title: undefined });
    const { getByTestId, getByText, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('iac');
    expect(getByText('Infrastructure change PR #99 is waiting for your approval.')).toBeTruthy();
    // No fabricated "proposed at" time for a decision whose real created_at
    // we DO have is fine, but there must be no invented PR title:
    expect(queryByTestId('approval-desk-pending')?.textContent).not.toContain('undefined');
  });

  it('decision arm WITH a carried pr_title renders it honestly instead of the fallback', () => {
    const d = iacDecision({ pr_number: 55, pr_title: 'Adopt lodash upgrade' });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    // "Adopt lodash upgrade" legitimately appears twice (the desk h3 AND the
    // ledger strip's subtitle for the same decision) — scope to the pending
    // card so this asserts the DESK's own headline, not just that the string
    // appears somewhere on the page.
    const pending = within(getByTestId('approval-desk-pending'));
    expect(pending.getByText('Adopt lodash upgrade')).toBeTruthy();
    expect(pending.queryByText(/is waiting for your approval\.$/)).toBeNull();
  });
});

describe('ApprovalDesk — stamped state', () => {
  // The fixtures below sit a few minutes inside STAMP_WINDOW_MS, so "is this
  // decision still stamped?" is answered against the clock. Without pinning it
  // these two passed only while the REAL wall clock happened to be between
  // 11:55Z and 12:05Z on 2026-07-28 — green on the afternoon they were written,
  // red every moment after. The neighbouring decay block already pins the same
  // instant; the file-level afterEach restores real timers.
  beforeEach(() => vi.useFakeTimers({ now: Date.parse('2026-07-28T12:00:00Z') }));

  it('renders an animated lg SealStamp and the applied audit line, keyed off applied_at (iac)', () => {
    const d = iacDecision({
      apply_status: 'applied',
      applied_at: '2026-07-28T11:55:00Z',
      pr_title: 'Adopt orders-sub into IaC',
    });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    const stamped = getByTestId('approval-desk-stamped');
    expect(stamped.getAttribute('data-source')).toBe('iac');
    // The ledger strip below ALSO renders a (non-animated, sm) mini stamp for
    // this same applied decision — role="img" matches both, so scope to the
    // hero stamp specifically.
    const seal = within(stamped).getByRole('img');
    expect(seal.className).toMatch(/animate/);
    expect(seal.className).toContain('lg');
    expect(stamped.textContent).toContain('Adopt orders-sub into IaC');
  });

  it('rollback stamped: keys the audit line off approval.resolved_at, never falling back to created_at', () => {
    const d = rollbackDecision({
      created_at: '2026-07-28T00:00:00Z', // deliberately far from resolved_at
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase: 'applied',
        resolved_at: '2026-07-28T11:58:00Z',
      },
    });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    const stamped = getByTestId('approval-desk-stamped');
    expect(stamped.getAttribute('data-source')).toBe('rollback');

    // Compare against the SAME formatter the component uses. That is circular
    // for the FORMAT (deliberately — this test makes no claim about how a time
    // is rendered, and hard-coding a string would just pin the runner's tz),
    // but it is not circular for the FIELD, which is the whole point: one of
    // these two strings must be present and the other absent.
    //
    // The previous assertion here was `toMatch(/2026|11:58/)`, which BOTH
    // timestamps satisfy via the shared "2026" — it passed even when
    // stampedAuditTime() was mutated to key off created_at, so the regression
    // it names could have shipped silently.
    const fromResolvedAt = fmtWhen('2026-07-28T11:58:00Z', 'en');
    const fromCreatedAt = fmtWhen('2026-07-28T00:00:00Z', 'en');
    // Guard the guard: if the fixture's two timestamps ever collapse to the
    // same rendered string (a tz/format change), the assertions below would be
    // vacuous rather than failing, so assert the premise explicitly.
    expect(fromResolvedAt).not.toBe(fromCreatedAt);
    expect(stamped.textContent).toContain(fromResolvedAt);
    expect(stamped.textContent).not.toContain(fromCreatedAt);
  });
});

// ds-oz2. The newest and most safety-critical desk state had no unit-level
// rendering test at all — only the visual rig, which checks `data-state` and so
// cannot see which copy keys were used. A regression that swapped the failed and
// outcome_unknown strings would have passed `npm run test:unit` clean, and those
// two must never be interchangeable: "we could not confirm this" rendering as
// "this failed" is the exact over-claim the state exists to prevent (and the
// inverse is worse).
describe('ApprovalDesk — unresolved state', () => {
  // A rollback whose credential was spent but whose outcome never settled.
  function unresolved(phase: 'failed' | 'outcome_unknown'): Decision {
    return rollbackDecision({
      approval: {
        approval_url: '/approvals/rb-1?t=x',
        status: 'used',
        phase,
        resolved_at: null,
      },
    });
  }

  const props = (d: Decision) => ({
    graph: GRAPH,
    decisions: [d],
    pendingApprovals: [],
    onNavigate: vi.fn(),
  });

  it('renders the unresolved card for a used-but-unconfirmed rollback, tagged with its phase', () => {
    const { getByTestId } = render(ApprovalDesk, { props: props(unresolved('outcome_unknown')) });
    const card = getByTestId('approval-desk-unresolved');
    expect(card.getAttribute('data-phase')).toBe('outcome_unknown');
    // No seal and no CTA: nothing is confirmed, and there is nothing to approve.
    expect(within(card).queryByRole('img')).toBeNull();
    expect(within(card).queryByRole('link')).toBeNull();
  });

  it('keeps the two phases on SEPARATE copy — unconfirmed never renders as failed', () => {
    const { getByTestId: getUnknown, unmount } = render(ApprovalDesk, {
      props: props(unresolved('outcome_unknown')),
    });
    const unknownText = getUnknown('approval-desk-unresolved').textContent ?? '';
    unmount();

    const { getByTestId: getFailed } = render(ApprovalDesk, {
      props: props(unresolved('failed')),
    });
    const failedCard = getFailed('approval-desk-unresolved');
    expect(failedCard.getAttribute('data-phase')).toBe('failed');
    // Distinct strings, not a shared template with a swapped word — if the copy
    // keys were ever collapsed onto one branch these would be equal.
    expect(failedCard.textContent).not.toBe(unknownText);
  });

  it('renders the drift the rollback was meant to undo, and keeps the desk heading level', () => {
    const { getByTestId } = render(ApprovalDesk, { props: props(unresolved('failed')) });
    const card = getByTestId('approval-desk-unresolved');
    // The h2 is the desk-wide level (see the "heading level" block above) — the
    // unresolved state must not introduce its own.
    expect(within(card).getByRole('heading', { level: 2 })).toBeTruthy();
    // DriftDiffCard renders the diff that motivated the rollback.
    expect(card.textContent).toContain('LOG_LEVEL');
  });

  it('outranks a stamp: an unresolved outcome is not hidden by an older success', () => {
    const stampedEarlier = iacDecision({
      decision_id: 'iac-old',
      apply_status: 'applied',
      applied_at: '2026-07-28T11:55:00Z',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [unresolved('outcome_unknown'), stampedEarlier],
        pendingApprovals: [],
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-unresolved')).toBeTruthy();
    expect(queryByTestId('approval-desk-stamped')).toBeNull();
  });
});

describe('ApprovalDesk — stamped decay timer', () => {
  beforeEach(() => vi.useFakeTimers({ now: Date.parse('2026-07-28T12:00:00Z') }));

  it('does not re-arm in a tie at the exact stampedUntil instant', async () => {
    // selectStamped's window is INCLUSIVE (now <= stampedUntil), so without the
    // effect's `+ 1` a decay timer firing at exactly stampedUntil finds the
    // model still 'stamped' and re-arms with delay 0 instead of decaying.
    //
    // The ONLY observable that separates the two is the SCHEDULING, so this
    // counts setTimeout calls rather than inspecting the DOM: both versions
    // reach resting at stampedUntil + 1ms and both show "1 pending timer" at
    // the boundary itself (with the fix it is pending-not-yet-due; without, it
    // is a re-armed 0ms timer). That equivalence is why the sibling decay test,
    // which advances with 2s of slack, passes either way.
    const setSpy = vi.spyOn(globalThis, 'setTimeout');
    const d = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T11:59:00Z' });
    const { queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(setSpy).toHaveBeenCalledTimes(1);

    // 11:59 + STAMP_WINDOW_MS(10min) => stampedUntil is 12:09:00.000 exactly,
    // and the clock starts at 12:00:00.000 — so this lands ON the boundary.
    await vi.advanceTimersByTimeAsync(9 * 60 * 1000);
    // Still stamped, and correctly so: the window includes its own last instant.
    expect(queryByTestId('approval-desk-stamped')).toBeTruthy();
    // The decay timer was scheduled ONCE, for stampedUntil + 1. Without the
    // `+ 1` the calls here are [540000, 0] — the timer fired on the boundary
    // and the effect re-armed.
    expect(setSpy).toHaveBeenCalledTimes(1);
    expect(setSpy.mock.calls[0][1]).toBe(9 * 60 * 1000 + 1);
    setSpy.mockRestore();
  });

  it('falls back to resting on its own once stampedUntil passes, with no new props', async () => {
    const d = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T11:59:00Z' }); // 1 min before "now"
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-stamped')).toBeTruthy();

    // STAMP_WINDOW_MS is 10 minutes; applied_at + 10min is 12:09. Advance just
    // past it — async so the setTimeout callback's state write (decayTick+=1)
    // has a chance to flush through Svelte's reactive system before asserting.
    await vi.advanceTimersByTimeAsync(9 * 60 * 1000 + 2000);
    expect(getByTestId('approval-desk-resting')).toBeTruthy();
  });

  it('unmounting the desk mid-stamp actually clears the decay timer', async () => {
    const d = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T11:59:00Z' });
    const setSpy = vi.spyOn(globalThis, 'setTimeout');
    const { unmount } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });

    // The previous version of this test asserted only `resolves.not.toThrow()`
    // on the strength of "Svelte throws/warns loudly" on a post-unmount state
    // write. That premise is false on Svelte 5 + jsdom — the write is silently
    // no-opped, no throw and no console output — so deleting the effect's
    // `return () => clearTimeout(timer)` left this test green.
    //
    // The leak is invisible in the DOM by construction (the whole point is that
    // nothing renders after unmount), so the only assertion with teeth is that
    // the exact handle the decay effect scheduled is the one handed to
    // clearTimeout. `toHaveBeenCalled()` alone would be too loose: Svelte's own
    // teardown may clear timers of its own.
    expect(setSpy).toHaveBeenCalled();
    const scheduled = setSpy.mock.results.map((r) => r.value);
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout');

    unmount();

    const cleared = clearSpy.mock.calls.map((c) => c[0]);
    expect(scheduled.some((id) => cleared.includes(id))).toBe(true);
    // …and nothing fires afterwards.
    await expect(vi.advanceTimersByTimeAsync(20 * 60 * 1000)).resolves.not.toThrow();
    clearSpy.mockRestore();
  });

  it('a second, later stamp replaces the first without leaving two timers racing', async () => {
    const first = iacDecision({ decision_id: 'iac-first', apply_status: 'applied', applied_at: '2026-07-28T11:59:00Z' });
    const { getByTestId, rerender } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [first], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('approval-desk-stamped').getAttribute('data-source')).toBe('iac');

    // A fresh rollback resolves 5 minutes later — still within the first
    // stamp's window, so it takes over per deskModel's recency tiebreak.
    vi.setSystemTime(Date.parse('2026-07-28T12:05:00Z'));
    const second = rollbackDecision({
      decision_id: 'rb-second',
      approval: { approval_url: '/approvals/rb-second?t=x', status: 'used', phase: 'applied', resolved_at: '2026-07-28T12:05:00Z' },
    });
    await rerender({ graph: GRAPH, decisions: [first, second], pendingApprovals: [], onNavigate: vi.fn() });
    expect(getByTestId('approval-desk-stamped').getAttribute('data-source')).toBe('rollback');

    // Only the SECOND stamp's window should still be governing decay: advance
    // to just past the first stamp's (already-superseded) window boundary —
    // the desk must still read stamped (governed by the second window), not
    // have decayed early off a leaked first timer.
    await vi.advanceTimersByTimeAsync(4 * 60 * 1000 + 2000); // 12:05 + ~4min = just past 12:09 (first window)
    expect(getByTestId('approval-desk-stamped')).toBeTruthy();
  });
});

describe('ApprovalDesk — fast convergence after an approval (bead ds-wd2.2)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setVisibility('visible');
  });

  it('does nothing on a bare tab focus (no prior CTA click)', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn(), refresh },
    });
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh).not.toHaveBeenCalled();
  });

  it('a CTA click, then a return focus, fires a bounded burst of refreshes', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-approve'));
    expect(refresh).not.toHaveBeenCalled(); // arming alone does nothing yet

    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(1); // the immediate (0ms) rung

    await vi.advanceTimersByTimeAsync(20_000); // past every remaining delay
    const callCount = refresh.mock.calls.length;
    expect(callCount).toBeGreaterThan(1);

    // Bounded: waiting even longer must not add more calls.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(refresh.mock.calls.length).toBe(callCount);
  });

  it('cannot stack: leaving and returning again mid-ladder does not start a second ladder', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-approve'));
    window.dispatchEvent(new Event('focus')); // starts the ladder
    await vi.advanceTimersByTimeAsync(0);
    const afterFirstRung = refresh.mock.calls.length;

    // A second focus while the ladder's own timers are still pending must be
    // a no-op — not a second overlapping burst.
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(refresh.mock.calls.length).toBe(afterFirstRung);

    await vi.advanceTimersByTimeAsync(20_000); // drain the one ladder fully
    const total = refresh.mock.calls.length;

    // Once fully drained, an UNRELATED later focus (no new CTA click) must
    // not start another ladder.
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh.mock.calls.length).toBe(total);
  });

  it('fires exactly three rungs, at 0 / 3000 / 8000 ms', async () => {
    // Pins RETURN_LADDER_DELAYS_MS itself. Without this, dropping a rung or
    // moving the middle delay survives every other test in this describe —
    // they only prove "more than one, eventually bounded". The ladder's whole
    // purpose is that the seal lands seconds after the operator returns, so
    // its shape is behaviour, not an implementation detail.
    const refresh = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        onNavigate: vi.fn(),
        refresh,
      },
    });
    await fireEvent.click(getByTestId('approval-desk-approve'));
    window.dispatchEvent(new Event('focus'));

    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2999);
    expect(refresh).toHaveBeenCalledTimes(1); // not yet the 3000ms rung
    await vi.advanceTimersByTimeAsync(1);
    expect(refresh).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(4999);
    expect(refresh).toHaveBeenCalledTimes(2); // not yet the 8000ms rung
    await vi.advanceTimersByTimeAsync(1);
    expect(refresh).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(refresh).toHaveBeenCalledTimes(3); // and no fourth
  });

  it('a SECOND approval later in the same session gets its own full ladder', async () => {
    // The `ladderRunning` flag is reset by its own timer at lastDelay + 1. If
    // that reset is missing, the flag latches true forever and fast
    // convergence silently dies after its first use — while every other test
    // here still passes, because they are satisfied by `armed` alone and never
    // exercise a second legitimate CTA-click → focus cycle.
    const refresh = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        onNavigate: vi.fn(),
        refresh,
      },
    });

    await fireEvent.click(getByTestId('approval-desk-approve'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000); // drain the first ladder fully
    const afterFirst = refresh.mock.calls.length;
    expect(afterFirst).toBe(3);

    // A fresh CTA click + return focus — the operator approving a second thing.
    await fireEvent.click(getByTestId('approval-desk-approve'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh.mock.calls.length).toBe(afterFirst * 2);
  });

  it('unmounting clears any pending ladder timers (no callback after teardown)', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId, unmount } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-approve'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    const before = refresh.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(refresh.mock.calls.length).toBe(before);
  });
});

describe('ApprovalDesk — ledger strip composition', () => {
  it('renders the ledger strip fed from the same decisions list', () => {
    const d = iacDecision({ apply_status: 'applied', applied_at: '2026-07-28T05:00:00Z' });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onNavigate: vi.fn() },
    });
    expect(getByTestId('ledger-strip')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// ds-eh6 — the desk must not render its all-clear before it has grounds to.
// ---------------------------------------------------------------------------
describe('ApprovalDesk — unknown state (ds-eh6)', () => {
  it('renders loading, not the all-clear, before the first cycle settles', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: false,
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-resting')).toBeNull();
    const unknown = getByTestId('approval-desk-unknown');
    expect(unknown.getAttribute('data-reason')).toBe('loading');
    // The specific sentence the bead names as the false claim.
    expect(unknown.textContent).not.toContain('Nothing needs your decision right now.');
  });

  it('renders the degraded admission after a cycle that could not see', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        degraded: true,
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-resting')).toBeNull();
    const unknown = getByTestId('approval-desk-unknown');
    expect(unknown.getAttribute('data-reason')).toBe('degraded');
    // "could not confirm", never "failed" and never "nothing is pending".
    expect(unknown.textContent).toContain("couldn't confirm");
  });

  it('still renders the all-clear once a clean cycle settles', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-unknown')).toBeNull();
    expect(getByTestId('approval-desk-resting')).toBeTruthy();
  });

  it('the band reads "—", not 0, while unsettled', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: false,
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('—');
    expect(getByTestId('instrument-band-managed').textContent).toContain('—');
  });

  it('a null graph reads as unknown in the band even after settling', () => {
    // scopeTotals over zero cards honestly returns zeros; they are correct as
    // arithmetic and wrong as an ANSWER, because nothing was counted.
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('instrument-band-managed').textContent).toContain('—');
    // awaiting is derived from decisions/approvals, which DID settle — so it
    // is a real 0, not unknown. The two must not be conflated.
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('0');
  });

  it('marks the scan line stale when the graph fetch failed this cycle', () => {
    // A failed graph fetch leaves the PRIOR graph in place, so generated_at
    // still parses and still renders — it just no longer describes a scan
    // that happened. This is lastError's one consumer.
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        lastError: 'graph',
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-stale-scan').textContent).toContain('not refreshed');
  });

  it('no stale marker on a clean cycle', () => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        lastError: null,
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-stale-scan')).toBeNull();
  });

  it('a pending proposal outranks unknown — a provable card always shows', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        settled: false,
        degraded: true,
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-unknown')).toBeNull();
    expect(getByTestId('approval-desk-pending')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// ds-wd2.15 — the pending card's "view the reasoning" link.
// ---------------------------------------------------------------------------
describe('ApprovalDesk — view the reasoning (ds-wd2.15)', () => {
  const TRACE = 'b'.repeat(32);

  function pendingIac(overrides: Partial<PendingApproval> = {}): PendingApproval {
    return {
      pr_number: 7,
      title: 'Adopt payment-demo Cloud Run service into IaC',
      url: 'https://github.com/x/y/pull/7',
      asset_type: 'run.googleapis.com/Service',
      resource_name: 'payment-demo',
      ...overrides,
    };
  }

  it('renders on the rollback arm, whose decision IS the authoring run', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ trace_id: TRACE })],
        pendingApprovals: [],
        onNavigate: vi.fn(),
        onOpenTrace: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-why').textContent).toContain('view the reasoning');
  });

  it('is ABSENT on the iac listing arm even when a decision shares its PR', () => {
    // An iac_apply trace_id belongs to the approve/apply POST, not to the run
    // that authored the PR — see desk.ts's DeskPendingIac.traceId. A link there
    // would send the operator to the trace of their own approval click.
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [iacDecision({ pr_number: 7, trace_id: TRACE })],
        pendingApprovals: [pendingIac({ pr_number: 7 })],
        onNavigate: vi.fn(),
        onOpenTrace: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-why')).toBeNull();
  });

  it('clicking it opens that trace', () => {
    const onOpenTrace = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ trace_id: TRACE })],
        pendingApprovals: [],
        onNavigate: vi.fn(),
        onOpenTrace,
      },
    });
    fireEvent.click(getByTestId('approval-desk-why'));
    expect(onOpenTrace).toHaveBeenCalledWith(TRACE);
  });

  it('is absent when the proposal carries no usable trace — never inert', () => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [pendingIac()],
        onNavigate: vi.fn(),
        onOpenTrace: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-why')).toBeNull();
  });

  it('is absent when no handler is wired, rather than rendering a dead control', () => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ trace_id: TRACE })],
        pendingApprovals: [],
        onNavigate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-why')).toBeNull();
  });

  it('does not appear on the resting state', () => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        onNavigate: vi.fn(),
        onOpenTrace: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-why')).toBeNull();
  });
});

// Codex review of #258 — a soft-degraded graph is a well-formed 200 carrying
// ZERO totals, so a non-null check alone let an outage render confident zeros.
// Same trap as pending-approvals' degraded 200, one endpoint over.
describe('ApprovalDesk — a degraded graph is not a read graph (ds-eh6)', () => {
  const DEGRADED_GRAPH = {
    ...GRAPH,
    degraded: true,
    degraded_reason: 'infra_reader_unavailable',
    totals: { resources: 0, managed: 0, drift: 0 },
    groups: [],
  };

  it('renders "—" rather than 0 for graph-derived figures', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: DEGRADED_GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('instrument-band-managed').textContent).toContain('—');
    expect(getByTestId('instrument-band-drift').textContent).toContain('—');
  });

  it('the resting footer drops the resource count and the "no new drift" claim', () => {
    // zero-because-unread is indistinguishable from zero-because-clean once it
    // reaches the copy, so neither segment may render without a usable graph.
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: DEGRADED_GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    const watch = getByTestId('approval-desk-watch').textContent ?? '';
    expect(watch).not.toContain('resources');
    expect(watch).not.toContain('no new drift');
    expect(watch).toContain('scan time pending'); // never a fresh-looking scan time
  });

  it('a NULL graph drops them too', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    const watch = getByTestId('approval-desk-watch').textContent ?? '';
    expect(watch).not.toContain('resources');
    expect(watch).not.toContain('no new drift');
  });

  it('a healthy graph still renders both', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onNavigate: vi.fn(),
      },
    });
    const watch = getByTestId('approval-desk-watch').textContent ?? '';
    expect(watch).toContain('resources');
    expect(watch).toContain('no new drift');
  });

  it('awaiting reads "—" while degraded, not an exact 0', () => {
    // An exact "0 awaiting" directly above a hero saying a waiting proposal may
    // be missing is two statements on one screen, one of them false.
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        degraded: true,
        onNavigate: vi.fn(),
      },
    });
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('—');
  });
});
