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
}

export interface OverviewStore extends Readable<OverviewState> {
  /**
   * Fetch all three endpoints in parallel; each fails independently (a
   * failed fetch preserves its prior value in state). Overlapping calls
   * collapse into the in-flight cycle (single-flight — see the guard below);
   * `reason` is a caller-supplied trigger label, carried for future
   * diagnostics only (never branched on, never rendered).
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

  async function fetchPendingList(): Promise<FetchResult<PendingApproval[]>> {
    try {
      const resp = await call('/infra/pending-approvals');
      if (!resp.ok) return { ok: false };
      const body = await resp.json();
      if (!Array.isArray(body?.approvals)) return { ok: false };
      return { ok: true, value: body.approvals as PendingApproval[] };
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

  async function refresh(_reason?: string): Promise<void> {
    if (inFlight) return; // collapse: a cycle is already running, this trigger rides it
    inFlight = true;
    const my = ++seq;
    try {
      const [g, p, d] = await Promise.all([fetchGraph(), fetchPendingList(), fetchDecisionsList()]);
      if (my !== seq) return; // defensive; unreachable while inFlight guards overlap
      update((state) => ({
        graph: g.ok ? g.value : state.graph,
        pendingApprovals: p.ok ? p.value : state.pendingApprovals,
        decisions: d.ok ? d.value : state.decisions,
        lastError: !g.ok ? 'graph' : !p.ok ? 'pending' : !d.ok ? 'decisions' : null,
      }));
    } finally {
      inFlight = false;
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
  const scheduler = new RefreshScheduler({ onFetch: () => void refresh('poll') });

  // Store creation trigger: fire the first refresh explicitly (rather than
  // relying solely on scheduler.open()'s own immediate-fetch side effect) so
  // this line reads as its own named trigger. scheduler.open(0) below ALSO
  // fires an immediate onFetch per its own contract (epoch 0 === its initial
  // lastHandledEpoch) — that call lands after `inFlight` is already true (set
  // synchronously by the line above, before this function's first await), so
  // it collapses into the cycle just started instead of double-fetching.
  void refresh('create');
  scheduler.open(0); // also starts the poll interval (default 45s)

  function destroy(): void {
    window.removeEventListener('focus', onFocus);
    document.removeEventListener('visibilitychange', onFocus);
    scheduler.destroy();
  }

  return { subscribe, refresh, destroy };
}
