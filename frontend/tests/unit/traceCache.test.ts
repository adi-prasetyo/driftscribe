import { describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import { createTraceCache, TRACE_CACHE_MAX } from '../../src/lib/traceCache';
import type { TraceCache, TraceCacheEntry } from '../../src/lib/traceCache';
import type { TraceEvent } from '../../src/lib/timeline';
import type { Decision, PrBody, TraceResponse } from '../../src/lib/types';

// Per-trace cache (ds-jns PR 1, design §0). The two axes — SSE lifecycle
// (`stream`) and /trace enrichment (`enrich`) — are deliberately independent:
// a live stream and a lazy /trace fetch can coexist and each fails on its own.
// These tests pin that separation, the meta-before-first-event ordering the
// disclosure relies on, and the fail-soft pr-body lane.

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** Drain the microtask queue so a fire-and-forget ensure() settles. */
async function flush(times = 10): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve();
}

function entryOf(cache: TraceCache, tid: string): TraceCacheEntry | undefined {
  return get(cache).get(tid);
}

const TID = 'a'.repeat(32);

function thought(text: string, insertId: string): TraceEvent {
  return {
    event: 'llm_thought',
    trace_id: TID,
    insert_id: insertId,
    thought_text: text,
  };
}

function mcp(tool: string, insertId: string): TraceEvent {
  return {
    event: 'mcp_call',
    trace_id: TID,
    insert_id: insertId,
    mcp_server: 'developer-knowledge',
    mcp_tool: tool,
  };
}

function traceResponse(over: Partial<TraceResponse> = {}): TraceResponse {
  return {
    trace_id: TID,
    events: [],
    decision: null,
    complete: true,
    ...over,
  };
}

const PR_BODY: PrBody = {
  pr_number: 42,
  head_sha: 'deadbeef',
  body: 'Adopts the bucket.',
  body_truncated: false,
  cached: false,
};

const IAC_DECISION: Decision = {
  decision_id: 'd-iac',
  action: 'iac_apply',
  trace_id: TID,
  pr_number: 42,
};

/** A `call` mock whose responses are chosen per path prefix. */
function makeCall(handlers: {
  trace?: () => Response | Promise<Response>;
  prBody?: () => Response | Promise<Response>;
} = {}) {
  const paths: string[] = [];
  const fn = vi.fn(async (path: string): Promise<Response> => {
    paths.push(path);
    if (path.endsWith('/pr-body')) {
      return handlers.prBody ? handlers.prBody() : res(PR_BODY);
    }
    return handlers.trace ? handlers.trace() : res(traceResponse());
  });
  return { fn, paths };
}

describe('createTraceCache — live stream lifecycle', () => {
  it('beginLive creates a streaming entry with no events, before any event arrives', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    const e = entryOf(cache, TID);
    expect(e).toBeDefined();
    expect(e!.stream).toBe('streaming');
    expect(e!.enrich).toBe('idle');
    expect(e!.events).toEqual([]);
    expect(e!.decision).toBeNull();
    expect(e!.prBody).toBeNull();
    expect(e!.prBodyMissing).toBe(false);
  });

  it('ensure() on a streaming entry never fetches /trace (the meta frame beat the events)', async () => {
    const { fn, paths } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    await cache.ensure(TID);
    expect(paths).toEqual([]);
    expect(entryOf(cache, TID)!.enrich).toBe('idle');
  });

  it('appendLive keeps arrival order and retains llm_usage (omittedThoughtTokens needs it)', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    const a = thought('one', 'i1');
    const b: TraceEvent = { event: 'llm_usage', trace_id: TID, insert_id: 'i2', thoughts_token_count: 12 };
    const c = thought('two', 'i3');
    cache.appendLive(TID, a);
    cache.appendLive(TID, b);
    cache.appendLive(TID, c);
    expect(entryOf(cache, TID)!.events).toEqual([a, b, c]);
  });

  it("endLive('error') marks the stream errored and keeps whatever arrived", () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    cache.appendLive(TID, thought('partial', 'i1'));
    cache.endLive(TID, 'error');
    const e = entryOf(cache, TID)!;
    expect(e.stream).toBe('error');
    expect(e.events).toHaveLength(1);
    expect(e.enrich).toBe('idle');
  });

  it("endLive('complete') marks the stream complete", () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    cache.endLive(TID, 'complete');
    expect(entryOf(cache, TID)!.stream).toBe('complete');
  });
});

describe('createTraceCache — settleBackfill', () => {
  it('merges only the mcp side-channel per reconcileBackfill and marks enrichment loaded', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    const live = thought('streamed', 'stream-1');
    cache.appendLive(TID, live);
    cache.endLive(TID, 'complete');

    // The fetched snapshot carries the SAME logical thought under a different
    // insert_id (Cloud Logging restamps it) plus the trace-only mcp row. A
    // wholesale replace would drop nothing visible here, so the fixture proves
    // the merge in BOTH directions: the mcp is added, the duplicate is not.
    const restamped = { ...live, insert_id: 'cloud-logging-1' };
    const side = mcp('search_documents', 'cloud-logging-2');
    const decision: Decision = { decision_id: 'd1', action: 'drift_pr' };
    cache.settleBackfill(TID, traceResponse({ events: [restamped, side], decision }));

    const e = entryOf(cache, TID)!;
    expect(e.events).toEqual([live, side]);
    expect(e.enrich).toBe('loaded');
    expect(e.decision).toEqual(decision);
    expect(e.stream).toBe('complete'); // the stream axis is untouched
  });

  it('settleBackfill(null) keeps the live events and leaves enrichment alone', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    cache.appendLive(TID, thought('streamed', 'i1'));
    cache.endLive(TID, 'complete');
    cache.settleBackfill(TID, null);
    const e = entryOf(cache, TID)!;
    expect(e.events).toHaveLength(1);
    expect(e.enrich).toBe('idle');
    expect(e.decision).toBeNull();
  });

  it('settles the trace id it was given, not whichever run is current', () => {
    // The key guarantee: a fast follow-up turn must not orphan the previous
    // trace's entry. The cache is keyed, so an old backfill lands on the old id.
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    const older = 'b'.repeat(32);
    cache.beginLive(older);
    cache.appendLive(older, { event: 'llm_thought', trace_id: older, insert_id: 'o1' });
    cache.endLive(older, 'complete');
    cache.beginLive(TID); // a newer turn started

    const side = { ...mcp('search_documents', 'o2'), trace_id: older };
    cache.settleBackfill(older, traceResponse({ trace_id: older, events: [side] }));

    expect(entryOf(cache, older)!.events).toHaveLength(2);
    expect(entryOf(cache, older)!.enrich).toBe('loaded');
    expect(entryOf(cache, TID)!.events).toEqual([]); // untouched
  });

  it('creates the entry when the backfill is the first thing we hear (JSON fallback)', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    const side = mcp('search_documents', 'i1');
    cache.settleBackfill(TID, traceResponse({ events: [side] }));
    const e = entryOf(cache, TID)!;
    expect(e.events).toEqual([side]);
    expect(e.enrich).toBe('loaded');
    expect(e.stream).toBe('idle');
  });
});

describe('createTraceCache — lazy enrichment', () => {
  it('ensure() walks idle → loading → loaded and stores events + decision', async () => {
    let release!: (r: Response) => void;
    const gate = new Promise<Response>((r) => {
      release = r;
    });
    const { fn, paths } = makeCall({ trace: () => gate });
    const cache = createTraceCache(fn);

    const p = cache.ensure(TID);
    expect(entryOf(cache, TID)!.enrich).toBe('loading');
    expect(paths).toEqual([`/trace/${TID}`]);

    const decision: Decision = { decision_id: 'd1', action: 'drift_pr' };
    const evts = [thought('historical', 'h1'), mcp('search_documents', 'h2')];
    release(res(traceResponse({ events: evts, decision })));
    await p;

    const e = entryOf(cache, TID)!;
    expect(e.enrich).toBe('loaded');
    expect(e.events).toEqual(evts);
    expect(e.decision).toEqual(decision);
    expect(e.stream).toBe('idle');
  });

  it('a second ensure() does not re-fetch', async () => {
    const { fn, paths } = makeCall();
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    await cache.ensure(TID);
    expect(paths).toEqual([`/trace/${TID}`]);
  });

  it('concurrent ensure() calls collapse into one fetch', async () => {
    const { fn, paths } = makeCall();
    const cache = createTraceCache(fn);
    await Promise.all([cache.ensure(TID), cache.ensure(TID)]);
    expect(paths).toEqual([`/trace/${TID}`]);
  });

  it('a non-ok /trace marks enrichment errored, never blank', async () => {
    const { fn } = makeCall({ trace: () => res({ detail: 'nope' }, 500) });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    expect(entryOf(cache, TID)!.enrich).toBe('error');
  });

  it('a rejected /trace marks enrichment errored', async () => {
    const { fn } = makeCall({ trace: () => Promise.reject(new Error('offline')) });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    expect(entryOf(cache, TID)!.enrich).toBe('error');
  });

  it('retry() re-fetches after an error and recovers', async () => {
    let fail = true;
    const { fn, paths } = makeCall({
      trace: () => (fail ? res({}, 500) : res(traceResponse({ events: [thought('ok', 'r1')] }))),
    });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    expect(entryOf(cache, TID)!.enrich).toBe('error');
    fail = false;
    await cache.retry(TID);
    expect(entryOf(cache, TID)!.enrich).toBe('loaded');
    expect(entryOf(cache, TID)!.events).toHaveLength(1);
    expect(paths).toHaveLength(2);
  });

  it('a live entry keeps its streamed events when ensure() enriches it later', async () => {
    const live = thought('streamed', 'stream-1');
    const side = mcp('search_documents', 'cloud-2');
    const { fn } = makeCall({
      trace: () => res(traceResponse({ events: [{ ...live, insert_id: 'cloud-1' }, side] })),
    });
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    cache.appendLive(TID, live);
    cache.endLive(TID, 'error'); // stream died; enrichment is a separate axis
    await cache.ensure(TID);
    const e = entryOf(cache, TID)!;
    expect(e.events).toEqual([live, side]); // merged, not replaced
    expect(e.enrich).toBe('loaded');
    expect(e.stream).toBe('error');
  });
});

describe('createTraceCache — pr-body lane (fail-soft)', () => {
  it('ensure() fetches the pr-body for an iac_apply decision', async () => {
    const { fn, paths } = makeCall({
      trace: () => res(traceResponse({ decision: IAC_DECISION })),
    });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    expect(paths).toEqual([`/trace/${TID}`, `/trace/${TID}/pr-body`]);
    expect(entryOf(cache, TID)!.prBody).toEqual(PR_BODY);
    expect(entryOf(cache, TID)!.prBodyMissing).toBe(false);
  });

  it('does not fetch a pr-body for a non-iac_apply decision', async () => {
    const { fn, paths } = makeCall({
      trace: () => res(traceResponse({ decision: { decision_id: 'd', action: 'drift_pr' } })),
    });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    expect(paths).toEqual([`/trace/${TID}`]);
  });

  it('a failed pr-body sets prBodyMissing and leaves enrichment loaded', async () => {
    const { fn } = makeCall({
      trace: () => res(traceResponse({ decision: IAC_DECISION })),
      prBody: () => Promise.reject(new Error('github down')),
    });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    const e = entryOf(cache, TID)!;
    expect(e.enrich).toBe('loaded'); // the trace itself is still usable
    expect(e.prBody).toBeNull();
    expect(e.prBodyMissing).toBe(true);
  });

  it('does not re-request a pr-body already known to be missing', async () => {
    const { fn, paths } = makeCall({
      trace: () => res(traceResponse({ decision: IAC_DECISION })),
      prBody: () => res({}, 404),
    });
    const cache = createTraceCache(fn);
    await cache.ensure(TID);
    await cache.ensure(TID);
    expect(paths.filter((p) => p.endsWith('/pr-body'))).toHaveLength(1);
  });

  it('ensure() on a settled live iac_apply entry fetches ONLY the pr-body', async () => {
    const { fn, paths } = makeCall();
    const cache = createTraceCache(fn);
    cache.beginLive(TID);
    cache.appendLive(TID, thought('streamed', 'i1'));
    cache.endLive(TID, 'complete');
    // The backfill already loaded the decision — /trace must not run again, but
    // the pr-body never had a chance to load, so it must.
    cache.settleBackfill(TID, traceResponse({ decision: IAC_DECISION }));
    await cache.ensure(TID);
    expect(paths).toEqual([`/trace/${TID}/pr-body`]);
    expect(entryOf(cache, TID)!.prBody).toEqual(PR_BODY);
  });
});

describe('createTraceCache — eviction', () => {
  it('evicts the oldest settled entries above the cap', async () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    const ids: string[] = [];
    for (let i = 0; i < TRACE_CACHE_MAX + 5; i++) {
      const id = String(i).padStart(32, '0');
      ids.push(id);
      cache.beginLive(id);
      cache.endLive(id, 'complete');
    }
    const map = get(cache);
    expect(map.size).toBe(TRACE_CACHE_MAX);
    expect(map.has(ids[0])).toBe(false);
    expect(map.has(ids[4])).toBe(false);
    expect(map.has(ids[5])).toBe(true);
    expect(map.has(ids[ids.length - 1])).toBe(true);
  });

  it('never evicts a streaming entry', () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    const streaming = 'f'.repeat(32);
    cache.beginLive(streaming); // oldest, and still live
    for (let i = 0; i < TRACE_CACHE_MAX + 5; i++) {
      const id = String(i).padStart(32, '0');
      cache.beginLive(id);
      cache.endLive(id, 'complete');
    }
    const map = get(cache);
    expect(map.has(streaming)).toBe(true);
    expect(map.size).toBeLessThanOrEqual(TRACE_CACHE_MAX);
  });
});

describe('createTraceCache — store notification', () => {
  it('notifies subscribers on every mutation with a fresh map', async () => {
    const { fn } = makeCall();
    const cache = createTraceCache(fn);
    const seen: number[] = [];
    const unsub = cache.subscribe((m) => seen.push(m.size));
    cache.beginLive(TID);
    cache.appendLive(TID, thought('x', 'i1'));
    await flush();
    unsub();
    expect(seen[0]).toBe(0);
    expect(seen.length).toBeGreaterThanOrEqual(3);
  });
});
