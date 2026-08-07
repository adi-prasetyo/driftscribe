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
  const pr = overrides.pr_number ?? 42;
  return {
    decision_id: 'iac-1',
    action: 'iac_apply',
    created_at: '2026-07-28T11:00:00Z',
    pr_number: pr,
    apply_status: 'waiting_for_rebake',
    // Every iac_apply doc the backend writes carries one — agent/main.py:7280,
    // :7331, :7461, :7503 all pass it — and it hashes `head_sha` (:6406), so it
    // names the GENERATION, not the PR. A fixture without one exercises a
    // document shape that does not occur, and used to let a PR-wide join look
    // safe. Default it per PR so same-PR rows are one generation, and set it
    // explicitly wherever a test needs two generations of the same PR.
    event_key: `iac-apply-${pr}-gen1`,
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
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onShowEstate: vi.fn() },
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
        onShowEstate: vi.fn(),
      },
    });
    // Settled with no usable scan TIME reads "unavailable"; "pending" is
    // reserved for a first cycle still in flight (Codex review of #258).
    expect(getByTestId('approval-desk-watch').textContent).toContain('scan time unavailable');
    expect(queryByText(/last scan/)).toBeNull();
  });

  it('omits the "no new drift" claim when there IS unresolved drift (never claims a false negative)', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: graphWithGroup(9, 6),
        decisions: [],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-watch').textContent).not.toContain('no new drift');
  });

  it('a null graph still renders resting with the scan-pending fallback, not a crash', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: null, decisions: [], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    // Settled with no usable scan TIME reads "unavailable"; "pending" is
    // reserved for a first cycle still in flight (Codex review of #258).
    expect(getByTestId('approval-desk-watch').textContent).toContain('scan time unavailable');
  });
});

// 2026-07-31 desk+estate merge — the calm states no longer have to carry the
// page alone (the estate section sits directly below), so a quiet one-line
// strip is the right size for "nothing needs you". This is LAYOUT, not
// semantics: the h2 stays, or the hero's heading outline would depend on which
// state is showing.
describe('ApprovalDesk — calm states are a slim strip (desk+estate merge)', () => {
  it('renders resting as a slim strip: headline and watch line share one row', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], settled: true, onShowEstate: vi.fn() },
    });
    const resting = getByTestId('approval-desk-resting');
    expect(resting.classList.contains('approval-desk__calm--slim')).toBe(true);
    expect(resting.querySelector('h2')).not.toBeNull(); // semantics preserved
    // The watch line and its segments are untouched by the slimming.
    expect(getByTestId('approval-desk-watch').textContent).toContain('735 resources');
  });

  it('renders the unknown pair as the same slim strip, data-reason intact', () => {
    for (const [reason, props] of [
      ['loading', { graph: null, settled: false }],
      ['degraded', { graph: GRAPH, settled: true, degraded: true }],
    ] as const) {
      const { getByTestId } = render(ApprovalDesk, {
        props: { decisions: [], pendingApprovals: [], onShowEstate: vi.fn(), ...props },
      });
      const unknown = getByTestId('approval-desk-unknown');
      expect(unknown.getAttribute('data-reason')).toBe(reason);
      expect(unknown.classList.contains('approval-desk__calm--slim')).toBe(true);
      expect(unknown.querySelector('h2')).not.toBeNull();
      cleanup();
    }
  });

  it('the tall states do NOT take the slim class', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        settled: true,
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-pending').classList.contains('approval-desk__calm--slim')).toBe(
      false,
    );
  });
});

describe('ApprovalDesk — heading level', () => {
  // The page's only h1 is the brand title in App.svelte, so the desk's state
  // headline must be h2: an h3 skips a level for anyone navigating by heading,
  // and EstateView's group headings — on the SAME page since the 2026-07-31
  // merge — are already h2. Easy to regress, because both
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
        props: { graph: GRAPH, decisions: s.decisions, pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: graphWithGroup(9, 6), decisions: [], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('0');
    cleanup();

    const { getByTestId: gt2 } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [rollbackDecision()], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [rb], pendingApprovals: [iac], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('rollback');
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('2');
  });

  it('managed/drift stat clicks call onShowEstate (scroll, not navigation)', async () => {
    const onShowEstate = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onShowEstate },
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    await fireEvent.click(getByTestId('instrument-band-drift'));
    // No argument: there is one destination now, and it is on this same page.
    expect(onShowEstate.mock.calls).toEqual([[], []]);
  });
});

// ds-7ag.2 — the band's own test can only prove `onStat` fired; it cannot catch
// THIS consumer mapping awaiting back to the estate, which is exactly the bug
// (the approval queue is on the desk, so the numeral that says "you have work"
// used to walk away from it).
describe('ApprovalDesk — awaiting band stat is a figure, not a control', () => {
  beforeEach(() => {
    // jsdom has no layout, so it implements no scrollIntoView. Stubbed so that
    // if a regression ever reintroduces a scroll, this suite records the call
    // rather than dying on an undefined method.
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  // ds-s61. ds-7ag.2 made this numeral scroll + focus the pending card, which
  // was the right instinct aimed at the wrong distance: the card is ~270px below
  // the numeral on the same screen, so scrollIntoView({block:'start'}) asked for
  // a journey the page had no room to make and merely spent the 38px of dead
  // scroll a stale `calc(100vh - 56px)` left behind. The operator saw the whole
  // page twitch and the card not arrive.
  it('does not scroll, focus, or show the estate — the card is already on screen', async () => {
    const onShowEstate = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        onShowEstate,
      },
    });
    const pending = getByTestId('approval-desk-pending');
    const awaiting = getByTestId('instrument-band-awaiting');

    expect(awaiting.tagName).not.toBe('BUTTON');
    await fireEvent.click(awaiting);

    expect(onShowEstate).not.toHaveBeenCalled();
    expect(window.HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
    expect(document.activeElement).not.toBe(pending);
  });

  // The two attributes existed ONLY to serve that jump. A focusable div with no
  // focus styling and nothing aimed at it is dead weight that a future reader
  // would have to reverse-engineer.
  it('the pending card no longer carries the jump-target id or tabindex', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [rollbackDecision()], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    const pending = getByTestId('approval-desk-pending');
    expect(pending.getAttribute('tabindex')).toBeNull();
    expect(pending.getAttribute('id')).toBeNull();
  });

  // managed/drift are still controls here, and they still reach the estate —
  // now the section further down THIS page — so making awaiting inert must not
  // have flattened the whole band.
  it('managed and drift still reach the estate', async () => {
    const onShowEstate = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [rollbackDecision()], pendingApprovals: [], onShowEstate },
    });
    await fireEvent.click(getByTestId('instrument-band-managed'));
    expect(onShowEstate).toHaveBeenCalledTimes(1);
    await fireEvent.click(getByTestId('instrument-band-drift'));
    expect(onShowEstate).toHaveBeenCalledTimes(2);
  });

  it('with nothing pending, the awaiting stat is still inert', async () => {
    const onShowEstate = vi.fn();
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [], onShowEstate },
    });
    expect(queryByTestId('approval-desk-pending')).toBeNull();
    const awaiting = getByTestId('instrument-band-awaiting');
    expect(awaiting.tagName).not.toBe('BUTTON');
    await fireEvent.click(awaiting);
    expect(onShowEstate).not.toHaveBeenCalled();
  });
});

describe('ApprovalDesk — pending state, rollback source', () => {
  it('renders the Anchor who-line, the diff table, and one CTA pointing at the safe href', () => {
    const d = rollbackDecision();
    const { getByTestId, queryByTestId, getByText } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    const pending = getByTestId('approval-desk-pending');
    expect(pending.getAttribute('data-source')).toBe('rollback');
    expect(getByText('Anchor is proposing a fix')).toBeTruthy();
    expect(getByTestId('drift-diff-card')).toBeTruthy();
    const review = getByTestId('approval-desk-review') as HTMLAnchorElement;
    expect(review.getAttribute('href')).toBe('/approvals/rb-1?t=abc');
    expect(review.getAttribute('target')).toBe('_blank');
    expect(review.getAttribute('rel')).toBe('noopener');
    // The rollback lane took the SAME collapse as the IaC lane: its old Reject
    // anchor carried this identical href and could not reject anything either.
    // Counting the controls is the guard that survives — querying the deleted
    // testid would pass no matter what this arm rendered.
    expect(getByTestId('approval-desk-acts').children).toHaveLength(1);
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').textContent).toMatch(/2026|09:15/);
  });
});

describe('ApprovalDesk — pending state, iac source, both provenance arms', () => {
  it('listing arm: renders the PR title from the open-PR payload', () => {
    const approval = pendingIac({ title: 'Adopt orders-sub into IaC' });
    const { getByTestId, getByText } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [approval], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('iac');
    expect(getByText('Adopt orders-sub into IaC')).toBeTruthy();
    expect(getByText('PR #7')).toBeTruthy();
  });

  // The ds-db0 split must not over-reach. A LISTING-provenance PR is one the
  // open-PR listing can still see, i.e. genuinely unmerged and genuinely
  // unapproved — "waiting for your approval" is TRUE there and must survive.
  it('listing arm (no title): keeps the approval-request copy, which is true for an unmerged PR', () => {
    const approval = pendingIac({ title: undefined });
    const { getByTestId, getByText } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [approval], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('iac');
    expect(getByText('Infrastructure change PR #7 is waiting for your approval.')).toBeTruthy();
    expect(getByTestId('approval-desk-pending').textContent).not.toContain('waiting to be applied');
  });

  // Codex review of ds-db0: the provenance split alone does NOT close the hole.
  // selectPendingIac (listing) is tried first and returns immediately
  // (desk.ts:627), the open-PR listing is cached up to 60s, and it is filtered
  // only for applied+merged (approval.ts:337) — so for a minute after the
  // approve click a stale listing row beats the decision row and used to
  // re-ask for the approval just given. That minute is exactly the frame the
  // approve→stamp beat is recorded on. Passing BOTH sources is the point of
  // this test; the earlier decision-arm test used `decisions: []` and could
  // never have caught it.
  it('stale cached listing + a decision proving approval: never re-asks for that approval', () => {
    const approval = pendingIac({ title: undefined }); // PR #7, still "open" per cache
    const approved = iacDecision({
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
    });
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [approved],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    const pending = getByTestId('approval-desk-pending').textContent ?? '';
    expect(pending).not.toContain('waiting for your approval');
    expect(pending).not.toContain('waiting for your review');
    expect(pending).toContain('approved');
  });

  // The negative twin (Codex). The proof-of-approval join is PR-WIDE and the
  // listing DTO carries no generation identity, so it must accept ONLY
  // evidence that cannot be misattributed across generations. After
  // `generation A applied → merge FAILED → head advances → generation B
  // unapproved`, the listing legitimately points at B while A's terminal
  // decision still names the same PR. Vouching for B there would tell the
  // operator they had approved something they had not — the dangerous
  // direction, and the ds-0rm mistake (desk.ts:620) repeated one layer up.
  it('a terminal decision whose merge FAILED never vouches for a newer generation', () => {
    const approval = pendingIac({ title: undefined }); // PR #7, genuinely open generation B
    const failedMerge = iacDecision({
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'applied',
      merge_state: 'pending', // merge never landed → cannot prove B was approved
    });
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [failedMerge],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    const pending = getByTestId('approval-desk-pending').textContent ?? '';
    expect(pending).toContain('waiting for your approval');
    expect(pending).not.toContain('approved and waiting to be applied');
  });

  // The OTHER way the witness can lie (Codex r3). Decision docs accumulate —
  // the waiting_for_rebake+merged row is never rewritten, so it is still there
  // after the resume-apply appends a terminal failure (agent/main.py:7452). A
  // stale listing still naming the PR (the pending fetch can fail while the
  // decisions refresh, overviewStore.ts:237) then produced "approved and
  // waiting to be applied" over an apply that had terminally FROZEN — a runbook
  // condition dressed up as ordinary pending work. The witness must be
  // uncontradicted, not merely present. Both docs are supplied here precisely
  // because the earlier tests supply only one.
  it.each(['failed', 'failed_state_suspect', 'ambiguous'])(
    'a later %s decision withdraws the "waiting to be applied" claim',
    (terminalStatus) => {
      const approval = pendingIac({ title: undefined }); // PR #7, stale listing row
      const approved = iacDecision({
        decision_id: 'iac-witness',
        pr_number: 7,
        pr_title: undefined,
        apply_status: 'waiting_for_rebake',
        merge_state: 'merged', // the witness — still on file, never rewritten
        created_at: '2026-07-28T11:00:00Z',
      });
      const frozen = iacDecision({
        decision_id: 'iac-frozen',
        pr_number: 7,
        pr_title: undefined,
        apply_status: terminalStatus,
        merge_state: 'merged',
        created_at: '2026-07-28T11:06:00Z',
      });
      const { getByTestId } = render(ApprovalDesk, {
        props: {
          graph: GRAPH,
          decisions: [frozen, approved],
          pendingApprovals: [approval],
          onShowEstate: vi.fn(),
        },
      });
      const pending = getByTestId('approval-desk-pending').textContent ?? '';
      expect(pending).not.toContain('approved and waiting to be applied');
    },
  );

  // ds-db0: the decision arm exists only for a PR the open-PR listing can no
  // longer see BECAUSE IT ALREADY MERGED (desk.ts:78-84), so its copy must say
  // "waiting to be applied". Claiming it awaits approval contradicts the
  // operator's own click on the very frame the approve→stamp beat lands.
  it('decision arm (no PR title carried): says the merged change awaits APPLYING, never approval', () => {
    const d = iacDecision({ pr_number: 99, pr_title: undefined });
    const { getByTestId, getByText, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-pending').getAttribute('data-source')).toBe('iac');
    expect(
      getByText('Infrastructure change PR #99 is approved and waiting to be applied.'),
    ).toBeTruthy();
    // Pins the claim, not just the string: nothing in this arm may ask the
    // operator for an approval they have already given.
    const pending = queryByTestId('approval-desk-pending')?.textContent ?? '';
    expect(pending).not.toContain('waiting for your approval');
    expect(pending).not.toContain('waiting for your review');
    // No fabricated "proposed at" time for a decision whose real created_at
    // we DO have is fine, but there must be no invented PR title:
    expect(pending).not.toContain('undefined');
  });

  it('decision arm WITH a carried pr_title renders it honestly instead of the fallback', () => {
    const d = iacDecision({ pr_number: 55, pr_title: 'Adopt lodash upgrade' });
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    // "Adopt lodash upgrade" legitimately appears twice (the desk h3 AND the
    // ledger strip's subtitle for the same decision) — scope to the pending
    // card so this asserts the DESK's own headline, not just that the string
    // appears somewhere on the page.
    const pending = within(getByTestId('approval-desk-pending'));
    expect(pending.getByText('Adopt lodash upgrade')).toBeTruthy();
    expect(pending.queryByText(/is waiting for your approval\.$/)).toBeNull();
  });

  // ds-22k. The byline fix left a card that said "approved" directly above a
  // button saying "Approve this proposal" and a Reject that could not un-merge
  // a merged PR — a control whose label is a promise it cannot keep, on the
  // human-in-the-loop surface of all places. The Reject anchor is now gone from
  // EVERY arm (it only ever navigated), so asserting its absence here would be
  // vacuous; the live guard is that this arm shows the apply and nothing else.
  it('an already-approved change offers the APPLY, and solicits nothing', () => {
    const d = iacDecision({ pr_number: 99, pr_title: undefined, merge_state: 'merged' });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    const apply = getByTestId('approval-desk-apply') as HTMLAnchorElement;
    expect(apply.textContent?.trim()).toBe('Apply this change');
    // The action is REAL — the approval page keeps a live form for
    // waiting_for_rebake (agent/main.py:6127) and the POST resumes the apply
    // (:7187). Only the name was wrong, so the destination must be untouched.
    expect(apply.getAttribute('href')).toContain('/iac-approvals/99');
    expect(apply.getAttribute('target')).toBe('_blank');
    expect(apply.getAttribute('rel')).toBe('noopener');
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(getByTestId('approval-desk-acts').children).toHaveLength(1);
    const pending = queryByTestId('approval-desk-pending')?.textContent ?? '';
    expect(pending).not.toContain('Review this proposal');
  });

  it('an approved pre-merge change offers CONTINUE, never Apply or a second approval', () => {
    const d = iacDecision({
      pr_number: 98,
      pr_title: undefined,
      merge_state: 'pending',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });

    expect(getByTestId('approval-desk-continue').textContent?.trim()).toBe(
      'Continue this change',
    );
    expect(queryByTestId('approval-desk-apply')).toBeNull();
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(getByTestId('approval-desk-acts').children).toHaveLength(1);
  });

  // The CTA must key off the SAME discriminator as the byline. This is the
  // stale-cache frame again (the listing arm wins for up to 60s after the
  // approve click): a card whose byline reads "approved" over a button that
  // still opens the first-approval gate is the two-surfaces-disagree bug ds-db0
  // was.
  it('stale cached listing + proof of approval: the CTA follows the byline', () => {
    const approval = pendingIac({ title: undefined }); // PR #7, still "open" per cache
    const approved = iacDecision({
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [approved],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-apply')).toBeTruthy();
    expect(queryByTestId('approval-desk-review')).toBeNull();
  });

  // The other direction, and the one that would matter most if it broke: a
  // genuinely unapproved change must keep its full approval path. Losing the
  // gate is far worse than labelling it awkwardly. What "the full path" means
  // is now ONE link to the HMAC-gated page — that page owns both Approve and
  // Reject, and the desk never did.
  it('a genuinely unapproved listing row keeps the gate, as one review link', () => {
    const approval = pendingIac({ title: 'Adopt orders-sub into IaC' });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [], pendingApprovals: [approval], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-review').textContent?.trim()).toBe('Review this proposal');
    expect(queryByTestId('approval-desk-apply')).toBeNull();
    // The anti-regression guard: a second control here could only be a second
    // DESTINATION, never a second verb against the same href.
    expect(getByTestId('approval-desk-acts').children).toHaveLength(1);
  });

  // ds-22k's acceptance criterion, and the reason it was filed as one bead
  // rather than three: the byline fix alone left a card saying "approved" over a
  // button saying "Approve this proposal", under a band counting it as
  // "Awaiting your approval". Asserting each surface separately is what let them
  // drift apart in the first place, so this pins the whole rendered desk at
  // once — any NEW surface that starts soliciting the approval fails here
  // without anyone remembering to add it to a list.
  it('no surface of an already-approved desk solicits the approval already given', () => {
    const d = iacDecision({ pr_number: 99, pr_title: undefined });
    const { container } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    const text = container.textContent ?? '';
    expect(text).not.toMatch(/awaiting your approval/i);
    expect(text).not.toMatch(/waiting for your (approval|review)/i);
    // Tracks the CURRENT first-approval copy. It used to read "approve this
    // proposal"; that string left the codebase with `approveCta`, and a regex
    // for text no render path can emit is a tooth this net no longer has.
    expect(text).not.toMatch(/review this proposal/i);
    // The aria layer too — a screen reader must not hear the solicitation the
    // visible layer stopped making.
    for (const el of Array.from(container.querySelectorAll('[aria-label]'))) {
      expect(el.getAttribute('aria-label')).not.toMatch(/awaiting your approval/i);
    }
    // NOT asserted absent: "Needs your decision" (the band) and "Approved ·
    // awaiting apply" (the ledger). Both are true — the apply is a real
    // outstanding operator step — and demanding the desk fall silent about it
    // would trade an over-claim for an under-claim.
    expect(text).toContain('Needs your decision');
  });

  // A terminal failure means the approval page suppresses its form. A stale
  // listing must therefore become view-only, not re-offer Approve/Reject for a
  // credential whose run has already ended.
  it('a contradicted witness offers only View failure details', () => {
    const approval = pendingIac({ title: undefined });
    const approved = iacDecision({
      decision_id: 'iac-witness',
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      created_at: '2026-07-28T11:00:00Z',
    });
    const frozen = iacDecision({
      decision_id: 'iac-frozen',
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'failed_state_suspect',
      merge_state: 'merged',
      created_at: '2026-07-28T11:06:00Z',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [frozen, approved],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-view-failure').textContent?.trim()).toBe(
      'View failure details',
    );
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(queryByTestId('approval-desk-apply')).toBeNull();
    expect(queryByTestId('approval-desk-continue')).toBeNull();
  });

  // The witness proves the PR MERGED; it does not prove the other rows under
  // that pr_number are the same generation. An apply that fails never merges, so
  // the PR stays open, the operator pushes a fix, and generation B merges beside
  // generation A's terminal failure. Joining PR-wide let A outrank B: the card
  // said "View failure details" while the approval page resolved B, kept its
  // form, and the rail said "Apply this change" — a missed live control AND the
  // card/rail drift the shared discriminator exists to prevent.
  it('a failure from an OLDER generation cannot speak for the merged one', () => {
    const approval = pendingIac({ title: undefined });
    const failedGenerationA = iacDecision({
      decision_id: 'iac-gen-a',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-gen0', // different head_sha => different generation
      apply_status: 'failed_state_suspect',
      merge_state: 'n/a', // it failed BEFORE the merge, which is why B exists
      created_at: '2026-07-28T11:00:00Z',
    });
    const mergedGenerationB = iacDecision({
      decision_id: 'iac-gen-b',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-gen1',
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      created_at: '2026-07-28T11:20:00Z',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [mergedGenerationB, failedGenerationA],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-apply')).toBeTruthy();
    expect(queryByTestId('approval-desk-view-failure')).toBeNull();
  });

  // Two merged witnesses = two generations merged this PR, which a same-head C2
  // rerun makes reachable (distinct event_key, independent approval claims, and
  // merge_pr_at_sha calls "already merged at the expected head" a success —
  // driftscribe_lib/github.py:1052 — so the later POST records a witness too).
  // The payload cannot say which generation the page will resolve, so picking
  // one would let a frozen apply read as live. Fall back to the gate.
  it('two merged witnesses license nothing — the card cannot pick a generation', () => {
    const approval = pendingIac({ title: undefined });
    const witnessA = iacDecision({
      decision_id: 'iac-a-merged',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-genA',
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      created_at: '2026-07-28T11:00:00Z',
    });
    const witnessB = iacDecision({
      decision_id: 'iac-b-merged',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-genB',
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      created_at: '2026-07-28T11:02:00Z',
    });
    // B is the generation the approval page resolves, and it is FROZEN. Scoping
    // to A (the first match) would have offered "Apply this change" for it.
    const frozenB = iacDecision({
      decision_id: 'iac-b-frozen',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-genB',
      apply_status: 'failed_state_suspect',
      merge_state: 'merged',
      created_at: '2026-07-28T11:09:00Z',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [frozenB, witnessB, witnessA],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-review')).toBeTruthy();
    expect(queryByTestId('approval-desk-apply')).toBeNull();
  });

  // The witness is the only thing that licenses reading sibling rows, so a
  // witness with no generation identity licenses nothing. Fail toward the gate,
  // exactly as the no-witness branch does — the page keeps a live form for
  // waiting_for_rebake (agent/main.py:6127), so the control still works.
  it('a witness with no event_key cannot license a PR-wide join', () => {
    const approval = pendingIac({ title: undefined });
    const frozen = iacDecision({
      decision_id: 'iac-frozen',
      pr_number: 7,
      pr_title: undefined,
      event_key: 'iac-apply-7-gen0',
      apply_status: 'failed_state_suspect',
      merge_state: 'merged',
    });
    const keylessWitness = iacDecision({
      decision_id: 'iac-witness',
      pr_number: 7,
      pr_title: undefined,
      event_key: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [keylessWitness, frozen],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-review')).toBeTruthy();
    expect(queryByTestId('approval-desk-view-failure')).toBeNull();
  });

  // The gate-safety twin of the two view-only tests above. Both of those supply
  // a `waiting_for_rebake`+`merged` witness, which PROVES the PR merged and the
  // listing is stale. Without that proof the same PR-wide join reaches across
  // generations: an apply that fails never merges, so the PR STAYS OPEN, the
  // operator pushes a fix, and the listing now points at a genuinely unapproved
  // generation B — whose card would offer nothing but "View failure details"
  // because generation A failed. A missed gate is the worst outcome this
  // component has; a redundant Approve on a formless page is merely annoying,
  // and the page itself tells the truth when they get there.
  it.each(['failed', 'failed_state_suspect', 'ambiguous'])(
    'a %s generation never removes the approval gate from a still-open PR',
    (terminalStatus) => {
      const approval = pendingIac({ title: undefined }); // PR #7, genuinely open (gen B)
      const failedGenerationA = iacDecision({
        decision_id: 'iac-gen-a',
        pr_number: 7,
        pr_title: undefined,
        apply_status: terminalStatus,
        merge_state: 'n/a', // never merged — which is WHY the PR is still open
      });
      const { getByTestId, queryByTestId } = render(ApprovalDesk, {
        props: {
          graph: GRAPH,
          decisions: [failedGenerationA],
          pendingApprovals: [approval],
          onShowEstate: vi.fn(),
        },
      });
      expect(getByTestId('approval-desk-review')).toBeTruthy();
      expect(queryByTestId('approval-desk-view-failure')).toBeNull();
    },
  );

  // Same shape for supersession: `superseded_by_pr` is written on a
  // waiting_for_rebake row, which can be PRE-merge, so it is not proof the PR
  // itself is settled either.
  it('an unmerged superseded generation never removes the gate either', () => {
    const approval = pendingIac({ title: undefined });
    const superseded = iacDecision({
      decision_id: 'iac-sup-unmerged',
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'pending',
      superseded_by_pr: 101,
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [superseded],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-review')).toBeTruthy();
    expect(queryByTestId('approval-desk-view')).toBeNull();
  });

  it('a stale listing for an explicitly superseded approval is view-only', () => {
    const approval = pendingIac({ title: undefined });
    const superseded = iacDecision({
      decision_id: 'iac-superseded',
      pr_number: 7,
      pr_title: undefined,
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
      superseded_by_pr: 101,
    });
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [superseded],
        pendingApprovals: [approval],
        onShowEstate: vi.fn(),
      },
    });

    expect(getByTestId('approval-desk-view').textContent?.trim()).toBe(
      'View approval details',
    );
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(queryByTestId('approval-desk-apply')).toBeNull();
    expect(queryByTestId('approval-desk-continue')).toBeNull();
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
    onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn() },
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
      props: { graph: GRAPH, decisions: [first], pendingApprovals: [], onShowEstate: vi.fn() },
    });
    expect(getByTestId('approval-desk-stamped').getAttribute('data-source')).toBe('iac');

    // A fresh rollback resolves 5 minutes later — still within the first
    // stamp's window, so it takes over per deskModel's recency tiebreak.
    vi.setSystemTime(Date.parse('2026-07-28T12:05:00Z'));
    const second = rollbackDecision({
      decision_id: 'rb-second',
      approval: { approval_url: '/approvals/rb-second?t=x', status: 'used', phase: 'applied', resolved_at: '2026-07-28T12:05:00Z' },
    });
    await rerender({ graph: GRAPH, decisions: [first, second], pendingApprovals: [], onShowEstate: vi.fn() });
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
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn(), refresh },
    });
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh).not.toHaveBeenCalled();
  });

  it('a CTA click, then a return focus, fires a bounded burst of refreshes', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-review'));
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

  it('the STALE view link arms the ladder too (ds-jk9)', async () => {
    // Codex round 4 of #290. The stale card replaces four live CTAs, all of
    // which armed the return ladder, with one view-only link — and the first
    // version of that link dropped the arming, so an operator who acted on the
    // approval page came back to a desk that would not converge until the 45s
    // poll. The one branch-local regression this PR introduced.
    //
    // The stale card arguably needs it MORE than the live ones: it is shown
    // precisely because the desk could not refresh, and the operator clicking
    // through is the one most likely to resolve the item while away.
    const refresh = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
        refresh,
        decisionsStale: true,
        degraded: true,
      },
    });
    await fireEvent.click(getByTestId('approval-desk-view-stale'));
    expect(refresh).not.toHaveBeenCalled(); // arming alone does nothing yet

    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh.mock.calls.length).toBeGreaterThan(1);
  });

  it('cannot stack: leaving and returning again mid-ladder does not start a second ladder', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-review'));
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
        onShowEstate: vi.fn(),
        refresh,
      },
    });
    await fireEvent.click(getByTestId('approval-desk-review'));
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
        onShowEstate: vi.fn(),
        refresh,
      },
    });

    await fireEvent.click(getByTestId('approval-desk-review'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000); // drain the first ladder fully
    const afterFirst = refresh.mock.calls.length;
    expect(afterFirst).toBe(3);

    // A fresh CTA click + return focus — the operator approving a second thing.
    await fireEvent.click(getByTestId('approval-desk-review'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(20_000);
    expect(refresh.mock.calls.length).toBe(afterFirst * 2);
  });

  it('unmounting clears any pending ladder timers (no callback after teardown)', async () => {
    const refresh = vi.fn();
    const d = rollbackDecision();
    const { getByTestId, unmount } = render(ApprovalDesk, {
      props: { graph: GRAPH, decisions: [d], pendingApprovals: [], onShowEstate: vi.fn(), refresh },
    });
    await fireEvent.click(getByTestId('approval-desk-review'));
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(0);
    const before = refresh.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(refresh.mock.calls.length).toBe(before);
  });
});

// ds-3em: the "ledger strip composition" suite that stood here asserted the
// desk MOUNTS the strip. It no longer does — App mounts it as a sibling card,
// because the desk card's border was what made an estate-wide ledger read as
// the pending proposal's history. The replacement claim (outside the desk,
// above the estate) can only be made where both are on screen, so it lives in
// App.test.ts's ds-jns desk-records suite.

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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-unknown')).toBeNull();
    expect(getByTestId('approval-desk-pending')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// ds-wd2.15 — the pending card's "view the reasoning" link.
//
// ds-jns re-pointed it: it used to call onOpenTrace, which switched to the chat
// view and replayed the trace there. It now calls onRecordChange, and the
// record opens on this same page in the ledger row the hero's decision already
// has. The gating rules below are unchanged — the handler being optional, the
// link never rendering inert — only the destination moved.
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
        onShowEstate: vi.fn(),
        onRecordChange: vi.fn(),
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
        onShowEstate: vi.fn(),
        onRecordChange: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-why')).toBeNull();
  });

  it('clicking it asks App to open that record — it does not leave the desk', () => {
    const onRecordChange = vi.fn();
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ trace_id: TRACE })],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
        onRecordChange,
      },
    });
    fireEvent.click(getByTestId('approval-desk-why'));
    expect(onRecordChange).toHaveBeenCalledWith(TRACE);
  });

  it('is absent when the proposal carries no usable trace — never inert', () => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [],
        pendingApprovals: [pendingIac()],
        onShowEstate: vi.fn(),
        onRecordChange: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
        onRecordChange: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
      },
    });
    const watch = getByTestId('approval-desk-watch').textContent ?? '';
    expect(watch).not.toContain('resources');
    expect(watch).not.toContain('no new drift');
    expect(watch).toContain('scan time unavailable'); // never a fresh-looking scan time
  });

  it('a NULL graph drops them too', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
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
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('instrument-band-awaiting').textContent).toContain('—');
  });
});

// Codex re-review of #258 — a FINISHED graph failure was still described as
// "scan time pending" (JA 走査時刻 取得中, literally "acquiring scan time"),
// which promises something in flight and would sit there until the next poll.
describe('ApprovalDesk — settled graph failure is not "pending" (ds-eh6)', () => {
  it('reads pending only while the first cycle is still out', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: false,
        onShowEstate: vi.fn(),
      },
    });
    // Unsettled renders the unknown hero, so the watch line lives there.
    expect(getByTestId('approval-desk-unknown')).toBeTruthy();
  });

  it('reads unavailable once the cycle has settled without a graph', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: null,
        decisions: [],
        pendingApprovals: [],
        settled: true,
        onShowEstate: vi.fn(),
      },
    });
    const watch = getByTestId('approval-desk-watch').textContent ?? '';
    expect(watch).toContain('scan time unavailable');
    expect(watch).not.toContain('scan time pending');
  });
});

describe('ApprovalDesk — undelivered-notification notice (ds-hdt)', () => {
  it('tells the operator when nothing was sent, so silence is not mistaken for "nothing needed me"', () => {
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ notify: { state: 'failed', error_code: 'worker_error', status_code: 503 } })],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
      },
    });
    expect(getByTestId('approval-desk-notify-failed').textContent).toContain(
      'No notification could be sent',
    );
    // The proposal itself is UNAFFECTED — the notice is a footnote on a card
    // that still carries its full approval path. That is the whole point of
    // ds-hdt: delivery is advisory, the row is the surface.
    expect(getByTestId('approval-desk-review')).toBeTruthy();
  });

  it.each([
    ['delivered', { state: 'delivered' as const }],
    ['pending (not yet known)', { state: 'pending' as const }],
  ])('stays silent when notify is %s', (_label, notify) => {
    const { queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision({ notify })],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
      },
    });
    expect(queryByTestId('approval-desk-notify-failed')).toBeNull();
  });

  it('stays silent on a pre-ds-hdt row that carries no notify field at all', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        graph: GRAPH,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        onShowEstate: vi.fn(),
      },
    });
    // The card renders; only the notice is absent. Warning here would fire on
    // every historical decision in the log.
    expect(getByTestId('approval-desk-pending')).toBeTruthy();
    expect(queryByTestId('approval-desk-notify-failed')).toBeNull();
  });
});

// ds-jk9 / ds-smr. deskModel stamps `stale` when the lane that produced the
// card did not refresh (desk.test.ts covers WHICH lane stamps WHICH rule). This
// block covers what the component owes a stale card: keep every piece of
// identity, drop the live CTA, and drop every present-tense claim about the
// item's current state.
//
// Suppressing only the button would be the half-fix: every byline and headline
// fallback on this card is written in the present tense, so a silenced Approve
// under "An infrastructure change is waiting for your review" still tells the
// operator something the app just failed to establish.
describe('ApprovalDesk — a stale lane keeps identity and drops the verdict (ds-jk9)', () => {
  const base = { graph: GRAPH, onShowEstate: vi.fn() };

  it('rollback: identity survives, CTA and byline do not', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [rollbackDecision()],
        pendingApprovals: [],
        decisionsStale: true,
        degraded: true,
      },
    });
    const card = getByTestId('approval-desk-pending');
    // Identity: the card is still here, still carries its diff evidence.
    expect(card.getAttribute('data-source')).toBe('rollback');
    expect(card.textContent).toContain('LOG_LEVEL');
    // Verdict: gone.
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(getByTestId('approval-desk-view-stale')).toBeTruthy();
    expect(getByTestId('approval-desk-stale-notice')).toBeTruthy();
    // The rollback byline reaches the template through its OWN ternary, not
    // through pendingWhoKey — so a stale arm added only to that helper would
    // miss every rollback card. This assertion is what catches that.
    expect(card.textContent).not.toMatch(/is proposing|waiting for your decision/i);
  });

  it('iac listing: identity survives, CTA and byline do not', () => {
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [],
        pendingApprovals: [pendingIac()],
        approvalsStale: true,
        degraded: true,
      },
    });
    const card = getByTestId('approval-desk-pending');
    // Identity: the PR title and number are facts about WHICH item this is.
    expect(card.textContent).toContain('Adopt orders-sub into IaC');
    expect(card.textContent).toContain('PR #7');
    expect(queryByTestId('approval-desk-review')).toBeNull();
    expect(getByTestId('approval-desk-view-stale')).toBeTruthy();
  });

  it('drops every present-tense variant, not just the ones the first draft listed', () => {
    // A presence-only assertion blesses whatever copy it finds — twice over in
    // #289. Assert the CLAIM. All six asserting strings on this card are
    // covered, including iacMerged's "is approved and waiting to be applied",
    // which an earlier regex here missed entirely.
    const merged = iacDecision({ apply_status: 'waiting_for_rebake', merge_state: 'merged' });
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [merged],
        pendingApprovals: [],
        decisionsStale: true,
        degraded: true,
      },
    });
    const card = getByTestId('approval-desk-pending');
    expect(card.textContent).not.toMatch(
      /waiting for your|waiting to be applied|needs attention|is proposing|is approved/i,
    );
    // and the positive: it says what it DID see, in the past tense.
    expect(card.textContent).toMatch(/last (seen|checked)/i);
  });

  it('suppresses the notify-failed notice, which is itself a waiting claim', () => {
    // "it has been waiting here unannounced" asserts the item is still waiting.
    // It renders on its own path, independent of the CTA, so silencing the
    // button does not silence it.
    const d = rollbackDecision({
      notify: { state: 'failed', error_code: 'worker_error', status_code: 503 },
    });

    // ASSERT THE FIXTURE'S PREMISE FIRST. The initial version of this test used
    // a `notify_status` field that does not exist, so the notice never rendered
    // and the test passed against the unfixed component — a suppression test
    // whose subject was already absent proves nothing.
    const fresh = render(ApprovalDesk, {
      props: { ...base, decisions: [d], pendingApprovals: [] },
    });
    expect(fresh.getByTestId('approval-desk-notify-failed')).toBeTruthy();
    cleanup();

    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [d],
        pendingApprovals: [],
        decisionsStale: true,
        degraded: true,
      },
    });
    expect(getByTestId('approval-desk-pending')).toBeTruthy();
    expect(queryByTestId('approval-desk-notify-failed')).toBeNull();
  });

  it('a FRESH card is completely unchanged', () => {
    // The negative that keeps this from being a blanket downgrade: with both
    // lanes good the desk must still make its full, live claim.
    const { getByTestId, queryByTestId } = render(ApprovalDesk, {
      props: { ...base, decisions: [rollbackDecision()], pendingApprovals: [] },
    });
    expect(getByTestId('approval-desk-review')).toBeTruthy();
    expect(queryByTestId('approval-desk-view-stale')).toBeNull();
    expect(queryByTestId('approval-desk-stale-notice')).toBeNull();
    expect(getByTestId('approval-desk-pending').textContent).toContain('Anchor is proposing a fix');
  });

  function unresolved(phase: 'failed' | 'outcome_unknown'): Decision {
    return rollbackDecision({
      approval: { approval_url: '/approvals/rb-1?t=x', status: 'used', phase },
    });
  }

  it('a stale outcome_unknown drops its live claim, not just its notice', () => {
    // The case that makes rule 2.5 a blocker rather than a nicety.
    // outcome_unknown is NONTERMINAL: /reconcile can promote the same attempt
    // to applied or failed. So "the outcome IS unconfirmed" and "we cannot
    // confirm it either way" are live claims a retained list cannot support.
    //
    // An earlier version of this test used only `phase: 'failed'` and asserted
    // that the notice EXISTED. It passed while both sentences above went on
    // asserting — a presence assertion blessing the copy it was meant to police,
    // for the second time on this branch.
    const fresh = render(ApprovalDesk, {
      props: { ...base, decisions: [unresolved('outcome_unknown')], pendingApprovals: [] },
    });
    // Premise: the live claim really is there when the lane is good.
    expect(fresh.getByTestId('approval-desk-unresolved').textContent).toMatch(
      /outcome is unconfirmed/i,
    );
    cleanup();

    const { getByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [unresolved('outcome_unknown')],
        pendingApprovals: [],
        decisionsStale: true,
        degraded: true,
      },
    });
    const card = getByTestId('approval-desk-unresolved');
    expect(card.getAttribute('data-phase')).toBe('outcome_unknown');
    expect(card.textContent).not.toMatch(/outcome is unconfirmed|cannot confirm it either way/i);
    expect(card.textContent).toMatch(/was unconfirmed when this was last checked/i);

    // The DETAIL chip, asserted on its own element. The regex above cannot
    // reach it: "Outcome unconfirmed" contains neither "is" nor "cannot", so a
    // whole-card negative silently let the shortest verdict on the card through
    // (Codex round 2). It reads as a label, which is exactly why it survived.
    const detail = card.querySelector('.approval-desk__meta')?.textContent?.trim();
    expect(detail).not.toBe('Outcome unconfirmed');
    expect(detail).toMatch(/last check/i);

    // And the body must not claim the traffic change was ACCEPTED: rule 2.5
    // also synthesises outcome_unknown from a stuck `claimed`, where no
    // operation handle exists at all.
    expect(card.textContent).not.toMatch(/was accepted/i);

    // Its notice names the reconciliation case, which the failed one cannot have.
    expect(getByTestId('approval-desk-unresolved-stale-notice').textContent).toMatch(
      /confirmed since/i,
    );
  });

  it('a stale failed keeps its terminal facts and gets the other notice', () => {
    // failed is terminal for its own attempt, so its sentences stay true. The
    // only thing a retained list gets wrong is presenting it as the CURRENT
    // open loop, so the notice names a later rollback and must NOT offer the
    // reconciliation escape that only outcome_unknown has.
    const { getByTestId } = render(ApprovalDesk, {
      props: {
        ...base,
        decisions: [unresolved('failed')],
        pendingApprovals: [],
        decisionsStale: true,
        degraded: true,
      },
    });
    const card = getByTestId('approval-desk-unresolved');
    expect(card.getAttribute('data-phase')).toBe('failed');
    expect(card.textContent).toMatch(/did not apply/i);
    const notice = getByTestId('approval-desk-unresolved-stale-notice');
    expect(notice.textContent).toMatch(/later rollback/i);
    expect(notice.textContent).not.toMatch(/confirmed since/i);
  });

  it('JA keeps the claim out too, on BOTH sources', async () => {
    // A locale that keeps the present tense is the same defect in the language
    // the product is delivered in.
    //
    // Both fixtures, because an earlier version rendered only the rollback one:
    // 確認が必要 and 承認済み belong to the IaC bylines, so against a rollback
    // card those alternatives could never appear and mutation-proved nothing.
    const { locale } = await import('../../src/lib/i18n');
    const CLAIMS = /待っています|確認が必要|承認済み|提案しています|適用を待って/;
    locale.set('ja');
    try {
      const rb = render(ApprovalDesk, {
        props: {
          ...base,
          decisions: [rollbackDecision()],
          pendingApprovals: [],
          decisionsStale: true,
          degraded: true,
        },
      });
      expect(rb.getByTestId('approval-desk-pending').textContent).not.toMatch(CLAIMS);
      cleanup();

      // Premise check: this fixture's JA byline really does assert, when fresh.
      const freshIac = render(ApprovalDesk, {
        props: {
          ...base,
          decisions: [iacDecision({ apply_status: 'waiting_for_rebake', merge_state: 'merged' })],
          pendingApprovals: [],
        },
      });
      expect(freshIac.getByTestId('approval-desk-pending').textContent).toMatch(CLAIMS);
      cleanup();

      const iac = render(ApprovalDesk, {
        props: {
          ...base,
          decisions: [iacDecision({ apply_status: 'waiting_for_rebake', merge_state: 'merged' })],
          pendingApprovals: [],
          decisionsStale: true,
          degraded: true,
        },
      });
      expect(iac.getByTestId('approval-desk-pending').textContent).not.toMatch(CLAIMS);
      cleanup();

      // The UNRESOLVED card too. Restricting this test to pending cards is how
      // 結果は未確認 survived round 2 in JA: it lives on a card this block was
      // never rendering. Premise first, then the negative.
      const freshUnknown = render(ApprovalDesk, {
        props: { ...base, decisions: [unresolved('outcome_unknown')], pendingApprovals: [] },
      });
      expect(freshUnknown.getByTestId('approval-desk-unresolved').textContent).toMatch(
        /結果は未確認/,
      );
      cleanup();

      const staleUnknown = render(ApprovalDesk, {
        props: {
          ...base,
          decisions: [unresolved('outcome_unknown')],
          pendingApprovals: [],
          decisionsStale: true,
          degraded: true,
        },
      });
      const jaCard = staleUnknown.getByTestId('approval-desk-unresolved');
      expect(jaCard.textContent).not.toMatch(/結果は未確認/);
      expect(jaCard.textContent).not.toMatch(/受理されました/);
      expect(jaCard.textContent).toMatch(/最後の確認時点|最後に確認した時点/);
    } finally {
      locale.set('en');
    }
  });
});
