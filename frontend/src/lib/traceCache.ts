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
  /** The last snapshot's `TraceResponse.complete`. Meaningful only once
   *  `enrich === 'loaded'`.
   *
   *  `complete: false` is ROUTINE, not exceptional: a cold post-restart
   *  observation cache returns it on a single fetch, and iac_apply traces never
   *  carry a final_response (App.svelte's openTrace documents both). So a
   *  `loaded` entry is NOT proof we hold the whole trace — without this flag an
   *  entry that settled from a too-early snapshot would be pinned to it for its
   *  whole cache lifetime, since ensure() re-fetches only 'idle'/'error'. In the
   *  worst case that shows "no reasoning recorded" forever for a trace whose
   *  rows simply had not landed in Cloud Logging yet.
   *
   *  It gates a RE-fetch, never a status label — deriving user-visible state
   *  from it would mislabel the routine cases above. */
  complete: boolean;
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
  /** Operator-driven recovery: an explicit "ask again" that ALWAYS re-fetches a
   *  settled entry, whatever state it is in. Anything narrower makes the retry
   *  button a no-op in exactly the states an operator would press it. */
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
    complete: false,
  };
}

/** Never let a re-fetch SHRINK what is already on screen. GET /trace accumulates
 *  as Cloud Logging ingestion catches up, so a shorter snapshot means a colder
 *  backend cache, not a corrected record — blanking rows the operator is reading
 *  would be a regression wearing a refresh's clothes. Equal lengths take the
 *  fetched copy: same size, newer read. */
function preferLonger(prev: TraceEvent[], fetched: TraceEvent[]): TraceEvent[] {
  return fetched.length >= prev.length ? fetched : prev;
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
      // Non-destructive, same rule as fetchTrace: an expand's ensure() can have
      // landed a decision doc first, and this snapshot arriving without one is
      // an absence of news, not news of an absence.
      decision: fetched.decision ?? prev.decision ?? null,
      enrich: 'loaded',
      complete: fetched.complete === true,
    }));
  }

  /** Report a failed /trace — but never over the top of a successful one.
   *
   *  Two writers can be in flight at once: App fires its post-`done` backfill
   *  in the BACKGROUND (it does not route through ensure(), because it also
   *  owns the global timeline write), while the operator expands the
   *  disclosure and ensure() starts its own request. If the racing request
   *  loses, the entry demonstrably HAS its trace — the events and decision are
   *  already on screen — so claiming "couldn't load" would be a visible lie
   *  with a retry button attached to it.
   *
   *  The same reasoning covers a RE-fetch of an already-loaded entry (an
   *  incomplete snapshot looking again): that attempt sets 'loading' over its
   *  own 'loaded', so the guard below cannot see what it replaced — hence
   *  `fallback`, which the caller captures BEFORE overwriting the state. */
  function failEnrich(traceId: string, fallback: 'loaded' | 'error'): void {
    patch(traceId, (prev) => (prev.enrich === 'loaded' ? prev : { ...prev, enrich: fallback }));
  }

  async function fetchTrace(traceId: string): Promise<void> {
    // An entry that already holds a snapshot must survive a failed refresh as
    // 'loaded', not 'error': the events are still on screen, `complete` is still
    // false, and the next expand will simply try again.
    const fallback = read(traceId)?.enrich === 'loaded' ? 'loaded' : 'error';
    patch(traceId, (prev) => ({ ...prev, enrich: 'loading' }));
    try {
      const resp = await call('/trace/' + encodeURIComponent(traceId));
      if (!resp.ok) {
        failEnrich(traceId, fallback);
        return;
      }
      const t = (await resp.json()) as TraceResponse;
      const fetchedEvents = Array.isArray(t.events) ? t.events : [];
      patch(traceId, (prev) => ({
        ...prev,
        // An entry that never streamed has no live timeline to protect, so the
        // snapshot IS the truth — subject to preferLonger, which matters now
        // that an incomplete entry can be fetched more than once. One that did
        // stream keeps its live events and gains only the mcp side-channel (see
        // reconcileBackfill's contract).
        events:
          prev.stream === 'idle'
            ? preferLonger(prev.events, fetchedEvents)
            : reconcileBackfill(prev.events, fetchedEvents),
        // A re-fetch that came back without a decision doc must not erase the
        // one an earlier snapshot already carried — same non-destructive rule.
        decision: t.decision ?? prev.decision ?? null,
        enrich: 'loaded',
        complete: t.complete === true,
      }));
    } catch {
      failEnrich(traceId, fallback);
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
    // 'loaded' is not the end of the story when the snapshot said it was
    // incomplete: look again. ensure() runs only when the operator opens a
    // disclosure, so this is bounded by clicks — and preferLonger makes a colder
    // answer harmless. Without it a trace that settled early stays partial (or
    // reads "no reasoning recorded") for its whole cache lifetime.
    const stale = enrich === 'loaded' && before?.complete !== true;
    if (!streaming && (enrich === 'idle' || enrich === 'error' || stale)) {
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
      // Unconditional for anything not mid-stream. Resetting only 'error' left
      // retry a silent no-op on a 'loaded' entry — including the one case an
      // operator most wants it for, a settled-but-empty trace whose rows had not
      // been ingested yet. 'streaming' is the one state to leave alone: the
      // stream is authoritative and /trace has nothing to add yet.
      enrich: prev.stream === 'streaming' ? prev.enrich : 'idle',
      // A retry is the operator asking again for everything that failed, and
      // the pr-body's absence is one of the things they can see.
      prBodyMissing: false,
    }));
    await ensure(traceId);
  }

  return { subscribe, beginLive, appendLive, endLive, settleBackfill, ensure, retry };
}
