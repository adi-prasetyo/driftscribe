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
  normalizeForPrompt,
  prefillLocation,
  resourceCards,
  scopeTotals,
  type InfraGraph,
} from './infra_graph';
import { coveragePercent } from './coverage';

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
 * not arrive on an errand (?ask_pr / ?preview_pr deep links from the
 * approval page) — interrupting intent is worse than not offering.
 */
export function shouldOfferTour(search: string, done: boolean): boolean {
  if (done) return false;
  const params = new URLSearchParams(search);
  return params.get('ask_pr') === null && params.get('preview_pr') === null;
}

export type TourStepId = 'welcome' | 'estate' | 'controls' | 'adopt' | 'next';

export interface TourStep {
  id: TourStepId;
  title: string;
  /** data-tour attribute of the page element to spotlight; null = none. */
  target: string | null;
}

export const TOUR_STEPS: readonly TourStep[] = [
  { id: 'welcome', title: 'Welcome', target: null },
  { id: 'estate', title: 'Your estate', target: 'estate' },
  { id: 'controls', title: 'You set the pace', target: 'controls' },
  { id: 'adopt', title: 'Adopt your first resource', target: 'estate' },
  { id: 'next', title: 'What happens next', target: 'composer' },
];

/** Step 1 — the project is unknown until /infra/graph resolves. */
export function welcomeLine(graph: InfraGraph | null): string {
  const subject = graph?.project
    ? `the GCP project ${graph.project}`
    : 'your GCP project';
  return (
    `DriftScribe is a small crew keeping ${subject} honest, from creation ` +
    'onward, and it works as a loop. Provision stands infrastructure up: you ' +
    'describe a change, it opens the IaC pull request. Anchor then guards what ' +
    'is live. It runs on its own, the only crew that does, watching your Cloud ' +
    'Run config and reacting the moment it drifts from its contract. Patch ' +
    'keeps your dependencies current, and Explore answers questions read-only, ' +
    'including how DriftScribe itself works. Provision, Patch, and Explore ' +
    'wait for you to ask. Infrastructure applies and rollbacks always wait for ' +
    'your approval. Only routine dependency updates can run end-to-end, and ' +
    'only at the Propose + Apply setting.'
  );
}

/** Step 2 — live totals, or an honest loading/degraded line (T3). */
export function estateLine(graph: InfraGraph | null): string {
  if (graph === null) {
    return (
      'Your estate is still loading. The Infrastructure panel below will ' +
      'fill in shortly.'
    );
  }
  if (graph.degraded) {
    return (
      'The resource inventory is unavailable right now (Cloud Asset ' +
      'Inventory may still be initializing). You can keep going and check ' +
      'the panel later.'
    );
  }
  // Scope-aware to match the panel (design 2026-06-25 scope-split, Codex MF2):
  // coverage is over the resource types DriftScribe manages, with the rest
  // (Cloud Run revisions, container images, …) called out as out-of-scope so the
  // tour never contradicts the meter below it.
  const s = scopeTotals(resourceCards(graph), graph.totals.resources);
  const tail = 'The coverage meter below tracks your migration.';
  // Nothing in scope: a single clean clause (avoid "None… The other N", which
  // contradicts itself — Workflow finding).
  if (s.resources === 0) {
    const body =
      s.otherResources > 0
        ? 'none are in resource types DriftScribe supports. They are types like ' +
          'Cloud Run revisions and container images it does not manage'
        : 'none are in resource types DriftScribe supports yet';
    return `${s.totalResources} resources indexed, ${body}. ${tail}`;
  }
  const pct = coveragePercent(s.managed, s.resources);
  const pctPart = pct === null ? '' : ` (${pct}%)`;
  const scopeSentence =
    `In the resource types DriftScribe supports, ${s.managed} of ${s.resources} ` +
    `are under IaC management${pctPart}, ${s.drift} not yet.`;
  const otherSentence =
    s.otherResources > 0
      ? ` The other ${s.otherResources} are types it does not manage, like Cloud Run ` +
        'revisions and container images.'
      : '';
  return `${s.totalResources} resources indexed. ${scopeSentence}${otherSentence} ${tail}`;
}

// Step 3 — honesty T2: the always-gated claim is scoped to INFRASTRUCTURE
// edits; Propose + Apply is allowed to finish routine dependency updates.
export const CONTROLS_LINE =
  'The Mode control in the top bar governs what Anchor does on its own when it ' +
  'spots a change, and what the other agents may do when you ask: Observe (they ' +
  'only watch and report), Propose (they draft changes for your review), or ' +
  'Propose + Apply (they may also complete routine dependency updates ' +
  'end-to-end). At every setting, infrastructure edits pass your explicit ' +
  'approval gate. The Pause control sits next to it in the top bar and suspends ' +
  'all agent activity in one click.';

// Step 5 — what sending the prefilled request actually does, and how to
// reopen the tour. Honesty T6 (Codex MF1): scoped to THIS adopt request —
// a blanket "nothing is applied until you approve" would overclaim, since
// Propose + Apply may merge dependency PRs on its own.
export const NEXT_LINE =
  'When you send this adopt request, the agent drafts it as a GitHub pull ' +
  'request with a plan you can read in plain language: what it changes, ' +
  'what it can never touch, and what it is estimated to cost. The ' +
  'infrastructure change is applied only after you approve it on the ' +
  'review page. You can reopen this tour anytime from the Tour button in ' +
  'the header.';

export type AdoptStepState =
  | { kind: 'unavailable'; line: string }
  | { kind: 'none'; line: string }
  | { kind: 'target'; line: string; prefill: string };

/**
 * Step 4 — the first-adoption suggestion. Candidate order mirrors the
 * panel's adopt list exactly (non-sensitive, adoptable, has an unmanaged
 * node; sorted by adoptGroupRank with unranked last, stable). The hint is
 * shown only when the group is RANKED — same rule as InfraDiagram.
 */
export function adoptStepState(graph: InfraGraph | null): AdoptStepState {
  if (graph === null || graph.degraded) {
    return {
      kind: 'unavailable',
      line:
        'The estate inventory is not available yet, so the tour cannot ' +
        'suggest a first adoption. When it returns, the Adopt buttons live ' +
        'in the Infrastructure panel.',
    };
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
        normalizeForPrompt(n.label, 254) !== '',
    );
    if (!node) continue;
    const hint =
      rank !== null && typeof g.adopt_hint === 'string' && g.adopt_hint
        ? g.adopt_hint
        : null;
    return {
      kind: 'target',
      line:
        `A good first adoption: the ${g.label} \`${node.label}\`. Adopting ` +
        'imports a resource into IaC exactly as it is. This zero-change ' +
        'import goes through the same review and approval as any ' +
        `other change.${hint ? ` ${hint}` : ''}`,
      prefill: adoptPrefill(g.label, node.label, prefillLocation(g.asset_type, node.location), node.topic ?? null, node.image ?? null),
    };
  }
  if (graph.totals.drift === 0) {
    return {
      kind: 'none',
      line:
        'Everything in your estate is already under IaC management, so ' +
        'there is nothing left to adopt. You are ahead of this tour.',
    };
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
  if (!hiddenActionable && unmanagedShown.length > 0 && nonControlPlane.length === 0) {
    return {
      kind: 'none',
      line:
        'The unmanaged resources the agent could otherwise adopt are ' +
        'system-managed infrastructure: DriftScribe control-plane services ' +
        'and IaC state/artifact buckets, or resources a Google service ' +
        'auto-creates, like Cloud Build buckets and Eventarc trigger ' +
        'transport. The always-on denylist blocks the agent from ' +
        'changing these, adoption included. The Infrastructure panel shows ' +
        'everything that is there.',
    };
  }
  return hiddenActionable || nonControlPlane.length > 0
    ? {
        kind: 'none',
        line:
          'There are unmanaged resources the agent could adopt, but none ' +
          'has a named adopt target the tour can prefill. The ' +
          'Infrastructure panel shows what the live graph can show.',
      }
    : {
        kind: 'none',
        line:
          'Your remaining unmanaged resources are not adoptable types. ' +
          'The Infrastructure panel shows what is there, and you can ask ' +
          'about any of them in chat.',
      };
}
