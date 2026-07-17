// tour.ts — pure logic for the first-run onboarding tour (roadmap item 14).
//
// The tour is a guided, NON-modal walkthrough of the real UI: a docked step
// card (TourCard.svelte) spotlights existing panels via [data-tour] markers
// and ends by prefilling — never sending — an adopt request through the same
// bridge as the panel's Adopt buttons. ALL step copy lives here (pure,
// unit-testable); the components only render it.
//
// HONESTY (item-10 lesson, pinned by tests): copy is confidence-framing,
// never a safety promise. The controls step deliberately scopes the
// always-gated claim to INFRASTRUCTURE edits — in Propose + Apply the
// upgrade workload may merge its own dependency PR.

import {
  adoptGroupRank,
  adoptPrefill,
  findPendingPr,
  infraTypeLabel,
  normalizeForPrompt,
  prefillLocation,
  resourceCards,
  scopeTotals,
  type InfraGraph,
  type PendingApproval,
} from './infra_graph';
import { coveragePercent } from './coverage';
import type { MessageKey, TranslateFn } from './i18n';

export const TOUR_DONE_KEY = 'driftscribe_tour_done';

/** Guarded read — localStorage can throw under strict privacy modes. */
export function tourDone(): boolean {
  try {
    return window.localStorage.getItem(TOUR_DONE_KEY) === '1';
  } catch {
    return false;
  }
}

export function markTourDone(): void {
  try {
    window.localStorage.setItem(TOUR_DONE_KEY, '1');
  } catch {
    /* best-effort — worst case the banner re-offers next visit */
  }
}

/**
 * Offer the banner? Only when the tour was never done AND the operator did
 * not arrive on an errand (?ask_pr / ?preview_pr deep links from the approval
 * page, or a ?reasoning shared replay link) — interrupting intent is worse
 * than not offering.
 */
export function shouldOfferTour(search: string, done: boolean): boolean {
  if (done) return false;
  const params = new URLSearchParams(search);
  return (
    params.get('ask_pr') === null &&
    params.get('preview_pr') === null &&
    params.get('reasoning') === null
  );
}

export type TourStepId = 'welcome' | 'estate' | 'controls' | 'adopt' | 'next';

export interface TourStep {
  id: TourStepId;
  /** i18n key for the step title; resolved by the component via $t. */
  titleKey: MessageKey;
  /** data-tour attribute of the page element to spotlight; null = none. */
  target: string | null;
}

export const TOUR_STEPS: readonly TourStep[] = [
  { id: 'welcome', titleKey: 'tour.step.welcome.title', target: null },
  { id: 'estate', titleKey: 'tour.step.estate.title', target: 'estate' },
  { id: 'controls', titleKey: 'tour.step.controls.title', target: 'controls' },
  { id: 'adopt', titleKey: 'tour.step.adopt.title', target: 'estate' },
  { id: 'next', titleKey: 'tour.step.next.title', target: 'composer' },
];

/** Step 1 — the project is unknown until /infra/graph resolves. */
export function welcomeLine(t: TranslateFn, graph: InfraGraph | null): string {
  const subject = graph?.project
    ? t('tour.welcome.subjectKnown', { project: graph.project })
    : t('tour.welcome.subjectUnknown');
  return t('tour.welcome.body', { subject });
}

/** Step 2 — live totals, or an honest loading/degraded line (T3). */
export function estateLine(t: TranslateFn, graph: InfraGraph | null): string {
  if (graph === null) return t('tour.estate.loading');
  if (graph.degraded) return t('tour.estate.degraded');
  // Scope-aware to match the panel (design 2026-06-25 scope-split, Codex MF2):
  // coverage is over the resource types DriftScribe manages, with the rest
  // (Cloud Run revisions, container images, …) called out as out-of-scope so the
  // tour never contradicts the meter below it.
  const s = scopeTotals(resourceCards(graph), graph.totals.resources);
  // Nothing in scope: a single clean clause (avoid "None… The other N", which
  // contradicts itself — Workflow finding).
  if (s.resources === 0) {
    return s.otherResources > 0
      ? t('tour.estate.zeroWithOther', { total: s.totalResources })
      : t('tour.estate.zeroAlone', { total: s.totalResources });
  }
  // resources > 0 here, so coveragePercent (null only when resources <= 0)
  // is always a number — the `?? 0` is a type-safety fallback, never live.
  const pct = coveragePercent(s.managed, s.resources) ?? 0;
  const params = {
    total: s.totalResources,
    managed: s.managed,
    resources: s.resources,
    pct,
    drift: s.drift,
  };
  return s.otherResources > 0
    ? t('tour.estate.inScopeWithOther', { ...params, other: s.otherResources })
    : t('tour.estate.inScope', params);
}

// Step 3 — honesty T2: the always-gated claim is scoped to INFRASTRUCTURE
// edits; Propose + Apply is allowed to finish routine dependency updates.
export function controlsLine(t: TranslateFn): string {
  return t('tour.controls.body');
}

// Step 5 — what sending the prefilled request actually does, and how to
// reopen the tour. Honesty T6 (Codex MF1): scoped to THIS adopt request —
// a blanket "nothing is applied until you approve" would overclaim, since
// Propose + Apply may merge dependency PRs on its own.
export function nextLine(t: TranslateFn): string {
  return t('tour.next.body');
}

export type AdoptStepState =
  | { kind: 'unavailable'; line: string }
  | { kind: 'none'; line: string }
  | { kind: 'target'; line: string; prefill: string };

/**
 * Step 4 — the first-adoption suggestion. Candidate order mirrors the
 * panel's adopt list exactly (non-sensitive, adoptable, has an unmanaged
 * node; sorted by adoptGroupRank with unranked last, stable). The hint is
 * shown only when the group is RANKED — same rule as InfraDiagram.
 *
 * `pendingApprovals` (optional — the panel's `/infra/pending-approvals` list,
 * lifted through App) lets the tour skip a resource that already has an open
 * adoption PR. Such a resource is still graph-unmanaged (the PR is not
 * merged/applied, so it is genuinely not in any .tf yet), so without this it
 * stayed the rank-1 pick — sending the operator to open a SECOND adoption of
 * something already in review. The panel already marks these with a blue
 * marker and `propose_adoption_tool` refuses the dupe; this brings the tour's
 * suggestion in line with both. Omitted/undefined = no filtering (unchanged).
 */
export function adoptStepState(
  t: TranslateFn,
  graph: InfraGraph | null,
  pendingApprovals?: PendingApproval[] | null,
): AdoptStepState {
  if (graph === null || graph.degraded) {
    return { kind: 'unavailable', line: t('tour.adopt.unavailable') };
  }
  const candidates = graph.groups
    .filter((g) => !g.sensitive && g.adoptable === true)
    .map((g) => ({ g, rank: adoptGroupRank(g) }))
    .sort(
      (a, b) =>
        (a.rank ?? Number.POSITIVE_INFINITY) -
        (b.rank ?? Number.POSITIVE_INFINITY),
    );
  for (const { g, rank } of candidates) {
    // T7 (Codex MF2): never suggest a node the graph didn't name — an empty
    // normalized label would yield an empty-backtick prefill and blank copy.
    // Control-plane nodes are skipped too: the denylist refuses their
    // adoption outright (ranking-filter follow-up — the live rank-1 pick was
    // DriftScribe's own -tofu-artifacts bucket). The same control_plane flag
    // also covers buckets a Google service auto-creates (e.g. _cloudbuild),
    // so those are skipped here as well.
    const node = g.nodes.find(
      (n) =>
        !n.managed &&
        n.control_plane !== true &&
        normalizeForPrompt(n.label, 254) !== '' &&
        // Skip a resource that already has an open adoption PR — don't send
        // the operator to adopt it a second time; the honest all-PR'd fall
        // through below points them at the open change instead.
        findPendingPr(pendingApprovals, g.asset_type, n.label) === null,
    );
    if (!node) continue;
    const hint =
      rank !== null && typeof g.adopt_hint === 'string' && g.adopt_hint
        ? g.adopt_hint
        : null;
    const typeLabel = infraTypeLabel(g.asset_type, g.label, t);
    return {
      kind: 'target',
      // `hint` is free backend prose with no stable id (like InfraDiagram's
      // degraded_reason/caveat) — it passes through untranslated.
      line: hint
        ? t('tour.adopt.target.withHint', { groupLabel: typeLabel, nodeLabel: node.label, hint })
        : t('tour.adopt.target.plain', { groupLabel: typeLabel, nodeLabel: node.label }),
      prefill: adoptPrefill(typeLabel, node.label, prefillLocation(g.asset_type, node.location), node.topic ?? null, node.image ?? null, t),
    };
  }
  if (graph.totals.drift === 0) {
    return { kind: 'none', line: t('tour.adopt.allManaged') };
  }
  // Distinguish WHY there is no suggestion (Codex 019eb76d round-2 + the
  // ranking-filter follow-up): control-plane-only ≠ unnamed ≠ no adoptable
  // type — each gets its own honest line.
  const unmanagedShown = candidates.flatMap(({ g }) =>
    g.nodes.filter((n) => !n.managed),
  );
  const nonControlPlane = unmanagedShown.filter((n) => n.control_plane !== true);
  // The per-type sample is capped, so a group can hold adoptable drift no
  // sampled row shows. Trust the aggregate `drift_adoptable` when present: if it
  // exceeds the adoptable (non-control-plane) rows on hand, there IS a target
  // the tour just can't name — never conclude "system-managed only" then.
  const hiddenActionable = candidates.some(({ g }) => {
    if (typeof g.drift_adoptable !== 'number' || !Number.isFinite(g.drift_adoptable)) return false;
    const shownActionable = g.nodes.filter(
      (n) => !n.managed && n.control_plane !== true,
    ).length;
    return g.drift_adoptable > shownActionable;
  });
  // Every named adopt target the tour could otherwise suggest already has an
  // open adoption PR (the loop above skips PR'd nodes, so reaching here with
  // named-actionable rows means they were ALL skipped for that reason). Point
  // the operator at the open change rather than the honest-but-wrong "no named
  // target" line below. Two guards keep the claim honest:
  //   - !hiddenActionable: if the aggregate says there is adoptable drift no
  //     sampled row shows, we can't claim ALL are PR'd.
  //   - nonControlPlane.length === namedActionable.length: no UNNAMED actionable
  //     row is present (an adoptable row whose name didn't resolve is not PR'd,
  //     it's un-nameable) — that case belongs to the "no named target" line
  //     below, not this one (Codex 019f4012).
  const namedActionable = candidates.flatMap(({ g }) =>
    g.nodes
      .filter(
        (n) =>
          !n.managed &&
          n.control_plane !== true &&
          normalizeForPrompt(n.label, 254) !== '',
      )
      .map((n) => ({ node: n, assetType: g.asset_type })),
  );
  if (
    !hiddenActionable &&
    namedActionable.length > 0 &&
    nonControlPlane.length === namedActionable.length &&
    namedActionable.every(
      ({ node, assetType }) =>
        findPendingPr(pendingApprovals, assetType, node.label) !== null,
    )
  ) {
    return { kind: 'none', line: t('tour.adopt.allPending') };
  }
  if (!hiddenActionable && unmanagedShown.length > 0 && nonControlPlane.length === 0) {
    return { kind: 'none', line: t('tour.adopt.systemManagedOnly') };
  }
  return hiddenActionable || nonControlPlane.length > 0
    ? { kind: 'none', line: t('tour.adopt.noNamedTarget') }
    : { kind: 'none', line: t('tour.adopt.notAdoptableTypes') };
}
