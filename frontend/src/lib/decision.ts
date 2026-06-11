// decision.ts — turn a stored decision document into a small, SAFE set of
// display rows for the DecisionSummary card.
//
// SECURITY NOTE (Codex review, must-fix): GET /trace returns the decision doc
// RAW from StateStore.find_decision_by_trace_id — only the log `events` are
// redacted, NOT the decision. So this module must NEVER iterate arbitrary keys
// off the doc (a future field could carry a token/url/secret). It renders ONLY
// an explicit allowlist of known-safe fields, each with a fixed renderer. A
// decision type we don't recognise simply shows whichever allowlisted fields it
// happens to carry (typically just Action + When) — never anything dynamic.

import type { Decision } from './types';
import { fmtWhen } from './format';

export type FieldBadge = 'ok' | 'danger' | 'warn' | 'muted';

export interface DecisionField {
  /** Human label for the row. */
  label: string;
  /** Display value (already formatted/clamped — safe to render as text). */
  value: string;
  /** Render the value in the reserved monospace face (ids/SHAs). */
  code?: boolean;
  /** Render the value as a status pill of this variant. */
  badge?: FieldBadge;
  /** Full untruncated value for a hover title (e.g. the full head_sha). */
  title?: string;
}

// Defensive cap: every allowlisted field today is short (an email, a status
// enum, a SHA), but clamp anyway so a malformed/oversized value can't blow out
// the card layout.
const MAX_VALUE = 256;
const clamp = (s: string): string => (s.length > MAX_VALUE ? s.slice(0, MAX_VALUE) + '…' : s);

const SHA_DISPLAY = 12;

const APPLY_STATUS_BADGE: Record<string, FieldBadge> = {
  applied: 'ok',
  failed: 'danger',
  failed_state_suspect: 'danger',
  ambiguous: 'warn',
};

const MERGE_STATE_BADGE: Record<string, FieldBadge> = {
  merged: 'ok',
  failed: 'danger',
  pending: 'warn',
};

const ACTION_LABEL: Record<string, string> = {
  iac_apply: 'Infra apply',
  rollback: 'Rollback',
  recheck: 'Re-check',
};

const isStr = (v: unknown): v is string => typeof v === 'string' && v.length > 0;

/**
 * Ordered, safe display rows for a decision. Pure; renders only allowlisted
 * fields. Returns [] for a null/empty decision.
 */
export function decisionFields(d: Decision | null | undefined): DecisionField[] {
  if (!d) return [];
  const rows: DecisionField[] = [];

  const action = isStr(d.action) ? d.action : 'decision';
  rows.push({ label: 'Action', value: ACTION_LABEL[action] ?? clamp(action) });

  if (typeof d.pr_number === 'number') {
    rows.push({ label: 'Pull request', value: `#${d.pr_number}` });
  }

  if (isStr(d.apply_status)) {
    rows.push({
      label: 'Apply',
      value: clamp(d.apply_status),
      badge: APPLY_STATUS_BADGE[d.apply_status] ?? 'muted',
    });
  }

  if (isStr(d.merge_state)) {
    rows.push({
      label: 'Merge',
      value: clamp(d.merge_state),
      badge: MERGE_STATE_BADGE[d.merge_state] ?? 'muted',
    });
  }

  if (isStr(d.head_sha)) {
    rows.push({
      label: 'Head SHA',
      value: d.head_sha.slice(0, SHA_DISPLAY),
      code: true,
      title: clamp(d.head_sha),
    });
  }

  if (isStr(d.approver)) {
    rows.push({ label: 'Approver', value: clamp(d.approver) });
  }

  // One "When" row: prefer applied_at (the apply moment), else created_at.
  const when = isStr(d.applied_at) ? d.applied_at : isStr(d.created_at) ? d.created_at : '';
  if (when) {
    rows.push({ label: 'When', value: fmtWhen(when) });
  }

  return rows;
}

// --------------------------------------------------------------------------- //
// Applied-decision watermark (resource-map refresh trigger).
// --------------------------------------------------------------------------- //

export interface AppliedWatermark {
  /** decision_id of the newest applied iac_apply seen so far (null = none). */
  id: string | null;
  /** False until the FIRST /decisions load has been observed. */
  seeded: boolean;
}

/**
 * Decide whether a /decisions load contains a FRESHLY-applied iac_apply (one
 * not seen on a previous load) → `bump: true` drives InfraDiagram's delayed
 * 0/10/30/60s re-fetch ladder (rides out CAI lag after an apply).
 *
 * The FIRST load only SEEDS the watermark — a historical applied decision
 * present at boot must NOT bump. (Prod incident, Phase-4 live e2e 2026-06-11:
 * every page boot bumped on a 10-hour-old applied decision, so every boot rode
 * the full ladder — ~6 /infra/graph fetches in the first minute. Against the
 * concurrency-1 infra-reader, whose CAI inventory takes 10-15s, the queue
 * exceeded the coordinator's 30s worker timeout and EVERY map load came back
 * degraded: the panel DDOSed its own backend on boot.)
 */
export function nextAppliedWatermark(
  prev: AppliedWatermark,
  decisions: Decision[],
): { next: AppliedWatermark; bump: boolean } {
  const applied = decisions.find(
    (d) => d.action === 'iac_apply' && d.apply_status === 'applied',
  );
  const id = applied?.decision_id ?? null;
  if (!prev.seeded) return { next: { id, seeded: true }, bump: false };
  if (id !== null && id !== prev.id) return { next: { id, seeded: true }, bump: true };
  return { next: prev, bump: false };
}
