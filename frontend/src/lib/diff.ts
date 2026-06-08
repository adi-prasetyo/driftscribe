// diff.ts — turn a stored decision's diffs[] into a small, SAFE set of display
// rows for the env-diff card. Mirrors lib/decision.ts's discipline: GET /trace
// returns the decision doc UNREDACTED, so this module NEVER trusts the shape and
// NEVER renders a raw value — every value goes through displayDiffValue, which
// applies the SAME redaction rule the backend uses for the GitHub PR/issue body
// (agent/renderer.py:_format_value_cell + agent/secret_guard.py).

import type { Decision, EnvDiff } from './types';
import type { FieldBadge } from './decision';
import { shouldRedact } from './secret_guard';

const REDACTED = '(value redacted: secret-like)';
const EMDASH = '—';

// Defensive cap (matches lib/decision.ts): clamp a shown value so a malformed /
// oversized one can't blow out the table. Applied AFTER the redaction decision
// so a long credentialed URL is still detected by shouldRedact on the full value.
const MAX_VALUE = 256;
const clamp = (s: string): string => (s.length > MAX_VALUE ? s.slice(0, MAX_VALUE) + '…' : s);

/** Mirror of agent/renderer.py:_format_value_cell. Redact (name secret-like OR
 *  value credentialed) → marker; null → em-dash; else the (clamped) value.
 *  Empty string is preserved — an explicitly-unset var is a real drift signal. */
export function displayDiffValue(name: string, value: string | null | undefined): string {
  if (shouldRedact(name, value)) return value != null ? REDACTED : EMDASH;
  if (value == null) return EMDASH;
  return clamp(value);
}

const CONTRACT_BADGE: Record<string, FieldBadge> = {
  // `match` = live value matches the contract = no drift → 'ok' (green).
  // `muted` (grey) is reserved for the unknown-status fallback below.
  match: 'ok',
  present_allow_manual: 'ok',
  present_disallow_manual: 'danger',
  absent: 'warn',
};

export interface DiffRow {
  name: string;
  /** Already display-formatted (redacted-or-value or em-dash) — safe as text. */
  expected: string;
  live: string;
  /** Raw contract_status enum string (rendered as the pill label). */
  status: string;
  badge: FieldBadge;
}

/**
 * Ordered, safe display rows for a decision's env diffs. Pure. Defensively
 * validates each diff (skips any without a string `name`); never trusts shape.
 * Returns [] for a null decision or one with no diffs[].
 */
export function diffRows(d: Decision | null | undefined): DiffRow[] {
  if (!d || !Array.isArray(d.diffs)) return [];
  const rows: DiffRow[] = [];
  for (const raw of d.diffs as unknown[]) {
    if (!raw || typeof raw !== 'object') continue;
    const o = raw as Partial<EnvDiff>;
    const name = typeof o.name === 'string' ? o.name : '';
    if (!name) continue; // a diff with no name is unrenderable
    const expected = typeof o.expected === 'string' ? o.expected : null;
    const live = typeof o.live === 'string' ? o.live : null;
    const status = typeof o.contract_status === 'string' ? o.contract_status : '';
    rows.push({
      name: clamp(name),
      // displayDiffValue gets the RAW (unclamped) name so shouldRedact sees the
      // full name text; clamp(name) above is display-only (redaction is not).
      expected: displayDiffValue(name, expected),
      live: displayDiffValue(name, live),
      status,
      badge: CONTRACT_BADGE[status] ?? 'muted',
    });
  }
  return rows;
}
