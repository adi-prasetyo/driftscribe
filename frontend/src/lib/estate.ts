// estate.ts — pure model for the estate view (Task 4.1, docs/plans/2026-07-28-
// composite-mockup.html "SCREEN 2 — 推定図"). The mockup groups resources by
// STATUS (drift first, then managed) and flattens across resource TYPES, with
// the type rendered as a per-row label — the inverse of resourceCards()'s own
// grouping (one card PER type, managed+drift rows mixed inside it). This
// module re-groups resourceCards()'s output rather than re-deriving anything
// from the raw graph, so the estate view can never disagree with the
// desk/InfraDiagram panel about what counts as managed/drift/adoptable
// (single source of truth: lib/infra_graph.ts).
//
// Pure + no fetches — EstateView.svelte (Task 4.1) performs no I/O of its own,
// same discipline as lib/desk.ts for ApprovalDesk.

import {
  findPendingPr,
  infraTypeLabel,
  resourceCards,
  splitCards,
  type InfraGraph,
  type PendingApproval,
  type ResourceRowStatus,
} from './infra_graph';
import type { TranslateFn } from './i18n';
import { translate } from './i18n';
import { resolvedIacPrNumbers } from './approval';
import type { Decision } from './types';

/**
 * Sanitize an externally-sourced approvals payload AND retire any entry a
 * decision proves already applied (ds-0rm's filter).
 *
 * Exported because THREE surfaces read this list and every one of them must
 * agree: the estate rows (via `estateModel`), App's adopt-target, and the
 * tour's step-4 suggestion. When only some applied the filter, one screen
 * contradicted itself — the InstrumentBand showed "0 awaiting" (its
 * `awaitingCount` does filter) directly above a row reading "PR #268 awaiting
 * review" (which did not). One function, one answer.
 *
 * Note the asymmetry with `approvalsStale`: that suppresses claims about
 * ABSENCE. This drops a POSITIVE claim we hold positive counter-evidence for.
 * Only an applied AND MERGED decision qualifies — `resolvedIacPrNumbers`
 * requires both — so an in-progress apply, a failed apply, or an apply whose
 * MERGE failed all leave the entry standing. That last case is load-bearing
 * (ds-dzd): an apply can succeed while its merge fails, leaving the PR open with
 * a later generation on it that has no decision row yet, and dropping the chip
 * there would delete live work from this view and the tour with nothing else to
 * surface it.
 */
export function reconcileApprovals(
  approvals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined,
  decisions?: ReadonlyArray<Decision | null | undefined> | null,
): PendingApproval[] {
  const resolvedPrs = resolvedIacPrNumbers(decisions ?? []);
  return (approvals ?? []).filter(
    (a): a is PendingApproval => a != null && !resolvedPrs.has(a.pr_number),
  );
}

// EN-bound fallback translator, mirroring infra_graph.ts's own EN_T: a caller
// that omits `t` (e.g. a quick test) gets byte-identical EN output rather than
// a crash on a missing translator.
const EN_T: TranslateFn = (key, params) => translate('en', key, params);

export interface EstateRow {
  /** each-key — the underlying node's server-assigned id (unique within the graph). */
  nodeId: string;
  /** UNTRUSTED resource name — reaches Svelte text interpolation + the chat
   *  input (via `prefill`) only, never an HTML sink. */
  label: string;
  assetType: string;
  /** Localized friendly type label (infraTypeLabel), rendered per-row since
   *  the estate view flattens across types instead of grouping by them. */
  typeLabel: string;
  /** managed | drift | untracked — control_plane rows never reach here; they
   *  live in EstateModel.systemManaged instead (mirrors ResourceCard's own
   *  rows-vs-systemManaged split). */
  status: ResourceRowStatus;
  /** drift AND the type is adoptable AND the node is not control-plane. */
  adoptable: boolean;
  /** Chat prefill — composed ONLY for adoptable rows, else ''. */
  prefill: string;
  /** Open adoption-PR number for this exact resource, or null. Joined via
   *  findPendingPr (asset_type + short name) — only ever non-null for a
   *  `drift` row (untracked/managed rows are never subject to an adoption PR). */
  pendingPr: number | null;
}

export interface EstateModel {
  /** Actionable-drift rows, flattened across PRIMARY cards, drift-first +
   *  adopt-rank order preserved (resourceCards()'s own sort — never re-derived). */
  drift: EstateRow[];
  /** Σ card.hiddenUnmanaged over primary cards — actionable drift the
   *  per-type sample didn't include a row for (server-side cap), never a
   *  client-invented truncation. */
  driftHidden: number;
  managed: EstateRow[];
  /** Unmanaged rows of a non-adoptable type: neutral, never amber, never a CTA. */
  untracked: EstateRow[];
  /** Folded system-managed (control-plane) rows, sampled — the `<details>` contents. */
  systemManaged: EstateRow[];
  /** Σ card.systemManagedTotal over primary cards — TRUE count for the fold's
   *  summary, >= systemManaged.length (see ResourceCard.systemManagedTotal). */
  systemManagedTotal: number;
  /** Resource types DriftScribe doesn't manage (splitCards().other.length). */
  otherTypes: number;
  /** Σ count over those OTHER cards — what the disclosure would list. */
  otherResources: number;
}

const EMPTY_MODEL: EstateModel = {
  drift: [],
  driftHidden: 0,
  managed: [],
  untracked: [],
  systemManaged: [],
  systemManagedTotal: 0,
  otherTypes: 0,
  otherResources: 0,
};

/**
 * Build the estate view's row model from the same `/infra/graph` DTO the
 * desk/InfraDiagram panel already read (via resourceCards()). `graph === null`
 * (not yet loaded) and `graph.degraded` both yield the empty model — the
 * caller renders an honest loading/degraded line for those two cases (never a
 * fabricated "all clear"; see EstateView.svelte).
 *
 * Only PRIMARY cards (resourceCards()+splitCards() — adoptable types or
 * anything with a managed resource) contribute rows; OTHER cards are summed
 * into otherTypes/otherResources only, matching InfraDiagram's own scope
 * split so the two views never disagree about what's in scope.
 */
export function estateModel(
  graph: InfraGraph | null,
  approvals: ReadonlyArray<PendingApproval | null | undefined> | null | undefined,
  t: TranslateFn = EN_T,
  /**
   * The decisions log, used ONLY to retire listing entries a decision proves
   * already applied (ds-0rm's filter, third consumer).
   *
   * Without it this view contradicted itself on one screen: the backend
   * listing is cached 60s (`_PENDING_APPROVALS_TTL_S`), so after a PR merges
   * and applies the row still rendered "PR #268 awaiting review" while the
   * InstrumentBand directly above it — which DOES apply the filter, via
   * `awaitingCount` — showed "0 awaiting". The applied decision positively
   * disproves the chip.
   *
   * Optional so existing callers and tests are unchanged; omitted means no
   * reconciliation, exactly as before.
   */
  decisions?: ReadonlyArray<Decision | null | undefined> | null,
): EstateModel {
  if (graph === null || graph.degraded) return EMPTY_MODEL;

  // Sanitized + reconciled once here rather than at every call site below.
  const cleanApprovals = reconcileApprovals(approvals, decisions);

  const cards = resourceCards(graph, t);
  const { primary, other } = splitCards(cards);

  const drift: EstateRow[] = [];
  const managed: EstateRow[] = [];
  const untracked: EstateRow[] = [];
  const systemManaged: EstateRow[] = [];
  let driftHidden = 0;
  let systemManagedTotal = 0;

  // Iterate PRIMARY cards in resourceCards()'s own sorted order (drift-first
  // tier, then adopt-rank within it) and bucket each row by status. Because
  // the outer loop already visits cards in that order, each bucket inherits
  // it as its own within-bucket order — no new ordering is invented here.
  for (const card of primary) {
    const typeLabel = infraTypeLabel(card.assetType, card.label, t);
    for (const row of card.rows) {
      const estateRow: EstateRow = {
        nodeId: row.nodeId,
        label: row.label,
        assetType: card.assetType,
        typeLabel,
        status: row.status,
        adoptable: row.adoptable,
        prefill: row.prefill,
        pendingPr:
          row.status === 'drift' ? findPendingPr(cleanApprovals, card.assetType, row.label) : null,
      };
      if (row.status === 'drift') drift.push(estateRow);
      else if (row.status === 'managed') managed.push(estateRow);
      else untracked.push(estateRow); // 'untracked' — the only remaining ResourceRowStatus in .rows
    }
    driftHidden += card.hiddenUnmanaged;
    systemManagedTotal += card.systemManagedTotal;
    for (const row of card.systemManaged) {
      systemManaged.push({
        nodeId: row.nodeId,
        label: row.label,
        assetType: card.assetType,
        typeLabel,
        status: row.status,
        adoptable: false,
        prefill: '',
        pendingPr: null,
      });
    }
  }

  const otherResources = other.reduce((acc, c) => acc + c.count, 0);

  return {
    drift,
    driftHidden,
    managed,
    untracked,
    systemManaged,
    systemManagedTotal,
    otherTypes: other.length,
    otherResources,
  };
}

/**
 * The first drift row that would actually render a clickable Adopt chip —
 * `adoptable` AND no open adoption PR already covers it (a PR'd row renders
 * the non-interactive "PR #N awaiting review" chip instead, so spotlighting
 * it would point the tour at nothing to click). The tour's "Adopt your first
 * resource" step (lib/tour.ts TOUR_STEPS) and App.svelte's nav-button fallback
 * both call this SAME function so the two `data-tour="adopt-target"` markers
 * (row vs. nav button) can never both — or neither — be present at once.
 */
export function firstAdoptableRow(model: EstateModel): EstateRow | null {
  return model.drift.find((r) => r.adoptable && r.pendingPr === null) ?? null;
}
