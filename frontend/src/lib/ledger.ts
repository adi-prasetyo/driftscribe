// ledger.ts — pure classification + ordering for the desk's "Recent record"
// ledger strip (Task 3.4, docs/plans/2026-07-28-composite-mockup.html
// ".striph"/".strip"/".srow"). Reduces the same `decisions` list the rail
// already holds down to a handful of rows a first-time operator can read at a
// glance: what did I approve, what still needs me, and what's just a note for
// the record.
//
// Pure + fully time-injectable, same shape as desk.ts: no fetch, no DOM, no
// store, no ambient clock beyond the one `Date.now()` default at the entry
// point. `desk.ts`'s header comment explains the rationale; this module
// mirrors it rather than restating it.
//
// The mockup's quiet 定期点検 (periodic scan, "no drift found") row is
// DEFERRED here, not implemented: scan runs are not persisted anywhere the
// SPA can read (only decisions — actions the agent took — are), so there is
// no data to build such a row from. Faking one would be a lie on an audit
// surface. A future iteration would need a backend change to persist scan
// runs before this module could add that row.

import type { Decision } from './types';
import { resolvedIacPrNumbers, isRollbackAwaitingOperator, isIacAwaitingOperator } from './approval';

export type LedgerState = 'applied' | 'open' | 'noted' | 'failed' | 'unconfirmed';

export interface LedgerRow {
  decision: Decision;
  state: LedgerState;
  /** epoch-ms from created_at for ordering; null when absent/unparseable. */
  ts: number | null;
}

const DEFAULT_MAX = 4;

/** Parses `created_at` to epoch-ms, or null if absent/unparseable. Unlike
 *  desk.ts's `parseForOrdering` (which substitutes -Infinity so a missing
 *  date never displaces a dated rival but is silently indistinguishable from
 *  "very old"), `ledgerRows` needs to know explicitly which rows had no
 *  parseable date at all, so it can send them to the end of the list without
 *  conflating them with a genuinely ancient decision. */
function parseCreatedAt(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

/**
 * Classify one decision, in precedence order: applied, then open, then noted
 * (the default — everything else, including an expired or denied approval,
 * which is correctly a record rather than a solicitation).
 */
function classify(
  d: Decision,
  resolvedPrs: ReadonlySet<number>,
  now: number,
  origin: string | undefined,
): LedgerState {
  // Rollback lane: `status === 'used'` means the single-use credential was
  // spent, NOT that traffic moved — the flip precedes the Cloud Run call by
  // construction. This branch used to return 'applied' for any `used`
  // approval, which reported failed and still-running rollbacks as applied
  // (ds-2mc). The outcome lives in `phase`.
  const approvalPhase = d.approval?.status === 'used' ? d.approval.phase : undefined;
  if (approvalPhase === 'failed') return 'failed';
  // Not shown to have failed — an uncancelled operation may yet succeed, and a
  // lost response says nothing about the outcome. Distinct state, distinct copy.
  if (approvalPhase === 'outcome_unknown') return 'unconfirmed';
  // 'claimed'/'applying' fall through to 'noted': in flight, nothing settled.

  if (d.apply_status === 'applied' || approvalPhase === 'applied') {
    return 'applied';
  }

  // Both lanes below delegate to approval.ts's shared "is this decision
  // awaiting the operator" predicates — the SAME ones desk.ts's
  // selectPendingRollback / selectPendingIacFromDecisions use, so the ledger
  // and the desk can never disagree about whether a decision needs the
  // operator. See each predicate's own doc comment for its exact criteria.
  if (isRollbackAwaitingOperator(d, { now, origin })) {
    return 'open';
  }
  if (isIacAwaitingOperator(d, resolvedPrs)) {
    return 'open';
  }

  return 'noted';
}

/**
 * Reduce `decisions` to at most `max` (default 4) ledger rows: classified,
 * newest-first by `created_at`, capped. Pure and defensive — tolerates a
 * null/undefined list and null/undefined elements (skips, never throws),
 * mirroring desk.ts / resolvedIacPrNumbers's guards on the same open Decision
 * shape.
 *
 * `resolvedIacPrNumbers(decisions)` is computed ONCE up front, not per row —
 * it is already an O(n) scan of the same list, so recomputing it inside the
 * per-row loop would make the whole function O(n²) for a set that never
 * changes mid-classification.
 *
 * `max <= 0` returns an empty array; a non-finite `max` (NaN, ±Infinity, or
 * simply omitted) falls back to the default of 4.
 *
 * `opts.origin` is passed straight through to `safeApprovalHref` (defaults to
 * `window.location.origin` there when omitted) — no `locale` option, unlike
 * desk.ts's `DeskModelInput`: `safeApprovalHref`'s `locale` param only ever
 * changes whether the returned href carries a `?lang=ja` suffix, never
 * null-vs-non-null, and this module discards the href anyway (it only needs
 * the actionability verdict), so there is nothing here for `locale` to affect.
 */
export function ledgerRows(
  decisions: ReadonlyArray<Decision | null | undefined> | null | undefined,
  max: number = DEFAULT_MAX,
  opts?: { now?: number; origin?: string },
): LedgerRow[] {
  const now = opts?.now ?? Date.now();
  const origin = opts?.origin;
  const cap = Number.isFinite(max) ? max : DEFAULT_MAX;
  if (cap <= 0) return [];

  const list = decisions ?? [];
  const resolvedPrs = resolvedIacPrNumbers(list);

  const rows: LedgerRow[] = [];
  for (const d of list) {
    if (d == null) continue; // defensive: malformed array element, skip not throw
    rows.push({ decision: d, state: classify(d, resolvedPrs, now, origin), ts: parseCreatedAt(d.created_at) });
  }

  // Newest first; a null ts (absent/unparseable created_at) sorts LAST. Written
  // as explicit branches rather than a `(b.ts ?? -Infinity) - (a.ts ?? -Infinity)`
  // subtraction — two null rows would both substitute -Infinity, and
  // -Infinity - (-Infinity) is NaN, which a comparator must never return (engines
  // are not required to treat it as "equal"). Returning a literal 0 for equal
  // (including both-null) timestamps instead relies on Array.prototype.sort's
  // guaranteed stability to preserve their original relative order.
  rows.sort((a, b) => {
    if (a.ts === b.ts) return 0;
    if (a.ts === null) return 1;
    if (b.ts === null) return -1;
    return b.ts - a.ts;
  });

  return rows.slice(0, cap);
}
