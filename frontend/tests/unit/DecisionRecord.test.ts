import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import DecisionRecord from '../../src/components/DecisionRecord.svelte';
import { createTraceCache } from '../../src/lib/traceCache';
import type { TraceEvent } from '../../src/lib/timeline';
import type { Decision, TraceResponse } from '../../src/lib/types';
import { enMessages } from '../../src/locales';

// DecisionRecord — one decision opened on the DESK (ds-jns PR 2): the ledger
// row's accordion body, and the pinned card for a `?reasoning=` deep link whose
// decision is older than the listed rows.
//
// Unlike ReasoningDisclosure there is no toggle: the record is MOUNTED when the
// operator opens it, so mounting is the open action and is what must fetch. The
// tests drive a real cache with a mocked `call` for that reason.

afterEach(cleanup);

// jsdom has no layout, so it implements no scrollIntoView — the record calls it
// on every open. Same stub as TourCard.test.ts / ApprovalDesk.test.ts.
beforeEach(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

const TID = 'a'.repeat(32);

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const ev = (over: Partial<TraceEvent>): TraceEvent =>
  ({ event: 'llm_thought', trace_id: TID, ...over }) as TraceEvent;

function traceResponse(over: Partial<TraceResponse> = {}): TraceResponse {
  return { trace_id: TID, events: [], decision: null, complete: true, ...over };
}

function mount(
  handler: (path: string) => Response | Promise<Response> = () => res(traceResponse()),
  props: Record<string, unknown> = {},
) {
  const paths: string[] = [];
  const call = vi.fn(async (path: string) => {
    paths.push(path);
    return handler(path);
  });
  const cache = createTraceCache(call);
  const view = render(DecisionRecord, { props: { traceId: TID, cache, ...props } });
  return { ...view, cache, paths, call };
}

const ROLLBACK: Decision = {
  decision_id: 'd-1',
  trace_id: TID,
  action: 'rollback',
  created_at: '2026-05-31T15:06:00Z',
};

describe('DecisionRecord — opening IS mounting', () => {
  it('fetches the trace on mount, with no toggle to press', async () => {
    const { paths } = mount();
    await waitFor(() => expect(paths).toContain(`/trace/${TID}`));
  });

  it('renders the fetched reasoning rows', async () => {
    const { getByTestId } = mount(() =>
      res(traceResponse({ events: [ev({ insert_id: 't1', thought_text: 'Reading the service' })] })),
    );
    await waitFor(() =>
      expect(getByTestId('trace-row-thought').textContent).toContain('Reading the service'),
    );
  });

  it('scrolls itself into view, because the row that opened it can be off-screen', () => {
    mount();
    expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it('offers TraceDetail’s retry, wired to the cache', async () => {
    let ok = false;
    const { getByTestId, paths } = mount((p) =>
      p.endsWith('/pr-body') || ok ? res(traceResponse()) : res({ detail: 'nope' }, 502),
    );
    await waitFor(() => expect(getByTestId('trace-detail-error')).toBeTruthy());
    ok = true;
    await fireEvent.click(getByTestId('trace-detail-retry'));
    await waitFor(() => expect(paths.filter((p) => p === `/trace/${TID}`)).toHaveLength(2));
  });
});

describe('DecisionRecord — header', () => {
  it('names the action and the moment from the decision it was opened from', () => {
    const { getByTestId } = mount(() => res(traceResponse()), { decision: ROLLBACK });
    expect(getByTestId('decision-record-action').textContent).toBe('Rollback');
    // The absolute form (fmtWhen), not the ledger row's HH:mm: a record can be
    // any age, and the pinned out-of-window one always is.
    const when = getByTestId('decision-record-when').textContent ?? '';
    expect(when).toContain('2026');
    expect(when).not.toContain('2026-05-31T15:06:00Z');
  });

  it('falls back to the decision the trace itself carries', async () => {
    // The pinned deep-link case: App has a trace id and nothing else until
    // /trace answers.
    const { getByTestId } = mount(() => res(traceResponse({ decision: ROLLBACK })));
    await waitFor(() => expect(getByTestId('decision-record-action').textContent).toBe('Rollback'));
  });

  it('takes the crew from the trace events, which is the only place it exists', async () => {
    // Decision docs carry NO workload field — neither record_decision writer in
    // agent/main.py persists one — so a glyph derived from the decision would be
    // the unknown node on every row. The trace's own events do carry it.
    const { getByTestId } = mount(
      () =>
        res(
          traceResponse({
            events: [ev({ insert_id: 't1', workload: 'provision', thought_text: 'Drafting' })],
          }),
        ),
      { decision: { ...ROLLBACK, action: 'iac_apply' } },
    );
    await waitFor(() => expect(getByTestId('crew-glyph-provision')).toBeTruthy());
    expect(getByTestId('decision-record-crew').textContent).toBe('Provision');
  });

  it('renders NO glyph when no event names a crew, rather than the unknown node', async () => {
    // An iac_apply is recorded directly by the approval handler: there is no
    // reasoning run, so there is no crew. A static "unknown" square would be a
    // decorative claim about an agent that never ran.
    const { queryByTestId, getByTestId } = mount(() => res(traceResponse()), {
      decision: { ...ROLLBACK, action: 'iac_apply' },
    });
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('crew-glyph-unknown')).toBeNull();
    expect(queryByTestId('decision-record-crew')).toBeNull();
  });

  it('renders no header at all when there is nothing true to put in it', async () => {
    const { queryByTestId, getByTestId } = mount();
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('decision-record-header')).toBeNull();
  });
});

describe('DecisionRecord — the decision’s own prose', () => {
  // Carried over from the page-level replay's hero card, which read exactly
  // `rationale ?? rendered_body`. Nothing else on the desk shows it.
  it('renders the rationale', async () => {
    const { getByTestId } = mount(() => res(traceResponse()), {
      decision: { ...ROLLBACK, rationale: 'PORT drifted on the agent service' },
    });
    await waitFor(() =>
      expect(getByTestId('decision-record-prose').textContent).toContain(
        'PORT drifted on the agent service',
      ),
    );
  });

  it('falls back to rendered_body when there is no rationale', async () => {
    const { getByTestId } = mount(() => res(traceResponse()), {
      decision: { ...ROLLBACK, rendered_body: 'Adopts the orders subscription.' },
    });
    await waitFor(() =>
      expect(getByTestId('decision-record-prose').textContent).toContain(
        'Adopts the orders subscription.',
      ),
    );
  });

  it('takes it from the FETCHED doc when the row it was opened from lacks it', async () => {
    // GET /decisions projects a listing row; GET /trace carries the whole
    // decision. Preferring the row outright hid the prose entirely — the two
    // docs describe one decision and the record has to read both.
    const { getByTestId } = mount(
      () => res(traceResponse({ decision: { ...ROLLBACK, rationale: 'from the trace doc' } })),
      { decision: ROLLBACK },
    );
    await waitFor(() =>
      expect(getByTestId('decision-record-prose').textContent).toContain('from the trace doc'),
    );
  });

  it('keeps the ROW’s value where both docs carry one', async () => {
    // The row is the serve-time-enriched copy; a fetched value must fill gaps,
    // never downgrade a field the listing already resolved.
    const { getByTestId } = mount(
      () =>
        res(
          traceResponse({
            // An event, purely so the assertion can wait for PROOF the fetch
            // landed. Without it the first frame — where entry.decision is
            // still null and the row is the only doc — satisfies the assertion
            // and the merge order is never actually exercised.
            events: [ev({ insert_id: 't1', thought_text: 'landed' })],
            decision: { ...ROLLBACK, rationale: 'stale' },
          }),
        ),
      { decision: { ...ROLLBACK, rationale: 'fresh' } },
    );
    await waitFor(() => expect(getByTestId('trace-row-thought')).toBeTruthy());
    expect(getByTestId('decision-record-prose').textContent).toContain('fresh');
    expect(getByTestId('decision-record-prose').textContent).not.toContain('stale');
  });

  it('refuses to blend two DIFFERENT decisions into one document', async () => {
    // A trace can belong to several decisions (the create-class IaC lifecycle
    // pair), and GET /trace answers with the newest. Splicing them would build a
    // document that never existed — one doc's status fields wearing the other's
    // prose. On a mismatch ONE doc wins WHOLE, and it is the ROW's: this card is
    // that row's accordion body, so answering with anything else leaves it
    // contradicting the row it is expanded under.
    const { getByTestId } = mount(
      () =>
        res(
          traceResponse({
            events: [ev({ insert_id: 't1', thought_text: 'landed' })],
            decision: {
              ...ROLLBACK,
              decision_id: 'fetched',
              rationale: 'the fetched doc',
              approver: 'fetched@example.com',
            },
          }),
        ),
      { decision: { ...ROLLBACK, decision_id: 'row', rationale: 'the row doc' } },
    );
    await waitFor(() => expect(getByTestId('trace-row-thought')).toBeTruthy());
    const prose = getByTestId('decision-record-prose').textContent ?? '';
    expect(prose).toContain('the row doc');
    expect(prose).not.toContain('the fetched doc');
    // The losing doc loses ENTIRELY: `approver` is a field only it carries, so
    // if any part of it were spliced in, this is where it would show.
    expect(getByTestId('decision-record').textContent).not.toContain('fetched@example.com');
  });

  it('shows the row’s CURRENT stage, not the stage the cache froze', async () => {
    // The reachable mismatch, and the reason the ROW wins rather than the
    // fetched doc. A record held open while the create-class pair's second
    // document is written: the cache is fetched ONCE per trace and then frozen
    // at the `pending` sibling, while the row re-flows from GET /decisions on
    // every overview poll and becomes the `merged` one. Answering with the
    // cache would leave the card describing a stage the ledger has moved past.
    const IAC = {
      decision_id: 'x',
      trace_id: TID,
      action: 'iac_apply',
      pr_number: 68,
      created_at: '2026-06-04T14:53:36Z',
    } as Decision;
    const { getByTestId } = mount(
      () =>
        res(
          traceResponse({
            events: [ev({ insert_id: 't1', thought_text: 'landed' })],
            decision: { ...IAC, decision_id: 'older', merge_state: 'pending' },
          }),
        ),
      { decision: { ...IAC, decision_id: 'newer', merge_state: 'merged' } },
    );
    // Waiting on the thought row proves the FETCH landed — asserting on frame 0
    // would pass with the fetched doc still absent, which is not the state
    // under test.
    await waitFor(() => expect(getByTestId('trace-row-thought')).toBeTruthy());
    const card = getByTestId('decision-record').textContent ?? '';
    expect(card).toContain('merged');
    expect(card).not.toContain('pending');
  });

  it('renders nothing at all when the decision has no prose', async () => {
    const { queryByTestId, getByTestId } = mount(() => res(traceResponse()), {
      decision: ROLLBACK,
    });
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('decision-record-prose')).toBeNull();
  });

  it('escapes it — the prose is model-authored', async () => {
    const { getByTestId } = mount(() => res(traceResponse()), {
      decision: { ...ROLLBACK, rationale: '<img src=x onerror=alert(1)>' },
    });
    await waitFor(() => expect(getByTestId('decision-record-prose')).toBeTruthy());
    const el = getByTestId('decision-record-prose');
    expect(el.querySelector('img')).toBeNull();
    expect(el.textContent).toContain('<img src=x onerror=alert(1)>');
  });
});

describe('DecisionRecord — one card, one decision', () => {
  it('the panel reads the same decision the header names, even when /trace has none', async () => {
    // The ledger row supplies an iac_apply; GET /trace answers with no decision
    // doc. Without the override the header would say "Infra apply" while the
    // panel beneath it said the reasoning could not be loaded.
    const { getByTestId } = mount(() => res(traceResponse()), {
      decision: { ...ROLLBACK, action: 'iac_apply' },
    });
    await waitFor(() =>
      expect(getByTestId('trace-detail-empty').textContent).toContain('recorded directly'),
    );
    // ...and the header and the summary row now agree on the action's NAME.
    // They did not before ds-jns consolidated the two label tables: this card
    // rendered "Infrastructure change" above "Infra apply" for one decision.
    expect(getByTestId('decision-record-action').textContent).toBe('Infrastructure change');
    expect(getByTestId('decision-summary').textContent).toContain('Infrastructure change');
    expect(getByTestId('decision-summary').textContent).not.toContain('Infra apply');
  });
});

// The GitHub artifact link, carried over from the decisions rail ds-jns
// deleted. Its gate is the security-relevant half and lives in lib/approval.ts
// (decisionGithubLink) — pinned there against every url shape. What is pinned
// HERE is that this card actually asks: dropping the call would silently strand
// an Anchor issue with no route to it, which is exactly what deleting the rail
// did before this.
describe('DecisionRecord — the GitHub artifact it produced', () => {
  const ISSUE_URL = 'https://github.com/acme/ops/issues/99';

  it('links the issue a drift_issue decision filed', async () => {
    const { findByTestId } = mount(() => res(traceResponse()), {
      decision: {
        decision_id: 'd-gh',
        trace_id: TID,
        action: 'drift_issue',
        created_at: '2026-05-31T15:06:00Z',
        github: { url: ISSUE_URL },
      } as Decision,
    });
    const link = await findByTestId('decision-github-link');
    expect(link.getAttribute('href')).toBe(ISSUE_URL);
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('links a COMPLETED iac_apply to its PR, which nothing else on the desk does', async () => {
    // The gap the two allowlists leave between them. DecisionSummary prints
    // this decision's PR as plain `#68`, and the desk's pending hero only ever
    // offers the iac change that still needs an operator — so a record of work
    // already applied had a PR number and no way to reach it.
    const { findByTestId } = mount(() => res(traceResponse()), {
      decision: {
        decision_id: 'd-iac',
        trace_id: TID,
        action: 'iac_apply',
        created_at: '2026-05-31T15:06:00Z',
        pr_number: 68,
        apply_status: 'applied',
        github: { url: 'https://github.com/adi-prasetyo/driftscribe/pull/68' },
      } as Decision,
    });
    const link = await findByTestId('decision-github-link');
    expect(link.getAttribute('href')).toBe('https://github.com/adi-prasetyo/driftscribe/pull/68');
    // …and it is still host-allowlisted on this arm too.
    cleanup();
    const evil = mount(() => res(traceResponse()), {
      decision: {
        decision_id: 'd-iac-evil',
        trace_id: TID,
        action: 'iac_apply',
        created_at: '2026-05-31T15:06:00Z',
        github: { url: 'https://evil.example/x/y/pull/68' },
      } as Decision,
    });
    await evil.findByTestId('decision-record');
    expect(evil.queryByTestId('decision-github-link')).toBeNull();
  });

  it("links an infra change to the app's OWN record of it, labelled by its state", async () => {
    // The other half of an iac_apply's story. The GitHub link reaches the PR;
    // `/iac-approvals/<n>` is where the plan, the approval history and the
    // failure details live, and an operator had to hand-build that URL.
    //
    // The LABEL is the point: one href means different things depending on
    // where the change got to, and a flat "Approve" would offer an action on a
    // change that already ended.
    const base = {
      decision_id: 'd-iac',
      trace_id: TID,
      action: 'iac_apply',
      created_at: '2026-05-31T15:06:00Z',
      pr_number: 68,
    };
    for (const [state, expected] of [
      [{ apply_status: 'applied', merge_state: 'merged' }, /history/i],
      [{ apply_status: 'failed' }, /failure/i],
      // …and the two ACTIONABLE states are deliberately NOT among them: a
      // waiting change reads as the neutral page link here. See the next test.
      [{ apply_status: 'waiting_for_rebake', merge_state: 'merged' }, /approval page/i],
    ] as const) {
      const d = { ...base, ...state } as Decision;
      const view = mount(() => res(traceResponse()), { decision: d, decisions: [d] });
      const link = await view.findByTestId('iac-approve-link');
      expect(link.getAttribute('href')).toContain('/iac-approvals/68');
      expect(link.textContent, JSON.stringify(state)).toMatch(expected);
      cleanup();
    }
  });

  it('points a SUPERSEDED change at the change that overtook it, and says so', async () => {
    // Linking a superseded row to its own dead page would send the operator to
    // a plan nobody will ever apply. The rail's rule travelled with the link.
    const d = {
      decision_id: 'd-old',
      trace_id: TID,
      action: 'iac_apply',
      created_at: '2026-05-31T15:06:00Z',
      pr_number: 68,
      apply_status: 'waiting_for_rebake',
      superseded_by_pr: 71,
    } as Decision;
    const { findByTestId } = mount(() => res(traceResponse()), { decision: d, decisions: [d] });
    const link = await findByTestId('iac-approve-link');
    expect(link.getAttribute('href')).toContain('/iac-approvals/71');
    expect(link.textContent).toContain('71');
  });

  it('does not offer "Apply" on a PINNED change a newer row has already ended', async () => {
    // The fail-open my own first fix shipped, caught by review round 3.
    // `supersededWaitingIds` can only mark a WAITING row it can SEE, and a
    // pinned record is by definition a decision outside the recent list — so
    // passing the snapshot alone left the pinned doc unmarked and its label
    // read "Apply this change" for work that had already ended. The doc has to
    // join the input it is being judged against.
    const waiting = {
      decision_id: 'd-waiting',
      trace_id: TID,
      action: 'iac_apply',
      created_at: '2026-05-31T15:06:00Z',
      pr_number: 68,
      event_key: 'ek-1',
      apply_status: 'waiting_for_rebake',
      merge_state: 'merged',
    } as Decision;
    // SAME pr_number as the waiting row, because an event_key is derived per
    // generation and includes the PR — two rows sharing a key are two attempts
    // at ONE change, not two PRs (Codex review round 4 flagged the earlier
    // fixture as unfaithful on exactly this).
    const newerTerminal = {
      decision_id: 'd-applied',
      trace_id: 'b'.repeat(32),
      action: 'iac_apply',
      created_at: '2026-05-31T16:00:00Z',
      pr_number: 68,
      event_key: 'ek-1',
      apply_status: 'applied',
      merge_state: 'merged',
    } as Decision;
    // `decisions` is the recent snapshot and does NOT contain the pinned doc,
    // which is the whole shape of the bug.
    const { findByTestId } = mount(() => res(traceResponse()), {
      decision: waiting,
      decisions: [newerTerminal],
    });
    const link = await findByTestId('iac-approve-link');
    // Exact, not merely "not apply": the neutral page label, at the change's
    // own approval page. A weaker assertion would also pass on a blank label
    // or a link that had quietly moved.
    expect(link.textContent?.trim()).toBe(enMessages['shared.approve.goToPage']);
    expect(link.getAttribute('href')).toContain('/iac-approvals/68');
  });

  it('never says "apply" on a WAITING change, however complete the snapshot looks', async () => {
    // The demotion, and the reason a record cannot carry this label at all.
    // `apply`/`continue` are the only states derived from an ABSENCE — "no
    // newer terminal row found" — and `/decisions` is limit=50, so an old
    // record and its terminal sibling can BOTH be outside the window. That is
    // durable, not a polling lag: the helper finds nothing and would offer the
    // change as still yours to apply, forever (Codex review round 4).
    //
    // Driven with a snapshot that contains the row and nothing contradicting
    // it, which is exactly the state that LOOKS safe and is not.
    // BOTH actionable states, not just one: `waiting_for_rebake` splits on
    // merge_state into `apply` (merged) and `continue` (not yet), and demoting
    // only the first leaves the other offering an action on the same evidence.
    const base = {
      decision_id: 'd-iac',
      trace_id: TID,
      action: 'iac_apply',
      created_at: '2026-05-31T15:06:00Z',
      pr_number: 68,
      event_key: 'ek-1',
      apply_status: 'waiting_for_rebake',
    };
    const cases = [
      { ...base, merge_state: 'merged' } as Decision, // -> apply
      { ...base, merge_state: 'pending' } as Decision, // -> continue
    ];
    for (const [d, snapshot] of cases.flatMap((c) => [
      [c, [c]] as const,
      [c, null] as const,
    ])) {
      const view = mount(() => res(traceResponse()), { decision: d, decisions: snapshot });
      const link = await view.findByTestId('iac-approve-link');
      // Still reachable — the plan and the history are worth a click.
      expect(link.getAttribute('href')).toContain('/iac-approvals/68');
      // …but the copy claims nothing about what is left to do.
      const why = `${d.merge_state} / ${snapshot === null ? 'no snapshot' : 'snapshot'}`;
      expect(link.textContent, why).not.toMatch(/apply this change|continue/i);
      expect(link.textContent?.trim(), why).toBe(enMessages['shared.approve.goToPage']);
      cleanup();
    }
  });

  it('renders no link for an off-allowlist host, and none for a decision with no artifact', async () => {
    // Not a restatement of lib/approval's own coverage: this asserts the card
    // renders NOTHING rather than an empty or href-less anchor, which is the
    // failure a component can add on top of a correct gate.
    const { findByTestId, queryByTestId } = mount(() => res(traceResponse()), {
      decision: {
        decision_id: 'd-evil',
        trace_id: TID,
        action: 'drift_issue',
        created_at: '2026-05-31T15:06:00Z',
        github: { url: 'https://evil.example/acme/ops/issues/99' },
      } as Decision,
    });
    await findByTestId('decision-record');
    expect(queryByTestId('decision-github-link')).toBeNull();
    cleanup();

    const plain = mount(() => res(traceResponse()), { decision: ROLLBACK });
    await plain.findByTestId('decision-record');
    expect(plain.queryByTestId('decision-github-link')).toBeNull();
  });
});

// The two tokens that say a decision did LESS than its headline implies. Both
// lived only in the decisions rail; between deleting that (ds-jns Task 3.3) and
// this, NOTHING read `suppressed_by_autonomy` or `github.dry_run` at all — a
// drift_issue row said it filed an issue that a dry run never created. This is
// the highest-stakes class of thing on the card, so it is pinned per-token,
// per-direction, and for position.
describe('DecisionRecord — what the decision did NOT do', () => {
  function withDecision(over: Partial<Decision>) {
    return mount(() => res(traceResponse()), {
      decision: {
        decision_id: 'd-x',
        trace_id: TID,
        action: 'drift_issue',
        created_at: '2026-05-31T15:06:00Z',
        rationale: 'EXTRA drifted on payment-demo.',
        ...over,
      } as Decision,
    });
  }

  it('says a dry run created nothing on GitHub', async () => {
    const { findByTestId } = await Promise.resolve(
      withDecision({ github: { url: 'https://github.com/acme/ops/issues/99', dry_run: true } }),
    );
    expect((await findByTestId('decision-dry-run')).textContent).toContain('dry run');
  });

  it('says which autonomy mode stopped the action, by its LABEL not its key', async () => {
    const { findByTestId } = withDecision({
      suppressed_by_autonomy: true,
      autonomy_mode: 'observe',
    } as Partial<Decision>);
    const pill = await findByTestId('decision-autonomy-suppressed');
    // The localized mode name, never the raw enum leaking into operator copy.
    expect(pill.textContent).toContain('Observe');
    expect(pill.textContent).not.toContain('propose_apply');
  });

  it('falls back to the raw mode string for a mode the dial does not know', async () => {
    // The backend only suppresses in observe today. A future value must not
    // render a catalog key at the operator, and must not blank the pill either
    // — the FACT of suppression is the load-bearing half.
    const { findByTestId } = withDecision({
      suppressed_by_autonomy: true,
      autonomy_mode: 'some_future_mode',
    } as Partial<Decision>);
    const pill = await findByTestId('decision-autonomy-suppressed');
    expect(pill.textContent).toContain('some_future_mode');
    expect(pill.textContent).not.toContain('decisions.autonomy');
  });

  it('claims neither for an ordinary decision, and not on a rollback dry run', async () => {
    // The rollback exclusion is the one that matters: on a rollback row
    // `dry_run=true` does NOT suppress the worker calls — a real approval is
    // minted — so a "nothing was created" token there would be a lie in the
    // opposite direction.
    const plain = withDecision({ github: { url: 'https://github.com/acme/ops/issues/99' } });
    await plain.findByTestId('decision-record');
    expect(plain.queryByTestId('decision-dry-run')).toBeNull();
    expect(plain.queryByTestId('decision-autonomy-suppressed')).toBeNull();
    cleanup();

    const rollback = withDecision({
      action: 'rollback',
      github: { url: 'https://github.com/acme/ops/issues/99', dry_run: true },
    } as Partial<Decision>);
    await rollback.findByTestId('decision-record');
    expect(rollback.queryByTestId('decision-dry-run')).toBeNull();
  });

  it('reads BEFORE the prose it qualifies, not after it', async () => {
    // Position is the claim. "This was a dry run" placed under the rationale it
    // contradicts is a footnote on a paragraph the operator has already
    // believed. jsdom has no layout, but DOM order is what drives both the
    // visual order and the screen-reader order here.
    const { findByTestId, container } = withDecision({
      github: { url: 'https://github.com/acme/ops/issues/99', dry_run: true },
    });
    const caveat = await findByTestId('decision-dry-run');
    const prose = await findByTestId('decision-record-prose');
    expect(caveat.compareDocumentPosition(prose) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container).toBeTruthy();
  });
});

describe('DecisionRecord — record incomplete', () => {
  it('says so when the trace loaded and no decision doc is attached', async () => {
    // Reachable: a bare ?reasoning= link can name a CHAT turn's trace, which has
    // reasoning and no decision. Quiet register, not an error — the reasoning
    // above it loaded fine.
    const { getByTestId } = mount(() =>
      res(traceResponse({ events: [ev({ insert_id: 't1', thought_text: 'Reading' })] })),
    );
    await waitFor(() => expect(getByTestId('decision-record-incomplete')).toBeTruthy());
    expect(getByTestId('trace-row-thought')).toBeTruthy();
  });

  it('stays silent once a decision doc is present', async () => {
    const { queryByTestId, getByTestId } = mount(() => res(traceResponse()), {
      decision: ROLLBACK,
    });
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('decision-record-incomplete')).toBeNull();
  });

  it('does not claim it while the trace is still loading', async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => {
      release = r;
    });
    const { queryByTestId, getByTestId } = mount(() => gate);
    await waitFor(() => expect(getByTestId('trace-detail-loading')).toBeTruthy());
    expect(queryByTestId('decision-record-incomplete')).toBeNull();
    release(res(traceResponse()));
  });

  it('does not claim it when the trace failed to load', async () => {
    // "Couldn't load" and "loaded, nothing attached" are different facts and the
    // error line already states the first one.
    const { queryByTestId, getByTestId } = mount(() => res({ detail: 'nope' }, 502));
    await waitFor(() => expect(getByTestId('trace-detail-error')).toBeTruthy());
    expect(queryByTestId('decision-record-incomplete')).toBeNull();
  });
});

describe('DecisionRecord — out-of-window note', () => {
  it('renders the note only when the caller asks for it', async () => {
    const { getByTestId } = mount(() => res(traceResponse()), {
      decision: ROLLBACK,
      note: 'outOfWindow',
    });
    // States ABSENCE, not age: `settled` proves the record is not among the
    // listed decisions, which is not the same as its being older than them —
    // and this line can sit directly above "no decision record is attached to
    // this trace", where calling it "this decision" would contradict the line
    // below it.
    await waitFor(() => expect(getByTestId('decision-record-outofwindow')).toBeTruthy());
    const note = getByTestId('decision-record-outofwindow').textContent ?? '';
    expect(note).toContain('not in the recent decisions');
    expect(note).not.toContain('older');
  });

  it('omits it by default', async () => {
    const { queryByTestId, getByTestId } = mount(() => res(traceResponse()), {
      decision: ROLLBACK,
    });
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('decision-record-outofwindow')).toBeNull();
  });
});
