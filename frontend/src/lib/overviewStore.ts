// overviewStore.ts — single owner of the desk/estate refresh triple: the infra
// graph, the open infra PRs awaiting approval, and the decisions log. Today
// App.svelte fetches decisions and InfraDiagram.svelte independently fetches
// graph + pending-approvals; the desk (Task 3.1+) and the estate view need the
// same three snapshots and must not race three separate fetchers to build
// them. This store owns exactly one copy of each, refreshed on a shared set
// of triggers, so every consumer reads one consistent snapshot.
//
// Factory-pattern store (createOverviewStore(call)), same shape as
// autonomyStore.ts / pauseStore.ts, instantiated once in App.svelte. Unlike
// those two, this store fetches EAGERLY on creation (see "store creation" in
// the trigger list below) — it doesn't wait for an explicit first call — so
// desk/estate render with data on their very first paint instead of an empty
// flash-then-fill.
//
// NOT rewired this phase: InfraDiagram.svelte keeps its own internal graph +
// pending-approvals fetching for the chat view. One redundant fetch pair
// exists for an operator who visits both views in one session — accepted for
// now, noted for post-pitch cleanup.
//
// Scope, precisely: only the `graph` and `pendingApprovals` slices are
// desk/estate-specific. `decisions` is consumed APP-WIDE and is NOT safe to
// treat as view-scoped — App.svelte renders <DecisionsRail> outside the view
// branch (visible on chat too), and, less obviously, the decisions payload
// drives noteApplied() -> appliedEpoch, which the CHAT view's <InfraDiagram>
// reads to trigger its post-apply CAI-lag ride-out. So this store is already
// load-bearing for chat's refresh timing: gating or removing it as a
// "desk/estate thing" would silently break the chat resource map's refresh
// after an apply lands.
import { writable, type Readable } from 'svelte/store';
import type { InfraGraph, PendingApproval } from './infra_graph';
import type { Decision } from './types';
import { RefreshScheduler } from './infra_refresh';

/** Which of the three GETs most recently failed (soft-fail — see OverviewState.lastError). */
export type OverviewFetchKind = 'graph' | 'pending' | 'decisions';

export interface OverviewState {
  graph: InfraGraph | null;
  pendingApprovals: PendingApproval[];
  /**
   * `NO_DECISIONS_YET` until the FIRST fetch (success or failure) settles —
   * consumers that need to key boot-seed logic off "have we ever loaded a
   * real payload" (App.svelte's noteApplied/appliedWatermark; see
   * lib/decision.ts's boot-seed comment) can reference-check against that
   * exported sentinel. A genuine empty-array response from the server is
   * ALWAYS a freshly-parsed array (JSON.parse never returns this exact
   * object), so the identity check can't be confused by a real "no
   * decisions yet" answer.
   */
  decisions: Decision[];
  /**
   * The kind of the most recent failure in the LAST COMPLETED refresh cycle,
   * or null when that cycle's three fetches all succeeded. Deliberately a
   * single field, not a per-kind error set: today's only consumer is a "last
   * scan" / staleness note, not a per-panel error banner. When more than one
   * fetch fails in the same cycle, graph wins the priority (it's the
   * slowest/most failure-prone of the three — CAI-backed, 10-30s cold — so
   * it's the one worth surfacing first), then pending, then decisions.
   */
  lastError: OverviewFetchKind | null;
  /**
   * False until the FIRST refresh cycle completes (success or failure); true
   * forever after. ds-eh6 — consumers must be able to tell "we have not looked
   * yet" from "we looked and there is nothing", because rendering the second
   * while the first is true is a confident false claim. The desk keys its
   * `unknown`/`loading` state off this.
   *
   * Deliberately separate from the `NO_DECISIONS_YET` sentinel above, which
   * answers the narrower question the boot-seed watermark asks ("has the
   * DECISIONS payload ever landed"). This one covers the whole cycle, so a
   * consumer reading `pendingApprovals` is not forced to infer its load state
   * from a sibling slice.
   */
  settled: boolean;
  /**
   * The last completed cycle could not get a trustworthy answer about pending
   * work — either `/decisions` or `/infra/pending-approvals` failed outright,
   * OR pending-approvals soft-failed to `{approvals: [], degraded: true}`
   * (agent/main.py:3531, its documented GitHub-outage behavior).
   *
   * That soft-fail is why this is not simply derived from `lastError`: it
   * arrives as a 200 with a well-formed empty list, so every ok/Array.isArray
   * check passes and the failure is invisible unless the flag is read. An
   * empty list that means "GitHub is down" and one that means "nothing is
   * pending" are the same bytes apart from this bit.
   *
   * Scoped to the two desk lanes ON PURPOSE: a `/infra/graph` failure does not
   * set it. Graph feeds the estate view, and it is the routinely-slow endpoint
   * — letting it flip this would put the desk in a degraded state during
   * ordinary cold starts.
   */
  degraded: boolean;
}

export interface OverviewStore extends Readable<OverviewState> {
  /**
   * Fetch all three endpoints in parallel; each fails independently (a
   * failed fetch preserves its prior value in state). Overlapping calls
   * collapse into the in-flight cycle (single-flight — see the guard below)
   * and are then served by ONE trailing cycle, so the returned promise always
   * resolves against state that reflects the caller's own trigger. `reason` is
   * a caller-supplied trigger label, carried for future diagnostics only
   * (never branched on, never rendered).
   */
  refresh(reason?: string): Promise<void>;
  /** Removes the focus/visibilitychange listeners and stops the poll timer. Call once, on App teardown. */
  destroy(): void;
}

/** See OverviewState.decisions. Exported so consumers can reference-compare. */
export const NO_DECISIONS_YET: Decision[] = [];

const INITIAL: OverviewState = {
  graph: null,
  pendingApprovals: [],
  decisions: NO_DECISIONS_YET,
  lastError: null,
  settled: false,
  degraded: false,
};

type FetchResult<T> = { ok: true; value: T } | { ok: false };

export function createOverviewStore(
  call: (path: string, init?: RequestInit) => Promise<Response>,
): OverviewStore {
  const { subscribe, update } = writable<OverviewState>({ ...INITIAL });

  // Single-flight + monotonic-seq stale guard, ported from autonomyStore.ts's
  // fetchAutonomy/confirm pairing: `inFlight` collapses an overlapping
  // refresh() into the cycle already running (mirrors InfraDiagram's
  // `refreshInFlight`); `seq` is the defense-in-depth staleness check applied
  // when the cycle's fetches finally settle — belt-and-braces, since inFlight
  // alone already makes two overlapping cycles impossible in practice.
  let inFlight = false;
  let seq = 0;

  // ds-6pi — TRAILING single-flight. A plain `if (inFlight) return` discards the
  // collapsed trigger, which is only sound when the running cycle's GETs were
  // issued AFTER that trigger. They frequently aren't: /infra/graph is CAI-backed
  // and 10-30s cold, and all three commit together after Promise.all, so a chat
  // turn that lands mid-cycle collapsed into a cycle whose /decisions read
  // predated its own new decision — and nothing retried before the next 45s poll.
  // Three of App's four `refresh('chat-turn')` call sites AWAIT this, so they
  // resumed on state that provably could not contain what they were waiting for.
  //
  // So: a collapse earns exactly one more cycle, and the collapsed caller's
  // promise resolves when THAT cycle lands. Bounded — collapses during the
  // trailing cycle earn the next one, never more than one outstanding.
  interface Deferred {
    promise: Promise<void>;
    resolve: () => void;
  }
  let pending: Deferred | null = null;

  function deferred(): Deferred {
    let resolve!: () => void;
    const promise = new Promise<void>((r) => {
      resolve = r;
    });
    return { promise, resolve };
  }

  async function fetchGraph(): Promise<FetchResult<InfraGraph>> {
    try {
      const resp = await call('/infra/graph');
      if (!resp.ok) return { ok: false };
      const body = (await resp.json()) as InfraGraph;
      return { ok: true, value: body };
    } catch {
      return { ok: false };
    }
  }

  // Returns the list AND whether the backend admitted it was guessing. The
  // `degraded` bit rides alongside rather than collapsing into `ok: false`:
  // the payload IS usable (it is a well-formed, possibly-partial list, and
  // overwriting the prior value with it is still correct), it just cannot be
  // read as authoritative about absence. See OverviewState.degraded.
  async function fetchPendingList(): Promise<
    FetchResult<{ approvals: PendingApproval[]; degraded: boolean }>
  > {
    try {
      const resp = await call('/infra/pending-approvals');
      if (!resp.ok) return { ok: false };
      const body = await resp.json();
      if (!Array.isArray(body?.approvals)) return { ok: false };
      return {
        ok: true,
        value: {
          approvals: body.approvals as PendingApproval[],
          degraded: body.degraded === true,
        },
      };
    } catch {
      return { ok: false };
    }
  }

  async function fetchDecisionsList(): Promise<FetchResult<Decision[]>> {
    try {
      const resp = await call('/decisions?limit=50');
      if (!resp.ok) return { ok: false };
      const body = await resp.json();
      if (!Array.isArray(body?.decisions)) return { ok: false };
      return { ok: true, value: body.decisions as Decision[] };
    } catch {
      return { ok: false };
    }
  }

  async function runCycle(): Promise<void> {
    const my = ++seq;
    const [g, p, d] = await Promise.all([fetchGraph(), fetchPendingList(), fetchDecisionsList()]);
    if (my !== seq) return; // defensive; unreachable while inFlight guards overlap
    update((state) => ({
      graph: g.ok ? g.value : state.graph,
      pendingApprovals: p.ok ? p.value.approvals : state.pendingApprovals,
      decisions: d.ok ? d.value : state.decisions,
      lastError: !g.ok ? 'graph' : !p.ok ? 'pending' : !d.ok ? 'decisions' : null,
      // Set once the first cycle completes, whatever the outcome — a cycle in
      // which everything failed still answers "have we looked yet" with yes.
      // That is why this is not `g.ok && p.ok && d.ok`: a total outage would
      // otherwise pin the desk on `loading` forever, promising a resolution
      // that is not coming. A failed look is reported as `degraded` below.
      settled: true,
      // Recomputed per cycle, never latched: a recovered fetch must clear it.
      // Only the two lanes the desk reads — a graph failure is deliberately
      // not a desk-degraded signal (see OverviewState.degraded).
      degraded: !p.ok || !d.ok || (p.ok && p.value.degraded),
    }));
  }

  async function refresh(_reason?: string): Promise<void> {
    // Collapse — but hand the caller a promise for the trailing cycle that will
    // actually cover their trigger, rather than resolving against this one.
    if (inFlight) {
      pending ??= deferred();
      return pending.promise;
    }
    inFlight = true;
    try {
      await runCycle();
      // Null the deferred BEFORE its cycle runs, so a collapse arriving during
      // the trailing cycle earns the cycle after it instead of being folded into
      // one it also predates. That detaching is also why the resolve needs its
      // own finally: `waiter` is no longer reachable from `pending`, so the
      // outer handler below cannot see it, and a throw here would leave an
      // awaited refresh('chat-turn') hanging forever. The outer comment used to
      // claim it covered this; it did not.
      while (pending) {
        const waiter = pending;
        pending = null;
        try {
          await runCycle();
        } finally {
          waiter.resolve();
        }
      }
    } finally {
      inFlight = false;
      // Same guarantee for a waiter that never got its cycle started, e.g. the
      // FIRST runCycle above threw. Unreachable today (every fetch is internally
      // try/caught and returns {ok:false}) — but "unreachable" is what the
      // comment said about the case directly above, too.
      if (pending) {
        const stranded = pending;
        pending = null;
        stranded.resolve();
      }
    }
  }

  // Focus / visibilitychange→visible trigger (Codex blocker fix): this is
  // what makes the stamped desk state appear promptly after the operator
  // returns from an approval page in another tab, instead of waiting out the
  // next poll tick. One handler for both events, gated on visibilityState —
  // mirrors InfraDiagram.svelte's onFocus (a `focus` without visibility can
  // fire on some platforms while the tab is still occluded).
  function onFocus(): void {
    if (document.visibilityState === 'visible') void refresh('focus');
  }
  window.addEventListener('focus', onFocus);
  document.addEventListener('visibilitychange', onFocus);

  // Light polling only — this store has no "apply observed" concept of its
  // own (that ladder-ride-out stays owned by App's appliedEpoch/InfraDiagram
  // pairing; see lib/decision.ts), so RefreshScheduler is used purely for its
  // interval timer via open(), never via onAppliedEpoch()/onFocus() (which
  // would additionally schedule the 0/10/30/60s ride-out ladder — the wrong
  // shape for a plain "tab got focus" trigger, handled directly above instead).
  // The creation trigger IS the scheduler's immediate fetch — `open(0)` takes
  // the else branch of its epoch check (0 === its initial lastHandledEpoch) and
  // calls onFetch synchronously. There used to be an explicit `refresh('create')`
  // above this as well, on the reasoning that the second call would harmlessly
  // collapse into the first. That was free only while a collapsed trigger was
  // DISCARDED; now that a collapse earns a trailing cycle (ds-6pi), a redundant
  // startup trigger would cost every visitor a second full triple-fetch on first
  // paint — including the CAI-backed graph and the /decisions read. One trigger,
  // labelled for what it is on its first firing.
  let started = false;
  const scheduler = new RefreshScheduler({
    onFetch: () => {
      void refresh(started ? 'poll' : 'create');
      started = true;
    },
  });
  scheduler.open(0); // fires the creation fetch; also starts the poll interval (default 45s)

  function destroy(): void {
    window.removeEventListener('focus', onFocus);
    document.removeEventListener('visibilitychange', onFocus);
    scheduler.destroy();
  }

  return { subscribe, refresh, destroy };
}
