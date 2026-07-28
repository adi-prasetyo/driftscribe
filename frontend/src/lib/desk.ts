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
} from './approval';
import type { Decision } from './types';
import type { PendingApproval } from './infra_graph';

/** The stamped state's confirmation window: how long a just-resolved action
 *  stays on the desk before decaying to resting. The component schedules its
 *  own decay timer off `stampedUntil` (an absolute epoch-ms), so this constant
 *  only needs to be right once, here. */
export const STAMP_WINDOW_MS = 10 * 60 * 1000;

/** A pending rollback: a decision whose live approval doc is still awaiting
 *  the operator (or was never enriched — see `status` below). */
export interface DeskPendingRollback {
  kind: 'pending';
  source: 'rollback';
  /** The decision this CTA acts on — carries identity for the card's title. */
  decision: Decision;
  /** Same-origin relative href, already validated by `safeApprovalHref`. */
  href: string;
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

export interface DeskResting {
  kind: 'resting';
}

export type DeskModel = DeskPending | DeskStamped | DeskResting;

export interface DeskModelInput {
  // Element type includes null/undefined: both arrays are open, externally-
  // sourced payloads (overviewStore.ts casts JSON.parse output straight to
  // `Decision[]` / `PendingApproval[]` — see its fetchDecisionsList /
  // fetchPendingList), so a malformed element is a real runtime possibility
  // the static element type doesn't rule out. Mirrors resolvedIacPrNumbers's
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
  return best === null ? null : { kind: 'pending', source: 'rollback', decision: best.decision, href: best.href };
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
 */
function selectPendingIac(
  pendingApprovals: ReadonlyArray<PendingApproval | null | undefined>,
  locale: string | undefined,
): DeskPendingIac | null {
  for (const approval of pendingApprovals) {
    if (approval == null) continue; // defensive: malformed array element, skip not throw
    const href = iacApprovalHref(approval.pr_number, locale);
    if (href == null) continue; // malformed pr_number — never a dead CTA, try the next one
    return {
      kind: 'pending',
      source: 'iac',
      prNumber: approval.pr_number,
      href,
      provenance: { kind: 'listing', approval },
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
 * `resolvedPrs` is computed once by the caller (`deskModel`) and threaded in,
 * mirroring ledger.ts's `ledgerRows` — it's an O(n) scan of the same
 * `decisions` list, so recomputing it per candidate here would make this
 * function O(n²) for a set that never changes mid-scan.
 */
function selectPendingIacFromDecisions(
  decisions: ReadonlyArray<Decision | null | undefined>,
  resolvedPrs: ReadonlySet<number>,
  locale: string | undefined,
): DeskPendingIac | null {
  let best: { decision: Decision; href: string; prNumber: number; ts: number } | null = null;
  for (const decision of decisions) {
    if (decision == null) continue; // defensive: malformed array element, skip not throw
    if (!isIacAwaitingOperator(decision, resolvedPrs)) continue;
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
    if (approval?.status === 'used') {
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
 * Selects which of the desk's three states applies, in strict priority order:
 * pending rollback (rule 1) > pending iac approval, listing-first then
 * decisions-derived (rule 2a, then rule 2b) > stamped (rule 3) > resting
 * (rule 4). See the per-rule helpers above for each rule's exact criteria;
 * this function only sequences them.
 */
export function deskModel(input: DeskModelInput): DeskModel {
  const now = input.now ?? Date.now();
  const decisions = input.decisions ?? [];
  const pendingApprovals = input.pendingApprovals ?? [];

  const rollback = selectPendingRollback(decisions, now, input.origin, input.locale);
  if (rollback) return rollback;

  const iacFromListing = selectPendingIac(pendingApprovals, input.locale);
  if (iacFromListing) return iacFromListing;

  const iacFromDecisions = selectPendingIacFromDecisions(
    decisions,
    resolvedIacPrNumbers(decisions),
    input.locale,
  );
  if (iacFromDecisions) return iacFromDecisions;

  const stamped = selectStamped(decisions, now);
  if (stamped) return stamped;

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

  const iacPrs = new Set<number>();
  for (const approval of pendingApprovals) {
    if (approval == null) continue;
    if (isPositiveIntPr(approval.pr_number)) iacPrs.add(approval.pr_number);
  }
  const resolvedPrs = resolvedIacPrNumbers(decisions);
  for (const decision of decisions) {
    if (decision == null) continue;
    if (isIacAwaitingOperator(decision, resolvedPrs) && isPositiveIntPr(decision.pr_number)) {
      iacPrs.add(decision.pr_number);
    }
  }

  return rollbackCount + iacPrs.size;
}
