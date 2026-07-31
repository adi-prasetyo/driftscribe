// traceCache.ts — per-trace state, keyed by trace id (ds-jns, design §0).
//
// App.svelte owns ONE global events/traceId/status/decision/pr-body cluster:
// every submission clears it, and a resumed conversation loads only the LATEST
// turn's trace into it. Inline per-message reasoning disclosures cannot ride on
// that — expanding an old message would fight the live run for the same state,
// and runSeq would cancel it. This cache gives every trace its own entry, so a
// live stream and three expanded historical disclosures coexist.
//
// TWO INDEPENDENT AXES, deliberately not collapsed into one union:
//
//   `stream`  — the SSE lifecycle for a trace produced in THIS session.
//   `enrich`  — the GET /trace (+ /trace/{id}/pr-body) lifecycle.
//
// They overlap: a live entry that already streamed its reasoning can still be
// enriched later for the decision doc and PR body it never carried, and either
// side can fail while the other is fine. A single status field would have to
// pick a winner and would lie about the loser.
//
// Factory-pattern store (createTraceCache(call)), same shape as
// overviewStore.ts / autonomyStore.ts, instantiated once in App.svelte.
import { writable, type Readable } from 'svelte/store';
import { reconcileBackfill, type TraceEvent } from './timeline';
import type { Decision, PrBody, TraceResponse } from './types';

/** App.svelte's local `call` helper (its token-aware fetch wrapper). */
export type CallFn = (path: string, init?: RequestInit) => Promise<Response>;

export interface TraceCacheEntry {
  /** Every ChatEvent for this trace, in arrival order — INCLUDING llm_usage,
   *  which `omittedThoughtTokens` needs (interleaveTimeline drops the rows it
   *  doesn't render, so filtering here would destroy information). */
  events: TraceEvent[];
  /** Live SSE lifecycle. 'idle' = never streamed here (history / deep-link). */
  stream: 'idle' | 'streaming' | 'complete' | 'error';
  /** GET /trace (+ pr-body) lifecycle — independent of `stream`. */
  enrich: 'idle' | 'loading' | 'loaded' | 'error';
  decision: Decision | null;
  prBody: PrBody | null;
  /** The pr-body fetch failed fail-soft. The trace itself stays usable, so this
   *  is NOT `enrich: 'error'` — it renders one quiet line, not an error state. */
  prBodyMissing: boolean;
}

export interface TraceCache extends Readable<ReadonlyMap<string, TraceCacheEntry>> {
  /** Called from the SSE `meta` frame — creates the entry with
   *  stream:'streaming' BEFORE any timeline event exists, so a disclosure the
   *  operator expands early never fires a premature /trace fetch for a trace
   *  that is still being produced. */
  beginLive(traceId: string): void;
  appendLive(traceId: string, event: TraceEvent): void;
  /** Stream ended. 'error' covers transport failure and error frames. */
  endLive(traceId: string, outcome: 'complete' | 'error'): void;
  /** Post-`done` backfill result for THIS trace id. Keyed, NOT ridden on the
   *  caller's runSeq guard: that guard protects the GLOBAL timeline write, and
   *  a fast follow-up turn bumping runSeq must not orphan the previous trace's
   *  entry. `null` = the backfill failed or returned nothing: keep the live
   *  events and leave `enrich` alone. Merges via reconcileBackfill and stores
   *  the decision doc; does NOT fetch the pr-body — ensure() does that lazily
   *  when a disclosure is actually opened. */
  settleBackfill(traceId: string, fetched: TraceResponse | null): void;
  /** Lazy enrichment, called when a disclosure/record expands.
   *   - fetches /trace when `enrich` is 'idle' or 'error' AND the stream is not
   *     currently running (a streaming trace is not yet in Cloud Logging, and
   *     the stream is authoritative for it anyway);
   *   - ALSO fetches /trace/{id}/pr-body when the decision is an `iac_apply`
   *     and no body is held yet — even when `enrich` is already 'loaded',
   *     because a settled LIVE entry got its decision from the backfill and
   *     never had a pr-body fetch of its own. */
  ensure(traceId: string): Promise<void>;
  /** Operator-driven recovery from `enrich: 'error'` / a missing pr-body. */
  retry(traceId: string): Promise<void>;
}

/** Retained entries. Chat threads are short and each entry is a handful of
 *  small event objects, so this is a leak bound rather than a tuned figure. */
export const TRACE_CACHE_MAX = 30;

function blank(): TraceCacheEntry {
  return {
    events: [],
    stream: 'idle',
    enrich: 'idle',
    decision: null,
    prBody: null,
    prBodyMissing: false,
  };
}

export function createTraceCache(call: CallFn): TraceCache {
  const { subscribe, update } = writable<ReadonlyMap<string, TraceCacheEntry>>(
    new Map<string, TraceCacheEntry>(),
  );

  // Per-trace-id single-flight for the two lanes. `enrich: 'loading'` already
  // collapses concurrent /trace calls, but the pr-body lane has no state of its
  // own between "asked" and "answered", so it needs an explicit guard or two
  // disclosures expanding together would each fetch it.
  const prBodyInFlight = new Set<string>();

  /** Replace one entry immutably and publish. `mutate` returns the NEXT entry
   *  (or the same reference to skip the write). */
  function patch(
    traceId: string,
    mutate: (prev: TraceCacheEntry) => TraceCacheEntry,
    { create = true }: { create?: boolean } = {},
  ): void {
    update((prev) => {
      const existing = prev.get(traceId);
      if (!existing && !create) return prev;
      const next = mutate(existing ?? blank());
      if (existing && next === existing) return prev;
      const map = new Map(prev);
      map.set(traceId, next);
      evict(map);
      return map;
    });
  }

  /** Drop the oldest non-streaming entries until the map fits the cap.
   *  Insertion order is the age order (Map preserves it). A `streaming` entry
   *  is never evicted — its events exist ONLY here, so dropping it would erase
   *  reasoning the operator is watching arrive. Every other state is
   *  re-derivable from GET /trace, so all of them are evictable (the design
   *  named idle/complete; including 'error' keeps the bound real rather than
   *  letting a run of failed streams grow without limit). */
  function evict(map: Map<string, TraceCacheEntry>): void {
    if (map.size <= TRACE_CACHE_MAX) return;
    for (const [id, e] of map) {
      if (map.size <= TRACE_CACHE_MAX) return;
      if (e.stream === 'streaming') continue;
      map.delete(id);
    }
  }

  function read(traceId: string): TraceCacheEntry | undefined {
    let found: TraceCacheEntry | undefined;
    const unsub = subscribe((m) => {
      found = m.get(traceId);
    });
    unsub();
    return found;
  }

  function beginLive(traceId: string): void {
    patch(traceId, (prev) => ({ ...prev, stream: 'streaming' }));
  }

  function appendLive(traceId: string, event: TraceEvent): void {
    // Creates the entry if the `meta` frame was missed — an event is proof the
    // trace exists, and dropping it would lose reasoning outright.
    patch(traceId, (prev) => ({
      ...prev,
      stream: prev.stream === 'idle' ? 'streaming' : prev.stream,
      events: [...prev.events, event],
    }));
  }

  function endLive(traceId: string, outcome: 'complete' | 'error'): void {
    patch(traceId, (prev) => ({ ...prev, stream: outcome }), { create: false });
  }

  function settleBackfill(traceId: string, fetched: TraceResponse | null): void {
    if (fetched == null) return; // failed/empty — keep the live events as they are
    const events = Array.isArray(fetched.events) ? fetched.events : [];
    patch(traceId, (prev) => ({
      ...prev,
      // reconcileBackfill protects the live timeline from an ingestion-lagged
      // snapshot and adds only the trace-only mcp_call side-channel. On an
      // entry with no live events it returns the snapshot wholesale.
      events: reconcileBackfill(prev.events, events),
      decision: fetched.decision ?? null,
      enrich: 'loaded',
    }));
  }

  async function fetchTrace(traceId: string): Promise<void> {
    patch(traceId, (prev) => ({ ...prev, enrich: 'loading' }));
    try {
      const resp = await call('/trace/' + encodeURIComponent(traceId));
      if (!resp.ok) {
        patch(traceId, (prev) => ({ ...prev, enrich: 'error' }));
        return;
      }
      const t = (await resp.json()) as TraceResponse;
      const fetchedEvents = Array.isArray(t.events) ? t.events : [];
      patch(traceId, (prev) => ({
        ...prev,
        // An entry that never streamed has no live timeline to protect, so the
        // snapshot IS the truth. One that did keeps its live events and gains
        // only the mcp side-channel (see reconcileBackfill's contract).
        events: prev.stream === 'idle' ? fetchedEvents : reconcileBackfill(prev.events, fetchedEvents),
        decision: t.decision ?? null,
        enrich: 'loaded',
      }));
    } catch {
      patch(traceId, (prev) => ({ ...prev, enrich: 'error' }));
    }
  }

  async function fetchPrBody(traceId: string): Promise<void> {
    prBodyInFlight.add(traceId);
    try {
      const resp = await call('/trace/' + encodeURIComponent(traceId) + '/pr-body');
      if (!resp.ok) {
        patch(traceId, (prev) => ({ ...prev, prBodyMissing: true }), { create: false });
        return;
      }
      const b = (await resp.json()) as PrBody;
      patch(traceId, (prev) => ({ ...prev, prBody: b, prBodyMissing: false }), { create: false });
    } catch {
      // Fail-soft: the trace stays fully usable without its PR description.
      patch(traceId, (prev) => ({ ...prev, prBodyMissing: true }), { create: false });
    } finally {
      prBodyInFlight.delete(traceId);
    }
  }

  async function ensure(traceId: string): Promise<void> {
    const before = read(traceId);
    const streaming = before?.stream === 'streaming';
    const enrich = before?.enrich ?? 'idle';
    if (!streaming && (enrich === 'idle' || enrich === 'error')) {
      await fetchTrace(traceId);
    }
    const after = read(traceId);
    if (
      after?.decision?.action === 'iac_apply' &&
      after.prBody == null &&
      !after.prBodyMissing &&
      !prBodyInFlight.has(traceId)
    ) {
      await fetchPrBody(traceId);
    }
  }

  async function retry(traceId: string): Promise<void> {
    patch(traceId, (prev) => ({
      ...prev,
      enrich: prev.enrich === 'error' ? 'idle' : prev.enrich,
      // A retry is the operator asking again for everything that failed, and
      // the pr-body's absence is one of the things they can see.
      prBodyMissing: false,
    }));
    await ensure(traceId);
  }

  return { subscribe, beginLive, appendLive, endLive, settleBackfill, ensure, retry };
}
