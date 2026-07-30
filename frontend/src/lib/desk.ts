// desk.ts — selection logic for the approval desk (Task 3.1). The desk is a
// three-state screen: PENDING (something needs the operator's approval right
// now), STAMPED (something was just resolved — a short-lived confirmation),
// or RESTING (nothing to do). This module decides which state applies and
// what it should point at; ApprovalDesk.svelte (Task 3.5) renders the result.
// It does not fetch, subscribe, or touch the DOM — callers pass in the same
// `graph`-adjacent slices `overviewStore.ts` (Task 3.0a) already holds
// (`decisions`, `pendingApprovals`), so the desk and the estate view read one
// consistent snapshot.
//
// Pure + fully time-injectable: every comparison takes `now` from the input
// (defaulting to Date.now() only at this one entry point), so decay/expiry
// boundaries are deterministic in tests without fake timers. `origin`/`locale`
// are likewise threaded through explicitly to approval.ts's helpers rather
// than reading `window`/an ambient locale store.

import {
  safeApprovalHref,
  iacApprovalHref,
  isRollbackAwaitingOperator,
  isIacAwaitingOperator,
  resolvedIacPrNumbers,
  supersededWaitingIds,
} from './approval';
import { isReplayableTraceId } from './deeplink';
import type { Decision } from './types';
import type { PendingApproval } from './infra_graph';

/** The stamped state's confirmation window: how long a just-resolved action
 *  stays on the desk before decaying to resting. The component schedules its
 *  own decay timer off `stampedUntil` (an absolute epoch-ms), so this constant
 *  only needs to be right once, here. */
export const STAMP_WINDOW_MS = 10 * 60 * 1000;

/** How long a rollback may sit in an in-flight phase (`claimed`/`applying`)
 *  before the desk stops calling it "in progress" and surfaces it as an
 *  unconfirmed outcome.
 *
 *  Comfortably past the worker's 60s LRO poll budget: inside that window
 *  /execute is still expected to settle the doc itself, so surfacing it would
 *  alarm the operator during a routine rollback.
 *
 *  Past it, we simply do not know — which is NOT the same as "nothing is coming
 *  back for it" (an earlier draft of this comment said that, and it was wrong).
 *  A long LRO can genuinely exceed five minutes, and the wired-up /reconcile may
 *  still settle it afterwards. That is exactly why the card says "unconfirmed"
 *  rather than "failed", and why it yields as soon as a terminal phase lands. */
export const STUCK_APPLYING_MS = 5 * 60 * 1000;

/** A pending rollback: a decision whose live approval doc is still awaiting
 *  the operator (or was never enriched — see `status` below). */
export interface DeskPendingRollback {
  kind: 'pending';
  source: 'rollback';
  /** The decision this CTA acts on — carries identity for the card's title. */
  decision: Decision;
  /** Same-origin relative href, already validated by `safeApprovalHref`. */
  href: string;
  /**
   * The reasoning trace that AUTHORED this proposal (ds-wd2.15), or null when
   * the row predates trace capture. Always `?reasoning=`-round-trippable —
   * gated on `isReplayableTraceId` so the desk can never offer a link that
   * opens once but fails to restore when shared.
   *
   * Sound on this arm specifically because a rollback decision is written BY
   * the proposing run (`_do_rollback` calls `record_decision` inside the agent
   * turn), so its `trace_id` really is the reasoning behind the proposal. The
   * iac arms do not have that property — see `DeskPendingIac.traceId`.
   */
  traceId: string | null;
}

/**
 * WHERE the desk learned a pending infra (IaC) approval exists — a
 * discriminated union so a `DeskPendingIac` can never be constructed
 * carrying neither. `listing`: the `/infra/pending-approvals` open-PR
 * payload (rule 2a) — carries the PR title the desk card wants. `decision`:
 * derived from the decisions log (rule 2b, `selectPendingIacFromDecisions`)
 * for a PR the open-PR listing can no longer see because it already merged —
 * see that function's header comment for why this fallback exists at all.
 */
export type DeskPendingIacProvenance =
  | { kind: 'listing'; approval: PendingApproval }
  | { kind: 'decision'; decision: Decision };

/** A pending infra (IaC) approval, from either provenance above. */
export interface DeskPendingIac {
  kind: 'pending';
  source: 'iac';
  /** Positive integer — validated by whichever of `iacApprovalHref`'s calls
   *  produced `href` below. */
  prNumber: number;
  /** Same-origin relative href, built by `iacApprovalHref`. */
  href: string;
  provenance: DeskPendingIacProvenance;
  /**
   * The reasoning run that AUTHORED this PR, or null when unknown.
   *
   * ⚠️ NEVER derived from the decision row. An `iac_apply` decision's `trace_id`
   * is NOT the authoring reasoning: `_record_iac_decision` stamps
   * `current_trace_id_or_new()` (agent/main.py ~5352) inside the **approve/apply
   * POST** — a different HTTP request from the crew run that authored the PR — so
   * linking "view the reasoning behind this" to it would point the operator at the
   * trace of their own approval click. Two tests pin that absence. Do NOT re-add a
   * `pr_number` join over `decisions` to fake it.
   *
   * Populated on the **listing** arm only, from the backend's
   * `PendingApproval.authoring_trace_id` (ds-qua) — a per-(repo, PR) record written
   * when the editor worker confirms a newly-opened PR, so it names the run that
   * really opened it. Gated by `isReplayableTraceId` like the rollback arm.
   *
   * Still null on the **decision** arm (rule 2b): `/decisions` carries no authoring
   * trace, and that arm renders a real diff table anyway, so it is not the one that
   * reads as evidence-free.
   */
  traceId: string | null;
}

export type DeskPending = DeskPendingRollback | DeskPendingIac;

/** Nothing is pending, but something resolved recently enough to still show a
 *  confirmation. No href/CTA — this is a receipt, not an action. */
export interface DeskStamped {
  kind: 'stamped';
  source: 'rollback' | 'iac';
  /** The decision that was just resolved (applied iac_apply row, or rollback
   *  decision whose approval.status is now 'used') — identity for the card. */
  decision: Decision;
  /** Absolute epoch-ms at which this stamp decays to resting. The component
   *  schedules its own `setTimeout` off this value. */
  stampedUntil: number;
}

/**
 * A rollback whose credential was spent but which did NOT demonstrably apply.
 *
 * Exists because "no seal" is not the same as "the operator was told". The
 * ledger strip cannot be relied on to carry this: `ledgerRows` orders by the
 * PROPOSAL's `created_at` and caps at four, so a rollback proposed twenty
 * minutes ago that fails right now is pushed out by four newer decisions and
 * vanishes — no seal, no row, hero renders resting. A failed rollback silently
 * disappearing is the same honesty failure as a fake seal, just quieter.
 *
 * `failed` and `outcome_unknown` are kept distinct all the way to the copy.
 * Collapsing them would re-introduce the original defect inverted: an
 * uncancelled operation that is still running, or one whose response we lost,
 * has NOT been shown to fail, and saying so would be a second false claim.
 */
export interface DeskUnresolved {
  kind: 'unresolved';
  source: 'rollback';
  decision: Decision;
  /** `failed` — definitely did not apply. `outcome_unknown` — may have applied;
   *  we could not confirm. Drives which of the two copy variants renders. */
  phase: 'failed' | 'outcome_unknown';
}

export interface DeskResting {
  kind: 'resting';
}

/**
 * We do not know whether anything needs the operator (ds-eh6).
 *
 * `resting` is a CLAIM — "nothing needs your decision right now" — and the
 * desk was making it in two states where it had no standing to: before the
 * first refresh cycle settles, and after a cycle in which a lane the desk
 * depends on failed. Both rendered as a confident all-clear, which is the
 * same class of dishonesty as a fake seal, just at the other end.
 *
 * The codebase already knew this pre-fetch state was dangerous: App.svelte
 * guards `noteApplied` on `ds !== NO_DECISIONS_YET` precisely so the applied
 * watermark is never seeded from placeholder data. The desk now honors the
 * same distinction.
 *
 * `loading` — no cycle has settled yet; this resolves on its own in seconds.
 * `degraded` — a cycle completed but `/decisions` or `/infra/pending-approvals`
 * did not answer usefully, so the snapshot may be missing a pending item.
 * They are kept distinct because the copy must differ: one asks for patience,
 * the other admits a gap.
 */
export interface DeskUnknown {
  kind: 'unknown';
  reason: 'loading' | 'degraded';
}

export type DeskModel = DeskPending | DeskStamped | DeskUnresolved | DeskResting | DeskUnknown;

export interface DeskModelInput {
  // Element type includes null/undefined: both arrays are open, externally-
  // sourced payloads (overviewStore.ts casts JSON.parse output straight to
  // `Decision[]` / `PendingApproval[]` — see its fetchDecisionsList /
  // fetchPendingList), so a malformed element is a real runtime possibility
  // the static element type doesn't rule out. Mirrors approval.ts's
  // `ReadonlyArray<... | null | undefined>` on the same Decision[] shape
  // (approval.ts:186-192).
  decisions: ReadonlyArray<Decision | null | undefined> | null | undefined;
  pendingApprovals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined;
  /** Injected clock (epoch-ms). Defaults to Date.now() — the only place in
   *  this module the ambient clock is read. */
  now?: number;
  /** Passed straight through to safeApprovalHref; defaults to
   *  window.location.origin there when omitted. */
  origin?: string;
  /** Passed straight through to safeApprovalHref / iacApprovalHref. */
  locale?: string;
  /**
   * Has the first refresh cycle completed (ds-eh6)? DEFAULTS TO TRUE, so
   * every existing caller and test keeps its current meaning — a caller that
   * genuinely knows about load state opts in by passing it. `false` suppresses
   * the `resting` all-clear in favour of `unknown`/`loading`; it never
   * suppresses a pending/unresolved/stamped result, because a card the desk
   * CAN prove is worth showing whether or not the rest of the snapshot landed.
   */
  settled?: boolean;
  /**
   * Did the last completed cycle fail to get a trustworthy answer from a lane
   * this desk depends on (ds-eh6)? Defaults to false. Same non-suppressing
   * semantics as `settled`.
   *
   * Deliberately a single boolean rather than the store's per-kind error
   * union: the desk depends on `/decisions` and `/infra/pending-approvals`
   * only. A `/infra/graph` failure must NOT set this — graph feeds the estate
   * view and the resting watch line, and treating it as a desk-lane failure
   * would fire the degraded state on the one endpoint that is routinely slow
   * (CAI-backed, 10-30s cold). Deciding which errors qualify is the caller's
   * job precisely because this module has no view of where they came from.
   */
  degraded?: boolean;
}

/** Parses an ISO timestamp to epoch-ms for ORDERING purposes only (picking
 *  the "newest" candidate among several that already independently qualify).
 *  A missing/unparseable value sorts as the oldest possible — it can still be
 *  picked if it's the only candidate, but never displaces a dated rival. This
 *  is deliberately more lenient than the stamped-window check below, which
 *  instead treats an unparseable timestamp as DISQUALIFYING (see
 *  `parseStrict`) — ordering among already-valid candidates tolerates a
 *  missing date; deciding whether something is fresh enough to stamp does not. */
function parseForOrdering(iso: string | null | undefined): number {
  if (!iso) return Number.NEGATIVE_INFINITY;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? Number.NEGATIVE_INFINITY : t;
}

/** Parses an ISO timestamp to epoch-ms, or null if absent/unparseable. Used
 *  for the stamped-window checks, where a missing timestamp must DISQUALIFY
 *  the candidate rather than merely sort it last (see `resolved_at: null`'s
 *  documented "genuinely unknown" semantics on DecisionApproval — it must
 *  never be treated as "just now"). */
function parseStrict(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/**
 * Rule 1: the newest (by `created_at`) rollback decision whose approval is
 * still actionable — approval_url present and same-origin-safe, status is
 * `'pending'` or absent (pre-enrichment rows never had a `status` field at
 * all, so absence must read as pending, not as "not a rollback"), and not
 * expired. A candidate that fails the safety check is skipped rather than
 * surfaced with a dead CTA — the next-newest candidate gets a chance instead.
 *
 * Rollback rows are identified by the PRESENCE of `approval.approval_url`,
 * never by `action === 'rollback'`. This is deliberate, not an oversight: the
 * coordinator's `_do_rollback` (agent/main.py:1128-1133) documents that a
 * rollback response carries `approval` in place of `github` — "the schema
 * divergence is intentional: `github` would be a lie here (no PR/issue was
 * opened)" — so no other decision shape ever carries `approval.approval_url`,
 * and gating on the field instead of the action string means a renamed or
 * variant rollback action can't silently drop a real pending approval. This
 * is the deliberate asymmetry with rule 3's iac lane below, which IS
 * action-gated (`action === 'iac_apply'`) — there, `apply_status`/
 * `applied_at` are generic open-shape fields that a non-iac row could in
 * principle also carry, so the action check is load-bearing there in a way
 * it would not be here.
 */
function selectPendingRollback(
  decisions: ReadonlyArray<Decision | null | undefined>,
  now: number,
  origin: string | undefined,
  locale: string | undefined,
): DeskPendingRollback | null {
  let best: { decision: Decision; href: string; ts: number } | null = null;
  for (const decision of decisions) {
    if (decision == null) continue; // defensive: malformed array element, skip not throw
    // isRollbackAwaitingOperator (approval.ts) is the single "is this row
    // actionable" predicate, shared with ledger.ts — see its doc comment.
    if (!isRollbackAwaitingOperator(decision, { now, origin })) continue;
    // Recompute WITH `locale` for the href this desk actually renders — the
    // predicate above already proved safeApprovalHref succeeds for this exact
    // URL/origin without `locale`, and `locale` only ever adds `?lang=ja`, so
    // this second call cannot newly fail (non-null asserted below).
    const href = safeApprovalHref(decision.approval!.approval_url!, origin, locale)!;
    const ts = parseForOrdering(decision.created_at);
    if (best === null || ts > best.ts) {
      best = { decision, href, ts };
    }
  }
  return best === null
    ? null
    : {
        kind: 'pending',
        source: 'rollback',
        decision: best.decision,
        href: best.href,
        traceId: replayableTraceId(best.decision),
      };
}

/** The decision's `trace_id` if it can round-trip a `?reasoning=` link, else
 *  null. See `DeskPendingIac.traceId` — null must render as no link. */
function replayableTraceId(decision: Decision | null | undefined): string | null {
  const id = decision?.trace_id;
  return isReplayableTraceId(id) ? id : null;
}


/**
 * Rule 2a: the first entry of the pending-approvals payload (the backend
 * already returns it newest-first; the DTO carries no timestamp of its own,
 * so no cross-kind recency claim is made here). Same skip-on-unsafe-href
 * fallthrough as rule 1, applied defensively — `PendingApproval.pr_number` is
 * typed as a plain number, so this only bites a malformed backend row.
 *
 * This listing is `agent/main.py`'s `_list_pending_approvals()` — GitHub
 * issues with `state="open"`. A PR that has already MERGED drops out of it
 * forever, even if it still genuinely needs the operator's post-merge,
 * post-rebake Apply. Rule 2b (`selectPendingIacFromDecisions`) below is the
 * fallback for exactly that gap; `deskModel` tries this listing FIRST, so
 * when both sources reference the same PR the listing wins (it carries the
 * PR title/body the desk card wants) purely by rule ordering — rule 2b never
 * runs at all once this one returns non-null.
 *
 * ds-0rm: this listing is CACHED backend-side for 60s
 * (`_PENDING_APPROVALS_TTL_S`, agent/main.py), so for up to a minute after a
 * PR merges and applies it still names that PR. Because this rule is tried
 * FIRST and originally skipped the resolved-PR check, a
 * stale cache entry outranked the seal that should have been showing — the
 * desk asked for a decision that had already been made. It now honors the
 * resolved-PR filter, so the two rules can never disagree about whether
 * a given PR is still open.
 */
function selectPendingIac(
  pendingApprovals: ReadonlyArray<PendingApproval | null | undefined>,
  decisions: ReadonlyArray<Decision | null | undefined>,
  resolvedPrs: ReadonlySet<number>,
  locale: string | undefined,
): DeskPendingIac | null {
  for (const approval of pendingApprovals) {
    if (approval == null) continue; // defensive: malformed array element, skip not throw
    if (resolvedPrs.has(approval.pr_number)) continue; // ds-0rm: already applied, stale cache row
    const href = iacApprovalHref(approval.pr_number, locale);
    if (href == null) continue; // malformed pr_number — never a dead CTA, try the next one
    return {
      kind: 'pending',
      source: 'iac',
      prNumber: approval.pr_number,
      href,
      provenance: { kind: 'listing', approval },
      // ds-qua. Same `isReplayableTraceId` gate as the rollback arm, so the desk can
      // never offer a link that opens once but fails to restore when shared. An older
      // coordinator omits the field entirely; absent and blank both read as null.
      traceId: isReplayableTraceId(approval.authoring_trace_id)
        ? approval.authoring_trace_id
        : null,
    };
  }
  return null;
}

/**
 * Rule 2b: when the open-PR listing (rule 2a) has no candidate, fall back to
 * scanning `decisions` for the newest `isIacAwaitingOperator` row (the same
 * predicate approval.ts's `iacApproveLabel` uses to decide the rail's own
 * "Review & approve →" CTA — see that predicate's doc comment for the full
 * gap this closes). Ties break by strict `>` (first-encountered wins),
 * mirroring rule 1's `selectPendingRollback` — deterministic, not
 * last-write-wins.
 *
 * `supersededIds` is computed once by the caller (`deskModel`) and threaded in,
 * mirroring ledger.ts's `ledgerRows` — it is an O(n) scan of the same `decisions`
 * list, so recomputing it per candidate here would make this function O(n^2) for
 * a set that never changes mid-scan. `resolvedPrs` is deliberately NOT passed: it
 * is PR-wide and belongs to the listing lane alone (ds-dzd — see approval.ts's
 * `resolvedIacPrNumbers`).
 */
function selectPendingIacFromDecisions(
  decisions: ReadonlyArray<Decision | null | undefined>,
  supersededIds: ReadonlySet<string>,
  locale: string | undefined,
): DeskPendingIac | null {
  let best: { decision: Decision; href: string; prNumber: number; ts: number } | null = null;
  for (const decision of decisions) {
    if (decision == null) continue; // defensive: malformed array element, skip not throw
    if (!isIacAwaitingOperator(decision, supersededIds)) continue;
    const href = iacApprovalHref(decision.pr_number, locale);
    if (href == null) continue; // malformed/missing pr_number — never a dead CTA, try the next one
    const ts = parseForOrdering(decision.created_at);
    if (best === null || ts > best.ts) {
      best = { decision, href, prNumber: decision.pr_number as number, ts };
    }
  }
  return best === null
    ? null
    : {
        kind: 'pending',
        source: 'iac',
        prNumber: best.prNumber,
        href: best.href,
        provenance: { kind: 'decision', decision: best.decision },
        // NOT best.decision.trace_id: an iac_apply row is written by the
        // approve POST, not the run that authored the PR. See the field doc.
        traceId: null,
      };
}

/**
 * Rule 3: nothing is pending, but something resolved within the last
 * STAMP_WINDOW_MS. Two independent candidate lanes:
 *  - iac: the newest `action === 'iac_apply'` row with `apply_status ===
 *    'applied'`, keyed off `applied_at` (NOT `created_at` — a merge-only
 *    reconcile rewrites created_at but carries the original applied_at
 *    forward, so keying off created_at would re-stamp a stale apply).
 *  - rollback: the newest decision whose `approval.status === 'used'`, keyed
 *    off `approval.resolved_at`. A null/absent resolved_at means "resolved,
 *    but we don't know when" and must NEVER be treated as "just now" — such a
 *    row is simply not a stamped candidate.
 * If both lanes have a within-window candidate, the more recently resolved
 * one wins (the freshest "you just did this" signal for the operator) — the
 * task text doesn't rank the two kinds against each other, so recency is the
 * tiebreak.
 */
function selectStamped(
  decisions: ReadonlyArray<Decision | null | undefined>,
  now: number,
): DeskStamped | null {
  let bestIac: { decision: Decision; ts: number } | null = null;
  let bestRollback: { decision: Decision; ts: number } | null = null;

  for (const decision of decisions) {
    if (decision == null) continue; // defensive: malformed array element, skip not throw
    if (decision.action === 'iac_apply' && decision.apply_status === 'applied') {
      const ts = parseStrict(decision.applied_at);
      if (ts !== null && (bestIac === null || ts > bestIac.ts)) {
        bestIac = { decision, ts };
      }
    }
    const approval = decision.approval;
    // `status === 'used'` alone is NOT evidence the rollback happened — the
    // credential flip precedes the traffic shift by construction, so a `used`
    // approval covers "applied", "still applying", "failed", and "we couldn't
    // tell" alike. Sealing on it certified rollbacks that never ran (ds-2mc).
    // Only a confirmed `applied` may seal; an absent phase (a pre-ds-2mc doc)
    // is unknown, and unknown never seals.
    if (approval?.status === 'used' && approval.phase === 'applied') {
      const ts = parseStrict(approval.resolved_at);
      if (ts !== null && (bestRollback === null || ts > bestRollback.ts)) {
        bestRollback = { decision, ts };
      }
    }
  }

  const iacUntil = bestIac === null ? null : bestIac.ts + STAMP_WINDOW_MS;
  const rollbackUntil = bestRollback === null ? null : bestRollback.ts + STAMP_WINDOW_MS;
  const iacValid = iacUntil !== null && now <= iacUntil;
  const rollbackValid = rollbackUntil !== null && now <= rollbackUntil;

  if (iacValid && rollbackValid) {
    return bestIac!.ts >= bestRollback!.ts
      ? { kind: 'stamped', source: 'iac', decision: bestIac!.decision, stampedUntil: iacUntil! }
      : { kind: 'stamped', source: 'rollback', decision: bestRollback!.decision, stampedUntil: rollbackUntil! };
  }
  if (iacValid) return { kind: 'stamped', source: 'iac', decision: bestIac!.decision, stampedUntil: iacUntil! };
  if (rollbackValid) {
    return { kind: 'stamped', source: 'rollback', decision: bestRollback!.decision, stampedUntil: rollbackUntil! };
  }
  return null;
}

/**
 * Rule 2.5: a rollback whose credential was spent but which did not
 * demonstrably apply — `failed`, or `outcome_unknown`.
 *
 * Unlike the stamped lane this has NO window and never decays. A stamp is a
 * receipt whose job is done once the operator has seen it; an unresolved
 * outcome is an open loop, and timing it out would just re-create the silent
 * disappearance this rule exists to prevent. It clears when the underlying
 * doc does — `/reconcile` promoting it to `applied`, or a new proposal.
 *
 * ds-mnf reviewed this as a DECISION rather than leaving it a discovery, since
 * during a public demo window one slow rollback parks "unconfirmed" on every
 * visitor's front door. Kept as-is, deliberately. The exits are real and
 * bounded — `/reconcile` promotes the row as soon as the LRO settles, a later
 * successful rollback supersedes it, and it leaves the served window after 50
 * decisions — while every alternative trades away the product's actual claim:
 * a desk that certifies only what it can prove. An operator dismissal would be
 * worst of all here, because the demo seat IS the visitor seat (see the
 * operator-seat window decision), so "dismiss" would hand an anonymous judge a
 * button that hides the one state we most want them to see us admit to.
 *
 * The compounding risk the review raised — ds-z4z making `outcome_unknown`
 * routine for any >60s LRO — was removed at its source rather than papered
 * over here: the approve POST now renders the recorded phase instead of an
 * outage page, so an unresolved card is the honest tail of a flow the operator
 * has already been told about, not a surprise.
 *
 * Ordered by `phase_at` where available, falling back to the decision's
 * `created_at`: `resolved_at` deliberately does not exist on these rows (it
 * means confirmed success only), so it cannot serve as the ordering key.
 */
function selectUnresolvedRollback(
  decisions: ReadonlyArray<Decision | null | undefined>,
  now: number,
): DeskUnresolved | null {
  let best: {
    decision: Decision;
    phase: 'failed' | 'outcome_unknown';
    observed: number;
    attempt: number;
  } | null = null;
  // The newest CONFIRMED rollback, keyed by ATTEMPT time. A later success
  // supersedes an earlier failure: once a subsequent rollback demonstrably
  // applied, the old failed attempt is history, not an open loop. Without this
  // the desk pins itself to an ancient failure forever — a new proposal only
  // hides it while pending, and it re-wins over the new success's own seal.
  let newestAppliedAttempt = -Infinity;

  for (const decision of decisions) {
    if (decision == null) continue; // defensive: malformed array element, skip not throw
    const approval = decision.approval;
    if (approval?.status !== 'used') continue;

    // TWO different clocks, and conflating them hides real failures.
    //   attempt  — when this rollback was proposed. Attempt chronology.
    //   observed — when its current phase was recorded. Observation order.
    // They diverge whenever /reconcile settles something late: a rollback that
    // succeeded at 10:00 but was only confirmed at 10:07 carries phase_at
    // 10:07. Ordering supersession by that would let it "post-date" — and
    // therefore silently bury — a DIFFERENT rollback that genuinely failed at
    // 10:06. Supersession is a question about which attempt came last, so it
    // uses `attempt`; picking among unresolved rows is a question about what
    // we most recently learned, so that uses `observed`.
    const attempt = parseForOrdering(decision.created_at);
    const observed = parseStrict(approval.phase_at) ?? attempt;

    if (approval.phase === 'applied') {
      if (attempt > newestAppliedAttempt) newestAppliedAttempt = attempt;
      continue;
    }

    let phase: 'failed' | 'outcome_unknown' | null = null;
    if (approval.phase === 'failed' || approval.phase === 'outcome_unknown') {
      phase = approval.phase;
    } else if (
      (approval.phase === 'applying' || approval.phase === 'claimed') &&
      now - observed > STUCK_APPLYING_MS
    ) {
      // Both in-flight phases go stale the same way, and BOTH must surface.
      //
      // `applying` is the ordinary case: /execute usually settles it inside its
      // own poll budget, so showing a card during a routine rollback would be
      // its own dishonesty — hence the age gate rather than an immediate one.
      //
      // `claimed` is the nastier one. It means the credential was burned and we
      // have no operation handle, which happens when the worker dies between
      // the claim and the handle write — INCLUDING the window where
      // update_service already accepted the traffic change. Nothing can
      // reconcile it (there is nothing to look up), so without this branch a
      // burned approval whose rollback may well have applied would sit
      // permanently invisible and the desk would render resting. That is
      // precisely the silent `used` this whole change exists to abolish.
      //
      // Neither reports as failure: in both cases the rollback may have
      // happened. `outcome_unknown` sends the operator to Cloud Run, which is
      // the only honest instruction available.
      phase = 'outcome_unknown';
    }
    if (phase === null) continue;

    if (best === null || observed > best.observed) {
      best = { decision, phase, observed, attempt };
    }
  }

  if (best === null || best.attempt < newestAppliedAttempt) return null;
  return { kind: 'unresolved', source: 'rollback', decision: best.decision, phase: best.phase };
}

/**
 * Selects which desk state applies, in strict priority order:
 * pending rollback (rule 1) > pending iac approval, listing-first then
 * decisions-derived (rule 2a, then rule 2b) > unresolved rollback outcome
 * (rule 2.5) > stamped (rule 3) > resting (rule 4). See the per-rule helpers
 * above for each rule's exact criteria; this function only sequences them.
 *
 * Rule 2.5 sits below the pending rules because something awaiting a decision
 * outranks something already decided — but ABOVE stamped, because a success
 * seal is precisely what an operator would otherwise read as "all good" while
 * a failed rollback sat unmentioned underneath it.
 */
export function deskModel(input: DeskModelInput): DeskModel {
  const now = input.now ?? Date.now();
  const decisions = input.decisions ?? [];
  const pendingApprovals = input.pendingApprovals ?? [];

  const rollback = selectPendingRollback(decisions, now, input.origin, input.locale);
  if (rollback) return rollback;

  // Computed ONCE and threaded into both iac rules. Rule 2a used to skip this
  // filter entirely (ds-0rm); sharing the set is what makes the two rules
  // structurally unable to disagree about which PRs are still open, rather
  // than merely happening to agree.
  const resolvedPrs = resolvedIacPrNumbers(decisions);
  const supersededIds = supersededWaitingIds(decisions);

  const iacFromListing = selectPendingIac(pendingApprovals, decisions, resolvedPrs, input.locale);
  if (iacFromListing) return iacFromListing;

  const iacFromDecisions = selectPendingIacFromDecisions(decisions, supersededIds, input.locale);
  if (iacFromDecisions) return iacFromDecisions;

  const unresolved = selectUnresolvedRollback(decisions, now);
  if (unresolved) return unresolved;

  const stamped = selectStamped(decisions, now);
  if (stamped) return stamped;

  // Everything above is something the desk can PROVE from the snapshot it
  // holds, so each is returned regardless of load state. Only the all-clear
  // is a claim about the ABSENCE of work, and absence is exactly what an
  // unsettled or degraded snapshot cannot establish (ds-eh6). Loading is
  // reported ahead of degraded: before the first cycle lands there is no
  // completed cycle for `degraded` to describe.
  if (input.settled === false) return { kind: 'unknown', reason: 'loading' };
  if (input.degraded === true) return { kind: 'unknown', reason: 'degraded' };

  return { kind: 'resting' };
}

/** A positive-integer PR number guard, mirroring `iacApprovalHref`'s own
 *  validation — shared by both lanes below so a malformed `pr_number` (from
 *  either source) can never inflate the count. */
function isPositiveIntPr(n: unknown): n is number {
  return typeof n === 'number' && Number.isInteger(n) && n > 0;
}

/**
 * Count of DISTINCT items genuinely awaiting the operator right now, across
 * BOTH lanes — the InstrumentBand "awaiting your approval" figure (Task
 * 3.5's ApprovalDesk). This is deliberately NOT the same number as "is
 * `deskModel` currently `pending`": `deskModel` surfaces one actionable item
 * at a time as a queue (the CTA card), while this is the honest system-wide
 * total of everything that queue will eventually work through — the ledger
 * strip beneath the desk already shows the rest as `◍` rows, so
 * under-reporting here would contradict what's directly beneath it on the
 * same screen. A desk showing ONE pending card can still report an
 * `awaitingCount` of 2 or more; that is correct, not a bug (see the
 * dedicated test in desk.test.ts that pins exactly this).
 *
 * Takes the same `DeskModelInput` shape as `deskModel` so a caller can feed
 * both from one snapshot (App.svelte's overview store).
 *
 * - Rollback lane: the count of `decisions` for which
 *   `isRollbackAwaitingOperator` (approval.ts) is true — the SAME predicate
 *   `selectPendingRollback` (rule 1, above) and `ledgerRows`'s `classify`
 *   use, so this can never disagree with either about which rollback rows
 *   are actionable.
 * - Iac lane: the count of DISTINCT `pr_number` values across the UNION of
 *   `pendingApprovals[].pr_number` and the `pr_number` of `decisions` for
 *   which `isIacAwaitingOperator` is true (the same predicate rule 2b uses).
 *   Deduped by PR number — the same PR can legitimately appear in BOTH
 *   sources (that's exactly the overlap `deskModel`'s rule 2a/2b dedup on
 *   href already handles for the single-selection case; here the two
 *   sources are unioned into a Set instead of "listing wins").
 *
 * Malformed/null array elements are skipped, not thrown on — mirrors every
 * other function in this module over the same open `Decision[]` /
 * `PendingApproval[]` shapes.
 */
export function awaitingCount(input: DeskModelInput): number {
  const now = input.now ?? Date.now();
  const decisions = input.decisions ?? [];
  const pendingApprovals = input.pendingApprovals ?? [];

  let rollbackCount = 0;
  for (const decision of decisions) {
    if (decision == null) continue;
    if (isRollbackAwaitingOperator(decision, { now, origin: input.origin })) rollbackCount += 1;
  }

  // Two DIFFERENT questions, two different sets (ds-dzd). resolvedPrs is PR-wide
  // and serves the LISTING lane only (ds-0rm: a provably applied-and-merged PR
  // still named by the 60s backend cache used to inflate this figure by one, so
  // the band over-reported against a desk that was correctly showing the seal).
  // supersededIds is per GENERATION and serves the DECISIONS lane: an `applied`
  // row on generation A says nothing about a waiting row on generation B.
  const resolvedPrs = resolvedIacPrNumbers(decisions);
  const supersededIds = supersededWaitingIds(decisions);

  const iacPrs = new Set<number>();
  for (const approval of pendingApprovals) {
    if (approval == null) continue;
    if (resolvedPrs.has(approval.pr_number as number)) continue;
    if (isPositiveIntPr(approval.pr_number)) iacPrs.add(approval.pr_number);
  }
  for (const decision of decisions) {
    if (decision == null) continue;
    if (
      isIacAwaitingOperator(decision, supersededIds) &&
      isPositiveIntPr(decision.pr_number)
    ) {
      iacPrs.add(decision.pr_number);
    }
  }

  return rollbackCount + iacPrs.size;
}
