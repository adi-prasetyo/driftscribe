// View-model grouping for the past-decisions rail. An iac_apply lifecycle
// writes one decision doc per apply attempt (live data: applied +
// waiting_for_rebake×2 for create-class PRs; applied + failed for a retry), so
// the rail shows 2-3 near-identical rows per PR. This folds them into ONE
// render item per PR — the newest doc is the face (latest known state), the
// earlier docs become the expandable lifecycle. Pure data → data; the
// component decides presentation.

import type { Decision } from './types';
import { iacStatusLabel } from './format';
import { iacPrHref } from './approval';
import type { IconName } from './icons';

/**
 * Maps a decision action string to a leading icon for the rail row.
 * Fail-safe: null/undefined/empty/unrecognised action all return 'file-text'.
 *
 * Priority (first match wins):
 *   rollback          → rotate-ccw
 *   iac               → git-merge
 *   upgrade | pr      → git-pull-request
 *   issue | drift | report → alert-triangle
 *   anything else     → file-text
 */
export function railRowIcon(action: string | null | undefined): IconName {
  if (!action) return 'file-text';
  const a = action.toLowerCase();
  if (a.includes('rollback')) return 'rotate-ccw';
  if (a.includes('iac')) return 'git-merge';
  if (a.includes('upgrade') || a.includes('pr')) return 'git-pull-request';
  if (a.includes('issue') || a.includes('drift') || a.includes('report')) return 'alert-triangle';
  return 'file-text';
}

export type RailItem =
  | { kind: 'single'; d: Decision }
  | {
      kind: 'group';
      pr: number;
      /** ≥2 docs, in list (newest-first) order; docs[0] is the face.
       *  Deliberately readonly for consumers: the component derives views off
       *  this array inside `$derived` and must copy (`[...docs]`) before any
       *  reorder — never mutate in place. */
      docs: readonly Decision[];
    };

/**
 * True when a decision can join a PR group: an `iac_apply` doc whose
 * `pr_number` is a positive integer — exactly the `iacApprovalHref` guard, so
 * a malformed pr_number can never form (or join) a group.
 */
function groupablePr(d: Decision): number | null {
  if (d.action !== 'iac_apply') return null;
  const n = d.pr_number;
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : null;
}

/**
 * Fold the newest-first /decisions list into rail render items. Same-PR
 * iac_apply docs (≥2) collapse into a `group` anchored at the position of the
 * PR's newest doc; everything else stays a `single` in place. Contiguity is
 * NOT assumed. Tolerates a null/undefined list and null entries (dropped),
 * matching `resolvedIacPrNumbers`.
 */
export function groupRailDecisions(
  decisions: ReadonlyArray<Decision | null | undefined> | null | undefined,
): RailItem[] {
  const ds = (decisions ?? []).filter((d): d is Decision => d != null);

  // Pass 1: count docs per groupable PR so lone docs stay singles.
  const counts = new Map<number, number>();
  for (const d of ds) {
    const pr = groupablePr(d);
    if (pr !== null) counts.set(pr, (counts.get(pr) ?? 0) + 1);
  }

  // Pass 2: emit items in order; a group is emitted at its first (newest) doc.
  const items: RailItem[] = [];
  const emitted = new Map<number, Decision[]>();
  for (const d of ds) {
    const pr = groupablePr(d);
    if (pr === null || (counts.get(pr) ?? 0) < 2) {
      items.push({ kind: 'single', d });
      continue;
    }
    const docs = emitted.get(pr);
    if (docs) {
      docs.push(d);
    } else {
      const fresh = [d];
      emitted.set(pr, fresh);
      items.push({ kind: 'group', pr, docs: fresh });
    }
  }
  return items;
}

/**
 * Whether to show the rail-header PR-numbering hint (explains why the numbers
 * skip values). The numbered rows are iac_apply decisions whose `pr_number`
 * comes straight from GitHub, so they skip every non-infra PR (UI, docs, code)
 * in between. Surface the note only once there are ≥2 DISTINCT numbered rows —
 * with 0 or 1 there is no sequence to explain, so it would be noise. Counts
 * DISTINCT numbers (via the same `groupablePr` guard the grouping uses), so one
 * PR's multi-doc lifecycle never trips it.
 *
 * (A stricter span-vs-count "is there literally a gap" test would, in this app,
 * almost never differ — infra applies are sparse among many dev PRs, so any two
 * are essentially always non-contiguous — so the distinct-count threshold is
 * the honest, non-brittle trigger; the copy says "can skip", not "do skip".)
 *
 * Counts only rows that actually render a linked `PR #n`: the render gate is the
 * GitHub href (`iacPrHref`, off `github.url`), the displayed value is a valid
 * `pr_number` (`groupablePr`). Requiring BOTH keeps the hint in lockstep with
 * the numbered rows the operator sees — a fail-soft row with a number but no
 * usable GitHub link renders as plain `iac_apply` and must not inflate the
 * count. Tolerates a null/undefined list + entries.
 */
export function showPrNumberingHint(
  decisions: ReadonlyArray<Decision | null | undefined> | null | undefined,
): boolean {
  const prs = new Set<number>();
  for (const d of decisions ?? []) {
    if (d == null) continue;
    const pr = groupablePr(d);
    if (pr !== null && iacPrHref(d) !== null) prs.add(pr);
  }
  return prs.size >= 2;
}

// Statuses that read as a normal in-flight/terminal lifecycle. Anything else —
// failed, failed_state_suspect, ambiguous, a future unknown value, or a doc
// with no status at all — is anomalous and must be visible without a click.
const CALM_STATUSES = new Set(['applied', 'waiting_for_rebake']);

/**
 * True when any earlier step carries a status outside CALM_STATUSES. Missing
 * and unknown statuses count as anomalous (fail-open to visible): the rail
 * must never collapse a failure — or something it cannot classify — behind a
 * closed expander for this audience.
 */
export function hasAnomalousStep(earlier: ReadonlyArray<Decision>): boolean {
  return earlier.some(
    (d) => typeof d.apply_status !== 'string' || !CALM_STATUSES.has(d.apply_status),
  );
}

/**
 * The complete `<summary>` text for a lifecycle expander: a count plus a
 * status composition, so the collapsed row never hides WHAT the earlier steps
 * were — e.g. `2 earlier steps · awaiting rebuild ×2`, `1 earlier step ·
 * failed`. `earlier` arrives in list (newest-first) order; composition labels
 * are ordered by first appearance oldest-first and deduped with `×k` counts.
 * Returned as ONE string so the component renders it as a single expression —
 * no markup seams, no whitespace-collapse risk.
 *
 * Precondition: callers pass `docs.slice(1)` of a group, so `earlier.length
 * >= 1` (a group has ≥2 docs by construction — see `groupRailDecisions`). An
 * empty input would render a malformed `0 earlier steps · ` and is never
 * produced by the grouping.
 */
export function lifecycleSummaryLabel(earlier: ReadonlyArray<Decision>): string {
  const n = earlier.length;
  const counts = new Map<string, number>();
  for (let i = earlier.length - 1; i >= 0; i--) {
    const label = iacStatusLabel(earlier[i].apply_status) || 'status not recorded';
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const composition = [...counts.entries()]
    .map(([label, k]) => (k > 1 ? `${label} ×${k}` : label))
    .join(', ');
  return `${n} earlier ${n === 1 ? 'step' : 'steps'} · ${composition}`;
}
