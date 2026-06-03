// infra_refresh.ts — a pure, framework-agnostic refresh scheduler for the
// Infrastructure panel.
//
// It owns the timer/epoch logic — the part the review flagged as the most
// regression-prone and previously untested — so it can be unit-tested with fake
// timers, independent of Svelte component rendering (which this repo's vitest
// setup doesn't support). The component (InfraDiagram.svelte) keeps only the
// view + the async fetch/render concurrency guards and drives this scheduler.
//
// Triggers it encapsulates (design §3 / §5 Phase 1):
//  - expand → one fetch (or, if an apply was observed while collapsed, the
//    CAI-lag ride-out ladder instead);
//  - an `applied` iac_apply observed while OPEN → the ride-out ladder; while
//    CLOSED → deferred, then ridden out on the next expand;
//  - focus while open → the ride-out ladder;
//  - light polling while open;
//  - full teardown on collapse / destroy.

export interface RefreshSchedulerOptions {
  /** Invoked to perform one /infra/graph fetch (+ render when open). */
  onFetch: () => void;
  /** Light-poll interval while open. Default 45s. */
  pollMs?: number;
  /** CAI-lag ride-out offsets after an observed apply. Default 0/10/30/60s. */
  applyDelays?: number[];
}

const DEFAULT_POLL_MS = 45_000;
const DEFAULT_APPLY_DELAYS = [0, 10_000, 30_000, 60_000];

export class RefreshScheduler {
  private readonly onFetch: () => void;
  private readonly pollMs: number;
  private readonly applyDelays: number[];

  private opened = false;
  // The last appliedEpoch we acted on. Starts at 0 (the prop's initial value);
  // an apply observed while CLOSED is deliberately left unhandled here so the
  // next open() rides it out (riding out CAI lag the operator can actually see).
  private lastHandledEpoch = 0;
  private timers: ReturnType<typeof setTimeout>[] = [];
  private pollTimer: ReturnType<typeof setInterval> | null = null;

  constructor(opts: RefreshSchedulerOptions) {
    this.onFetch = opts.onFetch;
    this.pollMs = opts.pollMs ?? DEFAULT_POLL_MS;
    this.applyDelays = opts.applyDelays ?? DEFAULT_APPLY_DELAYS;
  }

  get isOpen(): boolean {
    return this.opened;
  }

  /** Panel expanded. Rides out the ladder if an apply landed while collapsed,
   *  else a single fetch; starts light polling either way. */
  open(epoch: number): void {
    this.opened = true;
    this.startPolling();
    if (epoch !== this.lastHandledEpoch) {
      this.lastHandledEpoch = epoch;
      this.scheduleApplyRefetches();
    } else {
      this.onFetch();
    }
  }

  /** Panel collapsed — stop everything (a closed panel re-fetches on re-open). */
  close(): void {
    this.opened = false;
    this.clearTimers();
    this.stopPolling();
  }

  /** Parent observed an `applied` iac_apply (appliedEpoch bumped). While open →
   *  ride out the ladder; while closed → defer to the next open(). */
  onAppliedEpoch(epoch: number): void {
    if (epoch === this.lastHandledEpoch) return;
    if (this.opened) {
      this.lastHandledEpoch = epoch;
      this.scheduleApplyRefetches();
    }
    // closed: leave lastHandledEpoch unchanged so open() picks it up.
  }

  /** Tab regained focus (the caller checks document.visibilityState). Ride out
   *  the ladder so a focus landing mid-CAI-lag still converges. */
  onFocus(): void {
    if (this.opened) this.scheduleApplyRefetches();
  }

  /** Component destroyed — release every timer. */
  destroy(): void {
    this.clearTimers();
    this.stopPolling();
  }

  private scheduleApplyRefetches(): void {
    this.clearTimers();
    for (const delay of this.applyDelays) {
      const id = setTimeout(() => {
        if (this.opened) this.onFetch();
      }, delay);
      this.timers.push(id);
    }
  }

  private startPolling(): void {
    this.stopPolling();
    this.pollTimer = setInterval(() => {
      if (this.opened) this.onFetch();
    }, this.pollMs);
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  private clearTimers(): void {
    for (const id of this.timers) clearTimeout(id);
    this.timers = [];
  }
}
