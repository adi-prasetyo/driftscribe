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
import {
  supersededWaitingIds,
  isRollbackAwaitingOperator,
  isIacAwaitingOperator,
} from './approval';

// 'open' and 'awaiting_apply' both mean "this row still needs the operator",
// but they are NOT interchangeable copy. 'open' is a live solicitation (a
// rollback approval nobody has spent yet); 'awaiting_apply' is the IaC lane
// AFTER approval — the backend writes that apply_status at merge time
// (agent/main.py:7280,7331 record it alongside merge_state pending→merged), so
// calling it "awaiting your approval" told the operator their own approval had
// not happened (ds-db0).
//
// Named for the APPLY, not the re-bake, deliberately. The `waiting_for_rebake`
// status does not mean "the re-bake is still running" — the coordinator never
// observes the external build at all. It writes the status at merge and leaves
// it there until the operator's second submit resumes the apply
// (agent/main.py:7187 `_iac_resume_apply`). So "awaiting re-bake" goes stale the
// instant the build finishes, while "awaiting apply" is true for the whole
// window from merge to that second submit. A state name that asserts something
// this client cannot see will mislead the next reader of it (Codex review r3).
export type LedgerState =
  | 'applied'
  | 'open'
  | 'awaiting_merge'
  | 'awaiting_apply'
  | 'noted'
  | 'failed'
  | 'unconfirmed';

export interface LedgerRow {
  decision: Decision;
  state: LedgerState;
  /** epoch-ms from created_at for ordering; null when absent/unparseable. */
  ts: number | null;
}

const DEFAULT_MAX = 4;

/** The ONE apply_status that may be collapsed into a newer doc for the same
 *  event_key (ds-b0k). `waiting_for_rebake` is the pre-merge crash-recovery
 *  pointer — a phase, not an outcome — which is exactly why it is excluded
 *  from the server's own `_IAC_RUN_ENDED_STATUSES` (agent/main.py:918). Every
 *  other status, INCLUDING one this build does not recognise, keeps its row. */
const IAC_FOLDABLE_STATUS = 'waiting_for_rebake';

/** The one status foldable by shared `apply_attempt_id`. See the second fold
 *  rule in `ledgerRows` for why this is `applied` and nothing else: a terminal
 *  FAILURE must always keep its own row, no matter what follows it. */
const IAC_ATTEMPT_FOLDABLE_STATUS = 'applied';

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
  supersededIds: ReadonlySet<string>,
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
  // Same predicate as before — only the STATE differs, so the desk and the
  // ledger still agree on "does this need the operator". `isIacAwaitingOperator`
  // is true only for apply_status === 'waiting_for_rebake', which the backend
  // records at merge, so every row reaching here is already approved.
  if (isIacAwaitingOperator(d, supersededIds)) {
    // `waiting_for_rebake` is recorded TWICE: once before the merge
    // (merge_state 'pending', agent/main.py:7280) and once after
    // (merge_state 'merged', :7331). Only the second is waiting on the
    // operator's apply — while the merge is unfinished (or blocked) the pending
    // work is the MERGE, so naming the apply there would be a second wrong
    // claim in place of the one this fix removed.
    return d.merge_state === 'merged' ? 'awaiting_apply' : 'awaiting_merge';
  }

  return 'noted';
}

/**
 * Reduce `decisions` to at most `max` (default 4) ledger rows: classified,
 * newest-first by `created_at`, capped. Pure and defensive — tolerates a
 * null/undefined list and null/undefined elements (skips, never throws),
 * mirroring desk.ts's guards on the same open Decision shape.
 *
 * `supersededWaitingIds(decisions)` is computed ONCE up front, not per row — it
 * is already an O(n) scan of the same list, so recomputing it inside the per-row
 * loop would make the whole function O(n²) for a set that never changes
 * mid-classification. The PR-wide `resolvedIacPrNumbers` is deliberately NOT
 * consulted here: iac actionability is per GENERATION (ds-dzd).
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
  const supersededIds = supersededWaitingIds(list);

  const rows: LedgerRow[] = [];
  for (const d of list) {
    if (d == null) continue; // defensive: malformed array element, skip not throw
    rows.push({
      decision: d,
      state: classify(d, supersededIds, now, origin),
      ts: parseCreatedAt(d.created_at),
    });
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

  // One LIFECYCLE, one row (ds-b0k). A single `event_key` accumulates a SEQUENCE
  // of phase docs by design: PR #168 wrote waiting_for_rebake+pending and then
  // waiting_for_rebake+merged seven seconds apart, and June's `iac-apply-95`
  // ran pending → merged → failed_state_suspect. Drawing each doc as its own row
  // made one event look like several — and with a 4-row cap, one chatty event
  // could swallow the whole strip.
  //
  // ⚠️ `event_key` is NOT unique per occurrence — it hashes the drift signature
  // / PR generation, so a RECURRENCE reuses it. Live proof: the eventarc key
  // ...ae4170632c79c599 carries a no_op (07-29) AND a rollback (07-31) two days
  // apart, and `iac-apply-32-...` carries two separate applied+merged records 27
  // days apart. A naive collapse-by-key would delete the older of each — on a
  // strip headed "Recent record", hiding a real record is far worse than
  // showing a redundant one, so the collapse is deliberately narrow — two rules,
  // each folding one shape PROVEN to be the same fact written twice:
  //
  //   1. an unfinished IaC phase pointer a newer doc for the same event_key has
  //      already moved past;
  //   2. an `applied` row a newer `applied` row for the same apply_attempt_id
  //      has already restated.
  //
  // `_IAC_RUN_ENDED_STATUSES` (agent/main.py:918) is the server's own list of
  // statuses meaning "this apply request already ended", and it deliberately
  // EXCLUDES waiting_for_rebake — precisely because that value is the pre-merge
  // crash-recovery pointer, i.e. a phase and not an outcome. A terminal doc is a
  // completed historical fact and keeps its row unless rule 2 proves a strictly
  // newer doc is that same attempt restated. Non-iac lanes (rollback, no_op,
  // escalation) never write multiple docs per key within one lifecycle — their
  // repeats are recurrences — so they are excluded entirely.
  //
  // Runs AFTER the sort (so "newest" is meaningful) and BEFORE the cap, so the
  // cap is spent on distinct facts rather than on restatements of one. It is
  // NOT a guarantee of one row per lifecycle, and must not be described as one:
  // a lifecycle that genuinely records several DIFFERENT outcomes (an apply that
  // failed, then a later attempt that succeeded) still spends a row on each, by
  // design. Rows whose identity field is absent or empty are never collapsed on
  // that rule: absent identity is not shared identity.
  const seenIacEventKeys = new Set<string>();
  const seenAppliedAttemptIds = new Set<string>();
  const deduped: LedgerRow[] = [];
  for (const row of rows) {
    const d = row.decision;
    if (d.action !== 'iac_apply') {
      deduped.push(row);
      continue;
    }
    const key = d.event_key;
    const hasKey = typeof key === 'string' && key !== '';
    const attemptId = d.apply_attempt_id;
    const hasAttemptId = typeof attemptId === 'string' && attemptId !== '';

    // RULE 1 — the phase pointer. Fold ONLY the one status proven redundant.
    // Written as a positive test against `waiting_for_rebake` rather than as
    // `!IAC_RUN_ENDED_STATUSES`, because a negative test silently makes every
    // UNKNOWN status foldable — a legacy row with no apply_status, a malformed
    // doc, or a status added server-side later would all be discarded sight
    // unseen. On an audit surface unknown must fail toward RETENTION: show a
    // redundant row rather than delete an unrecognised one.
    if (hasKey && d.apply_status === IAC_FOLDABLE_STATUS && seenIacEventKeys.has(key)) {
      continue;
    }

    // RULE 2 — one apply attempt, one row (Codex review r3). Rule 1 alone still
    // let ONE apply fill the strip: the merge-only reconcile records
    // `applied`+`merged` (agent/main.py:7117) over an earlier `applied`+`failed`
    // (:7060) for the same attempt, and both are terminal, so both survived —
    // as two rows reading identically ("Approved · applied"), because the
    // ledger's copy turns on apply_status alone. Worse, `reconcile_merge_state`
    // promotes a stale `failed` to `merged` at SERVE time, so the two rows can
    // arrive already byte-identical. Repeated reconciles could push four of them
    // and crowd out every unrelated record.
    //
    // `apply_attempt_id` is the right identity and the only one available: the
    // worker mints it per apply, and the reconcile path passes the ORIGINAL
    // value straight through (:7219), so rows sharing it are one apply seen at
    // several moments. Both sides must be `applied`: if the newer row is a
    // terminal FAILURE it never enters the set, so the older row keeps its place
    // and the transition stays visible. A failure is never folded into anything.
    if (
      hasAttemptId &&
      d.apply_status === IAC_ATTEMPT_FOLDABLE_STATUS &&
      seenAppliedAttemptIds.has(attemptId)
    ) {
      continue;
    }

    if (hasKey) seenIacEventKeys.add(key);
    if (hasAttemptId && d.apply_status === IAC_ATTEMPT_FOLDABLE_STATUS) {
      seenAppliedAttemptIds.add(attemptId);
    }
    deduped.push(row);
  }

  return deduped.slice(0, cap);
}
