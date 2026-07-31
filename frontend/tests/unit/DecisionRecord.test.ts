import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import DecisionRecord from '../../src/components/DecisionRecord.svelte';
import { createTraceCache } from '../../src/lib/traceCache';
import type { TraceEvent } from '../../src/lib/timeline';
import type { Decision, TraceResponse } from '../../src/lib/types';

// DecisionRecord — one decision opened on the DESK (ds-jns PR 2): the ledger
// row's accordion body, and the pinned card for a `?reasoning=` deep link whose
// decision is older than the listed rows.
//
// Unlike ReasoningDisclosure there is no toggle: the record is MOUNTED when the
// operator opens it, so mounting is the open action and is what must fetch. The
// tests drive a real cache with a mocked `call` for that reason.

afterEach(cleanup);

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
    await waitFor(() =>
      expect(getByTestId('decision-record-outofwindow').textContent).toContain('older'),
    );
  });

  it('omits it by default', async () => {
    const { queryByTestId, getByTestId } = mount(() => res(traceResponse()), {
      decision: ROLLBACK,
    });
    await waitFor(() => expect(getByTestId('trace-detail-empty')).toBeTruthy());
    expect(queryByTestId('decision-record-outofwindow')).toBeNull();
  });
});
