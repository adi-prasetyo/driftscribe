import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { createOverviewStore, NO_DECISIONS_YET } from '../../src/lib/overviewStore';
import type { InfraGraph, PendingApproval } from '../../src/lib/infra_graph';
import type { Decision } from '../../src/lib/types';

// Store-level tests for the desk/estate single-owner refresh store (Task
// 3.0a). Ports the monotonic-seq + single-flight guard style from
// autonomyStore.test.ts / pauseStore.test.ts; the timer-driven poll trigger
// borrows infra_refresh.test.ts's fake-timer technique for RefreshScheduler.

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// Drains the microtask queue. Every fetch in this module resolves through a
// real (non-network) `Response` object — `.json()` on those settles in a
// handful of microtask hops with no timer involved — so a fixed number of
// `await Promise.resolve()` passes reliably observes the end state of a
// fire-and-forget refresh() the test didn't get a promise handle to (the
// creation-time trigger, the focus/visibilitychange triggers).
async function flush(times = 10): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

const GRAPH: InfraGraph = {
  generated_at: '2026-07-28T00:00:00Z',
  project: 'demo-proj',
  caveat: '',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 1, drift: 0 },
  groups: [],
  edges: [],
};

const APPROVALS: PendingApproval[] = [
  { pr_number: 5, title: 'Adopt bucket', url: 'https://github.com/x/y/pull/5', asset_type: 'storage.googleapis.com/Bucket', resource_name: 'b1' },
];

const DECISIONS: Decision[] = [{ decision_id: 'd1', action: 'iac_apply' }];

// A stateful call() mock: each endpoint's response is produced by a factory
// so individual tests can swap behaviour between calls (e.g. graph succeeds
// once then fails). Defaults mirror the happy path used by most tests.
function makeCall(overrides: {
  graph?: () => Response | Promise<Response>;
  pending?: () => Response | Promise<Response>;
  decisions?: () => Response | Promise<Response>;
} = {}) {
  const calls: string[] = [];
  const fn = vi.fn(async (path: string): Promise<Response> => {
    calls.push(path);
    if (path.startsWith('/infra/graph')) return overrides.graph ? overrides.graph() : res(GRAPH);
    if (path.startsWith('/infra/pending-approvals')) return overrides.pending ? overrides.pending() : res({ approvals: APPROVALS });
    if (path.startsWith('/decisions')) return overrides.decisions ? overrides.decisions() : res({ decisions: DECISIONS });
    throw new Error(`unexpected path in test: ${path}`);
  });
  return { fn, calls };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('createOverviewStore — creation + happy path', () => {
  it('fetches all three endpoints on creation, with no caller ever calling refresh()', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/infra/pending-approvals'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/decisions'))).toHaveLength(1);
    s.destroy();
  });

  it('applies graph, pendingApprovals and decisions from a successful refresh; lastError is null', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    const state = get(s);
    expect(state.graph).toEqual(GRAPH);
    expect(state.pendingApprovals).toEqual(APPROVALS);
    expect(state.decisions).toEqual(DECISIONS);
    expect(state.lastError).toBeNull();
    s.destroy();
  });

  it('initial state (before any fetch settles) is empty/null with the decisions sentinel', () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    const state = get(s);
    expect(state.graph).toBeNull();
    expect(state.pendingApprovals).toEqual([]);
    expect(state.decisions).toBe(NO_DECISIONS_YET); // reference identity — see App.svelte wiring
    expect(state.lastError).toBeNull();
    s.destroy();
  });
});

describe('createOverviewStore — focus / visibilitychange triggers', () => {
  it('a window focus event while the document is visible triggers a refetch', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    calls.length = 0;
    setVisibility('visible');
    window.dispatchEvent(new Event('focus'));
    await flush();
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    s.destroy();
  });

  it('a visibilitychange event to visible triggers a refetch', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    calls.length = 0;
    setVisibility('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    await flush();
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    s.destroy();
  });

  it('a visibilitychange event while hidden does NOT trigger a refetch', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    calls.length = 0;
    setVisibility('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    await flush();
    expect(calls).toHaveLength(0);
    s.destroy();
  });

  it('a focus event while the document is hidden does NOT trigger a refetch', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    calls.length = 0;
    setVisibility('hidden');
    window.dispatchEvent(new Event('focus'));
    await flush();
    expect(calls).toHaveLength(0);
    s.destroy();
  });
});

describe('createOverviewStore — single-flight collapse', () => {
  it('overlapping refreshes collapse: a refresh() called while one is in flight makes no extra fetches', async () => {
    let resolveGraph!: (r: Response) => void;
    const blocked = new Promise<Response>((r) => (resolveGraph = r));
    const { fn, calls } = makeCall({ graph: () => blocked });
    const s = createOverviewStore(fn); // creation-time refresh blocks on /infra/graph

    // pending + decisions resolve immediately even though graph is still in
    // flight — the whole cycle (Promise.all) hasn't committed to the store yet.
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/infra/pending-approvals'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/decisions'))).toHaveLength(1);

    const overlapping = s.refresh('manual'); // must collapse: refresh already in flight
    await flush();
    // Still exactly one call per endpoint — the overlapping call made no new ones.
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/infra/pending-approvals'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/decisions'))).toHaveLength(1);

    resolveGraph(res(GRAPH));
    await overlapping;
    await flush();
    expect(get(s).graph).toEqual(GRAPH);
    s.destroy();
  });

  // ds-6pi. The test above pins that a collapse makes no EXTRA fetches; this one
  // pins that the collapsed caller's need is actually met. They pull in opposite
  // directions and both matter: discarding the trigger is what kept the count at
  // one, and it is also what let an awaited refresh('chat-turn') resume on state
  // that provably predated the turn it was called for.
  it('a collapsed refresh is served by a trailing cycle, not by the one it rode', async () => {
    let resolveGraph!: (r: Response) => void;
    const blocked = new Promise<Response>((r) => (resolveGraph = r));
    // The decisions payload GAINS a row partway through, exactly as it would
    // when a chat turn lands while a slow (CAI-cold) graph fetch is in flight.
    let decisions: Decision[] = [];
    const { fn, calls } = makeCall({
      graph: () => blocked,
      decisions: () => res({ decisions }),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisions).toEqual([]);

    decisions = [{ decision_id: 'from-the-chat-turn', action: 'rollback' }];
    const chatTurn = s.refresh('chat-turn'); // collapses into the blocked cycle

    resolveGraph(res(GRAPH));
    await chatTurn;
    await flush();

    // The in-flight cycle's /decisions read was issued before the turn existed,
    // so only a SECOND read can see it. Awaiting must not resolve before then.
    expect(get(s).decisions).toEqual([{ decision_id: 'from-the-chat-turn', action: 'rollback' }]);
    expect(calls.filter((p) => p.startsWith('/decisions')).length).toBeGreaterThan(1);
    s.destroy();
  });

  // The trailing cycle is earned once per collapse, not once per collapsed
  // CALLER — three triggers arriving during one cycle must not queue three more.
  it('many collapsed callers share ONE trailing cycle', async () => {
    let resolveGraph!: (r: Response) => void;
    const blocked = new Promise<Response>((r) => (resolveGraph = r));
    const { fn, calls } = makeCall({ graph: () => blocked });
    const s = createOverviewStore(fn);
    await flush();

    const all = Promise.all([s.refresh('a'), s.refresh('b'), s.refresh('c')]);
    resolveGraph(res(GRAPH));
    await all;
    await flush();

    // One creation cycle + exactly one trailing cycle.
    expect(calls.filter((p) => p.startsWith('/decisions'))).toHaveLength(2);
    s.destroy();
  });

  // Guards the startup path the trailing cycle made expensive: the store used to
  // fire refresh('create') AND let scheduler.open(0)'s immediate onFetch collapse
  // into it. Harmless while collapses were discarded; a second full triple-fetch
  // on every first paint once they aren't.
  it('creation makes exactly one cycle, not a cycle plus a trailing one', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);
    expect(calls.filter((p) => p.startsWith('/decisions'))).toHaveLength(1);
    s.destroy();
  });
});

describe('createOverviewStore — independent soft failure', () => {
  it('a failed graph fetch keeps the prior graph and sets lastError; pending/decisions still apply', async () => {
    let graphCalls = 0;
    const { fn } = makeCall({
      graph: () => {
        graphCalls += 1;
        return graphCalls === 1 ? res(GRAPH) : res('server error', 500);
      },
      decisions: () => res({ decisions: graphCalls >= 2 ? [{ decision_id: 'd2', action: 'rollback' }] : DECISIONS }),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).graph).toEqual(GRAPH);
    expect(get(s).lastError).toBeNull();

    await s.refresh('manual'); // graph now 500s; pending/decisions still succeed
    const state = get(s);
    expect(state.graph).toEqual(GRAPH); // preserved, not nulled or cleared
    expect(state.lastError).toBe('graph');
    expect(state.decisions).toEqual([{ decision_id: 'd2', action: 'rollback' }]); // independent success still lands
    s.destroy();
  });

  it('a thrown graph fetch (network error) is treated the same as a non-ok response', async () => {
    let graphCalls = 0;
    const { fn } = makeCall({
      graph: () => {
        graphCalls += 1;
        if (graphCalls === 1) return res(GRAPH);
        throw new Error('network down');
      },
    });
    const s = createOverviewStore(fn);
    await flush();
    await s.refresh('manual');
    const state = get(s);
    expect(state.graph).toEqual(GRAPH);
    expect(state.lastError).toBe('graph');
    s.destroy();
  });

  it('a failed pending-approvals fetch keeps the prior list and sets lastError', async () => {
    let pendingCalls = 0;
    const { fn } = makeCall({
      pending: () => {
        pendingCalls += 1;
        return pendingCalls === 1 ? res({ approvals: APPROVALS }) : res('err', 500);
      },
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).pendingApprovals).toEqual(APPROVALS);

    await s.refresh('manual');
    const state = get(s);
    expect(state.pendingApprovals).toEqual(APPROVALS); // preserved
    expect(state.lastError).toBe('pending');
    s.destroy();
  });

  it('a malformed pending-approvals body (approvals not an array) is treated as a failure', async () => {
    const { fn } = makeCall({ pending: () => res({ approvals: 'nope' }) });
    const s = createOverviewStore(fn);
    await flush();
    const state = get(s);
    expect(state.pendingApprovals).toEqual([]); // never assigned the malformed value
    expect(state.lastError).toBe('pending');
    s.destroy();
  });

  it('a failed decisions fetch keeps the prior list and sets lastError', async () => {
    let decisionsCalls = 0;
    const { fn } = makeCall({
      decisions: () => {
        decisionsCalls += 1;
        return decisionsCalls === 1 ? res({ decisions: DECISIONS }) : res('err', 500);
      },
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisions).toEqual(DECISIONS);

    await s.refresh('manual');
    const state = get(s);
    expect(state.decisions).toEqual(DECISIONS); // preserved
    expect(state.lastError).toBe('decisions');
    s.destroy();
  });

  it('a malformed decisions body (decisions not an array) is treated as a failure', async () => {
    const { fn } = makeCall({ decisions: () => res({ decisions: 'nope' }) });
    const s = createOverviewStore(fn);
    await flush();
    const state = get(s);
    expect(state.decisions).toBe(NO_DECISIONS_YET); // never assigned the malformed value
    expect(state.lastError).toBe('decisions');
    s.destroy();
  });

  it('lastError clears on a subsequent fully-successful refresh', async () => {
    let graphCalls = 0;
    const { fn } = makeCall({
      graph: () => {
        graphCalls += 1;
        return graphCalls === 1 ? res('err', 500) : res(GRAPH);
      },
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).lastError).toBe('graph');

    await s.refresh('manual'); // graph succeeds this time
    expect(get(s).lastError).toBeNull();
    expect(get(s).graph).toEqual(GRAPH);
    s.destroy();
  });

  // lastError is ONE field, so a cycle with several failures has to pick a
  // winner. The documented order is graph > pending > decisions (graph is the
  // slowest and most failure-prone — CAI-backed, 10-30s cold — so it's the one
  // worth surfacing). Every other failure test fails exactly one endpoint, which
  // leaves that ternary chain unprotected; these two pin the precedence so a
  // later reorder can't quietly change which failure the operator is shown.
  it('when graph and pending both fail in one cycle, graph wins lastError', async () => {
    const { fn } = makeCall({
      graph: () => res('err', 500),
      pending: () => res('err', 500),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).lastError).toBe('graph');
    // The one endpoint that DID succeed still applied — a multi-failure cycle
    // is not an all-or-nothing abort.
    expect(get(s).decisions).toEqual(DECISIONS);
    s.destroy();
  });

  it('when pending and decisions both fail but graph succeeds, pending wins lastError', async () => {
    const { fn } = makeCall({
      pending: () => res('err', 500),
      decisions: () => res('err', 500),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).lastError).toBe('pending');
    expect(get(s).graph).toEqual(GRAPH);
    s.destroy();
  });
});

describe('createOverviewStore — polling via RefreshScheduler', () => {
  beforeEach(() => vi.useFakeTimers());

  it('polls on the scheduler default interval (45s) and keeps polling', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await vi.advanceTimersByTimeAsync(0); // settle the creation-time fetch
    calls.length = 0;

    await vi.advanceTimersByTimeAsync(45_000);
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(45_000);
    expect(calls.filter((p) => p.startsWith('/infra/graph'))).toHaveLength(2);

    s.destroy();
  });
});

describe('createOverviewStore — destroy()', () => {
  it('removes the focus/visibilitychange listeners — no refetch after teardown', async () => {
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    s.destroy();
    calls.length = 0;

    setVisibility('visible');
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
    await flush();
    expect(calls).toHaveLength(0);
  });

  it('stops the poll timer — no further fetches after teardown', async () => {
    vi.useFakeTimers();
    const { fn, calls } = makeCall();
    const s = createOverviewStore(fn);
    await vi.advanceTimersByTimeAsync(0);
    s.destroy();
    calls.length = 0;

    await vi.advanceTimersByTimeAsync(200_000);
    expect(calls).toHaveLength(0);
  });
});

// ds-eh6 — the store must let a consumer tell "we have not looked yet" and
// "we looked and could not see" apart from "there is nothing". Before this,
// all three were the same empty snapshot.
describe('createOverviewStore — settled / degraded (ds-eh6)', () => {
  it('starts unsettled and becomes settled after the first cycle', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    // The creation trigger fires synchronously but its fetches have not
    // resolved yet, so this observes the genuine pre-first-settle state.
    expect(get(s).settled).toBe(false);
    await flush();
    expect(get(s).settled).toBe(true);
    s.destroy();
  });

  it('settles even when every fetch fails — a failed look is still a look', async () => {
    // Otherwise a total outage pins the desk on "loading" forever, promising a
    // resolution that is not coming. The honest report is degraded, not
    // perpetual patience.
    const { fn } = makeCall({
      graph: () => res({}, 500),
      pending: () => res({}, 500),
      decisions: () => res({}, 500),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).settled).toBe(true);
    expect(get(s).degraded).toBe(true);
    s.destroy();
  });

  it('a clean cycle is not degraded', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).degraded).toBe(false);
    s.destroy();
  });

  it('honors the pending-approvals soft-fail flag on an otherwise-ok 200', async () => {
    // agent/main.py:3531 returns {approvals: [], degraded: true} when GitHub
    // errors. That is a well-formed 200 with an empty list, so every ok /
    // Array.isArray check passes and the outage is invisible unless the flag
    // is read — an empty list meaning "GitHub is down" and one meaning
    // "nothing is pending" are otherwise the same bytes.
    const { fn } = makeCall({ pending: () => res({ approvals: [], degraded: true }) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).lastError).toBeNull(); // nothing "failed" by the old measure
    expect(get(s).degraded).toBe(true);
    s.destroy();
  });

  it('a graph failure alone does NOT mark the desk degraded', async () => {
    // Graph feeds the estate section and is the routinely-slow endpoint
    // (CAI-backed, 10-30s cold). Letting it flip `degraded` would put the desk
    // in a degraded state during ordinary cold starts.
    const { fn } = makeCall({ graph: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).lastError).toBe('graph');
    expect(get(s).degraded).toBe(false);
    s.destroy();
  });

  it('degraded clears when a later cycle succeeds — recomputed, never latched', async () => {
    let fail = true;
    const { fn } = makeCall({
      pending: () => (fail ? res({}, 500) : res({ approvals: APPROVALS })),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).degraded).toBe(true);
    fail = false;
    await s.refresh('manual');
    await flush();
    expect(get(s).degraded).toBe(false);
    s.destroy();
  });
});

// Codex round 3 of #258 — approvalsStale is narrower than `degraded` on
// purpose: consumers that key an ABSENCE claim off pendingApprovals must not
// also fire when only /decisions failed.
describe('createOverviewStore — approvalsStale (ds-eh6)', () => {
  it('is set by the pending-approvals soft-fail 200', async () => {
    const { fn } = makeCall({ pending: () => res({ approvals: [], degraded: true }) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).approvalsStale).toBe(true);
    s.destroy();
  });

  it('is set by an outright pending-approvals failure', async () => {
    const { fn } = makeCall({ pending: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).approvalsStale).toBe(true);
    s.destroy();
  });

  it('is NOT set when only /decisions failed, though degraded IS', async () => {
    // The distinction that makes this field worth having: a decisions outage
    // says nothing about whether an adoption PR exists.
    const { fn } = makeCall({ decisions: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).degraded).toBe(true);
    expect(get(s).approvalsStale).toBe(false);
    s.destroy();
  });

  it('is NOT set on a clean cycle', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).approvalsStale).toBe(false);
    s.destroy();
  });
});

// ds-1vn r2 (Codex review). Sibling of approvalsStale above, one lane over.
// A failed graph fetch preserves the previous graph — right for the numbers,
// wrong for `iac_snapshot_stale`, which is an ASSURANCE: retaining a `false`
// reports "checked, current" about a check that did not run this cycle.
describe('createOverviewStore — graphStale (ds-1vn)', () => {
  it('is set when the graph fetch fails', async () => {
    const { fn } = makeCall({ graph: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).graphStale).toBe(true);
    s.destroy();
  });

  it('is NOT set when only pending or decisions failed', async () => {
    // Same distinction that earns approvalsStale its existence: a decisions
    // outage says nothing about the currency of the graph.
    const { fn } = makeCall({ pending: () => res({}, 500), decisions: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).degraded).toBe(true);
    expect(get(s).graphStale).toBe(false);
    s.destroy();
  });

  it('is NOT set on a clean cycle', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).graphStale).toBe(false);
    s.destroy();
  });

  it('clears on the next successful cycle, while the retained graph is replaced', async () => {
    let fail = true;
    const { fn } = makeCall({ graph: () => (fail ? res({}, 500) : res(GRAPH)) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).graphStale).toBe(true);
    fail = false;
    await s.refresh();
    await flush();
    expect(get(s).graphStale).toBe(false);
    s.destroy();
  });
});

// ds-jk9. The third per-lane flag, and the one the desk hero needs: `degraded`
// already fires when EITHER desk lane fails, so gating the hero's CTA on it
// would discard a perfectly fresh listing whenever /decisions blinked.
describe('createOverviewStore — decisionsStale (ds-jk9)', () => {
  it('is set when the decisions fetch fails', async () => {
    const { fn } = makeCall({ decisions: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisionsStale).toBe(true);
    s.destroy();
  });

  it('is set when /decisions answers a malformed body', async () => {
    // fetchDecisionsList rejects a non-array `decisions` as `ok: false`, which
    // retains the previous list exactly like a 500 does — so it must set the
    // flag by the same route. A 200 that fails the shape check is still a
    // failed refresh.
    const { fn } = makeCall({ decisions: () => res({ decisions: 'nope' }) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisionsStale).toBe(true);
    s.destroy();
  });

  it('is NOT set when only pending or graph failed, though degraded IS', async () => {
    // The distinction that earns this field its existence, in the direction
    // that matters for the hero: a pending-approvals outage says nothing about
    // whether the retained decisions are current.
    const { fn } = makeCall({ pending: () => res({}, 500), graph: () => res({}, 500) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).degraded).toBe(true);
    expect(get(s).decisionsStale).toBe(false);
    s.destroy();
  });

  it('is NOT set by the pending-approvals soft-fail 200', async () => {
    // /decisions has no soft-fail mode of its own; the sibling lane's must not
    // leak across.
    const { fn } = makeCall({ pending: () => res({ approvals: [], degraded: true }) });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).approvalsStale).toBe(true);
    expect(get(s).decisionsStale).toBe(false);
    s.destroy();
  });

  it('is NOT set on a clean cycle', async () => {
    const { fn } = makeCall();
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisionsStale).toBe(false);
    s.destroy();
  });

  it('clears on the next successful cycle, while the retained list is replaced', async () => {
    let fail = true;
    const { fn } = makeCall({
      decisions: () => (fail ? res({}, 500) : res({ decisions: [] })),
    });
    const s = createOverviewStore(fn);
    await flush();
    expect(get(s).decisionsStale).toBe(true);
    fail = false;
    await s.refresh();
    await flush();
    expect(get(s).decisionsStale).toBe(false);
    s.destroy();
  });
});
