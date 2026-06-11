# Onboarding Tour (ClickOps roadmap item 14) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

> **Codex plan review (thread 019eb76d):** round 1 NO-GO ×5 — all folded: (MF1) NEXT_LINE scoped to *this adopt request* — the old "Nothing is applied until you approve it" overclaimed vs propose_apply's autonomous dependency merges; (MF2) adoptStepState skips nodes whose normalized label is empty (no empty-backtick prefill); (MF3) honest busy note on the final step when chat is disabled; (MF4) `.tour-target { display: flow-root; }` so child margins can't collapse outside the spotlight box; (MF5) new App.test.ts smoke pinning boot suppression / flag marking / header reopen / graph lift. Should-fixes folded: per-test scrollIntoView mock, localStorage-throws tests, welcomeLine softened ("helps you bring it under IaC management"), header flex-wrap.

**Goal:** A first-run guided tour of the operator SPA — banner-offered, header-button-reopenable — that walks a new ClickOps-migrant operator through their estate, the controls they hold, and ends by prefilling (never sending) their first adopt request.

**Architecture:** Frontend-only. A pure `lib/tour.ts` (step definitions, all copy, localStorage flag, first-adopt-target selection) + two components: `TourBanner.svelte` (dismissible first-visit offer) and `TourCard.svelte` (docked, NON-modal step card that spotlights the *real* panels via `[data-tour]` attributes + a `.tour-spotlight` class — no positioning library). The card reads the same `/infra/graph` payload the Infrastructure panel already fetched, lifted via a new `onGraph` callback prop. The adopt step routes through the existing `handleAdopt` prefill bridge.

**Tech Stack:** Svelte 5 (runes), TypeScript, vitest + @testing-library/svelte. Zero backend changes — no new endpoint, no gate/denylist change (⇒ coordinator rebake only at ship time, **no tofu-editor rebake**), zero pytest changes (baseline stays 2824).

---

## Design decisions (locked with the user, 2026-06-12)

1. **Scope:** Guided in-SPA tour. "Connect project" is reframed as *confirm connection* — the GCP project is fixed at deploy time; the tour shows which project this instance watches. No setup-script wizard, no new backend surface.
2. **Trigger:** Dismissible banner when `localStorage['driftscribe_tour_done']` is absent **and** the URL carried no intent param (`?ask_pr` / `?preview_pr` — an operator arriving on an errand is not interrupted). A permanent "Tour" button in the header reopens it anytime (what makes it demoable on prod, where the estate is not fresh).
3. **Form:** Docked bottom-right step card + real-UI spotlight (scrollIntoView + outline class on the matching `[data-tour]` element). The operator learns the actual panels, not a modal replica.
4. **Ending:** The adopt step prefills the composer via the existing Adopt bridge — **prefilled, never auto-sent** — and the final card honestly explains what happens after Send. No PR-lifecycle tracking.

## Honesty ledger (contractual — tests pin the load-bearing phrases)

- **T1 — copy is confidence-framing, NEVER safety** (item-10 lesson). No "safe"/"safety" promises anywhere in tour copy.
- **T2 — the controls step must not overclaim the approval gate.** In Propose + Apply the upgrade workload may merge its own dependency PR (`upgrade_merge_pr` is the sole apply-tier tool). The copy therefore scopes the always-gated claim to *infrastructure edits* and admits the agent "may also complete routine dependency updates end-to-end".
- **T3 — estate numbers are live or absent, never invented.** `estateLine` renders real totals, an honest "still loading" line, or an honest degraded line. Same for the adopt step (`unavailable` state) — the tour never names a resource it didn't read from the graph.
- **T4 — prefill-never-send.** The tour's adopt button calls the same `onAdopt`/`handleAdopt` bridge as the panel button and is disabled under the same `chatDisabled` condition. The card states "nothing is sent until you press Send."
- **T5 — the all-managed branch must not lie about non-adoptable leftovers.** `drift === 0` → congratulations; `drift > 0` with no adoptable candidate → "remaining unmanaged resources are not adoptable types yet", not "everything is managed".
- **T6 — the what-next step is scoped to *this adopt request*** (Codex MF1). It must NOT claim "nothing is applied until you approve" in general — in Propose + Apply the upgrade workload may merge its own dependency PR. The claim is: *the infrastructure change* is applied only after review-page approval.
- **T7 — never name what the graph didn't name** (Codex MF2). A node whose normalized label is empty is skipped as an adopt target — no empty-backtick prefill, no blank resource name in copy.

## Grounding facts (verified 2026-06-12)

- `App.svelte` renders, in order inside `#chat-area`: `PauseControl`, `AutonomyControl`, `InfraDiagram`, `CapabilityCard`, `ChatForm`, … (`frontend/src/App.svelte:401-430`). `.chat-area > :global(*)` provides `margin-bottom: var(--ds-sp-4)` to **direct children only** — wrapper divs need a `* + *` rule to restore inner spacing.
- `handleAdopt(text)` (App.svelte:104-109) sets `chatPrefill = { text, workload: 'provision', epoch: prev+1 }` and scrolls `#chat-form` into view. The tour reuses it verbatim.
- `InfraDiagram` fetches `/infra/graph` itself; `refresh()` applies the body at `graph = body; error = null;` (InfraDiagram.svelte:218-219) under last-applied-wins. The `onGraph` hook goes exactly there.
- `adoptGroupRank` + `adoptPrefill` are exported from `lib/infra_graph.ts` (lines 317-369); `coveragePercent` from `lib/coverage.ts`. `InfraGroup.adopt_hint` is shown only when the group is *ranked* (InfraDiagram.svelte:147-149) — the tour mirrors that rule.
- `AuthPanel` is `position: fixed; z-index: 100` — the tour card uses `z-index: 50` so the auth modal always wins.
- Buttons: `.ds-btn` with `--approve` / `--reject` / `--ghost` variants (`src/styles/base.css:318-375`). Cards: `.ds-card`. Shadows: `--ds-shadow-lg`.
- Frontend scripts: `npm run test:unit` (vitest, baseline **490**), `npm run check` (svelte-check), `npm run build`. jsdom does **not** implement `scrollIntoView` — component tests stub `window.HTMLElement.prototype.scrollIntoView`.
- Component-test pattern: `render(Component, { props })` + `getByTestId`/`queryByTestId` (`tests/unit/IacApprovalCta.test.ts`), `afterEach(cleanup)`.

---

### Task 1: `lib/tour.ts` — done-flag + banner-offer logic

**Files:**
- Create: `frontend/src/lib/tour.ts`
- Test: `frontend/tests/unit/tour.test.ts`

**Step 1: Write the failing tests**

```ts
// frontend/tests/unit/tour.test.ts
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  TOUR_DONE_KEY,
  tourDone,
  markTourDone,
  shouldOfferTour,
} from '../../src/lib/tour';

describe('tour done flag (localStorage)', () => {
  beforeEach(() => window.localStorage.clear());

  it('tourDone is false on a fresh profile', () => {
    expect(tourDone()).toBe(false);
  });

  it('markTourDone persists and tourDone reads it back', () => {
    markTourDone();
    expect(window.localStorage.getItem(TOUR_DONE_KEY)).toBe('1');
    expect(tourDone()).toBe(true);
  });

  it('swallows storage failures (strict privacy modes)', () => {
    const get = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    const set = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    try {
      expect(tourDone()).toBe(false);
      expect(() => markTourDone()).not.toThrow();
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
});

describe('shouldOfferTour', () => {
  it('offers on a clean first visit', () => {
    expect(shouldOfferTour('', false)).toBe(true);
    expect(shouldOfferTour('?other=1', false)).toBe(true);
  });

  it('never offers once done', () => {
    expect(shouldOfferTour('', true)).toBe(false);
  });

  it('suppressed when the operator arrived with intent (?ask_pr / ?preview_pr)', () => {
    expect(shouldOfferTour('?ask_pr=102', false)).toBe(false);
    expect(shouldOfferTour('?preview_pr=7', false)).toBe(false);
  });
});
```

**Step 2: Run to verify failure**

Run: `cd frontend && npx vitest run tests/unit/tour.test.ts`
Expected: FAIL — module `../../src/lib/tour` not found.

**Step 3: Minimal implementation**

```ts
// frontend/src/lib/tour.ts
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
```

**Step 4: Run to verify pass**

Run: `cd frontend && npx vitest run tests/unit/tour.test.ts`
Expected: PASS (5 tests).

**Step 5: Commit**

```bash
git add frontend/src/lib/tour.ts frontend/tests/unit/tour.test.ts
git commit -m "feat(ui): tour done-flag + banner-offer logic (item 14)"
```

---

### Task 2: `lib/tour.ts` — steps, copy, and the adopt-step state machine

**Files:**
- Modify: `frontend/src/lib/tour.ts`
- Test: `frontend/tests/unit/tour.test.ts` (append)

**Step 1: Write the failing tests** (append to `tour.test.ts`)

```ts
import {
  TOUR_STEPS,
  welcomeLine,
  estateLine,
  CONTROLS_LINE,
  NEXT_LINE,
  adoptStepState,
} from '../../src/lib/tour';
import type { InfraGraph, InfraGroup, InfraNode } from '../../src/lib/infra_graph';

function makeNode(over: Partial<InfraNode> = {}): InfraNode {
  return {
    id: 'g0n0',
    label: 'demo-bucket',
    asset_type: 'storage.googleapis.com/Bucket',
    managed: false,
    location: 'asia-northeast1',
    ...over,
  };
}

function makeGroup(over: Partial<InfraGroup> = {}): InfraGroup {
  return {
    asset_type: 'storage.googleapis.com/Bucket',
    label: 'Storage bucket',
    count: 1,
    managed: 0,
    drift: 1,
    sensitive: false,
    nodes: [makeNode()],
    adoptable: true,
    ...over,
  };
}

function makeGraph(over: Partial<InfraGraph> = {}): InfraGraph {
  return {
    generated_at: null,
    project: 'driftscribe-hack-2026',
    caveat: 'CAI may lag recent changes.',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 12, managed: 9, drift: 3 },
    groups: [makeGroup()],
    edges: [],
    ...over,
  };
}

describe('TOUR_STEPS', () => {
  it('is the locked 5-step sequence with spotlight targets', () => {
    expect(TOUR_STEPS.map((s) => s.id)).toEqual([
      'welcome',
      'estate',
      'controls',
      'adopt',
      'next',
    ]);
    expect(TOUR_STEPS.map((s) => s.target)).toEqual([
      null,
      'estate',
      'controls',
      'estate',
      'composer',
    ]);
  });
});

describe('step copy', () => {
  it('welcomeLine names the project when known, generic otherwise', () => {
    expect(welcomeLine(makeGraph())).toContain(
      'the GCP project driftscribe-hack-2026',
    );
    expect(welcomeLine(null)).toContain('your GCP project');
    // Honesty T1/T2: approval framing without a safety promise.
    expect(welcomeLine(null)).toContain('only after you approve it');
    expect(welcomeLine(null).toLowerCase()).not.toContain('safe');
  });

  it('estateLine renders live totals with coverage percent', () => {
    expect(estateLine(makeGraph())).toBe(
      '12 resources indexed — 9 under IaC management (75%), 3 not yet. ' +
        'The coverage meter below tracks your migration.',
    );
  });

  it('estateLine is honest while loading and when degraded (T3)', () => {
    expect(estateLine(null)).toContain('still loading');
    expect(estateLine(makeGraph({ degraded: true }))).toContain('unavailable');
  });

  it('CONTROLS_LINE scopes the gate claim to infrastructure edits (T2)', () => {
    expect(CONTROLS_LINE).toContain('infrastructure edits pass your explicit approval gate');
    expect(CONTROLS_LINE).toContain('routine dependency updates');
    expect(CONTROLS_LINE).toContain('Pause');
    expect(CONTROLS_LINE.toLowerCase()).not.toContain('safety');
  });

  it('NEXT_LINE is scoped to THIS request and the review-page gate (T6)', () => {
    expect(NEXT_LINE).toContain('this adopt request');
    expect(NEXT_LINE).toContain('pull request');
    expect(NEXT_LINE).toContain(
      'applied only after you approve it on the review page',
    );
    expect(NEXT_LINE).toContain('Tour button');
    // The old blanket claim must not return — propose_apply may merge
    // dependency PRs on its own (Codex MF1).
    expect(NEXT_LINE).not.toContain('Nothing is applied');
  });
});

describe('adoptStepState', () => {
  it('unavailable while loading or degraded (T3)', () => {
    expect(adoptStepState(null).kind).toBe('unavailable');
    expect(adoptStepState(makeGraph({ degraded: true })).kind).toBe('unavailable');
  });

  it('picks the rank-1 group first (same order as the panel)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          asset_type: 'run.googleapis.com/Service',
          label: 'Cloud Run service',
          adopt_rank: 2,
          nodes: [makeNode({ id: 'g0n0', label: 'svc-a', asset_type: 'run.googleapis.com/Service' })],
        }),
        makeGroup({
          adopt_rank: 1,
          adopt_hint: 'Buckets are the simplest first adoption.',
          nodes: [makeNode({ id: 'g1n0', label: 'demo-bucket' })],
        }),
      ],
    });
    const s = adoptStepState(g);
    expect(s.kind).toBe('target');
    if (s.kind !== 'target') throw new Error('unreachable');
    expect(s.prefill).toBe(
      'Adopt the Storage bucket `demo-bucket` in asia-northeast1 into IaC management.',
    );
    expect(s.line).toContain('demo-bucket');
    expect(s.line).toContain('Buckets are the simplest first adoption.');
    expect(s.line).toContain('zero-change import');
  });

  it('skips sensitive and non-adoptable groups and managed nodes', () => {
    const g = makeGraph({
      groups: [
        makeGroup({ sensitive: true, nodes: [] }),
        makeGroup({ adoptable: false, label: 'Project' }),
        makeGroup({ nodes: [makeNode({ managed: true })] }),
        makeGroup({ nodes: [makeNode({ id: 'g3n0', label: 'pick-me' })] }),
      ],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('pick-me');
  });

  it('an unranked adoptable group still yields a target, without a hint', () => {
    const g = makeGraph({
      groups: [makeGroup({ adopt_hint: 'should not show — unranked' })],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.line).not.toContain('should not show');
  });

  it('skips nodes with an empty normalized label — no empty-backtick prefill (T7)', () => {
    const g = makeGraph({
      groups: [
        makeGroup({
          nodes: [
            makeNode({ id: 'g0n0', label: '   ' }), // normalizes to ''
            makeNode({ id: 'g0n1', label: 'named-bucket' }),
          ],
        }),
      ],
    });
    const s = adoptStepState(g);
    if (s.kind !== 'target') throw new Error('expected target');
    expect(s.prefill).toContain('named-bucket');

    const allUnnamed = adoptStepState(
      makeGraph({ groups: [makeGroup({ nodes: [makeNode({ label: ' ' })] })] }),
    );
    expect(allUnnamed.kind).toBe('none');
  });

  it('all-managed congratulates; non-adoptable leftovers stay honest (T5)', () => {
    const allManaged = adoptStepState(
      makeGraph({
        totals: { resources: 9, managed: 9, drift: 0 },
        groups: [makeGroup({ drift: 0, nodes: [makeNode({ managed: true })] })],
      }),
    );
    expect(allManaged.kind).toBe('none');
    expect(allManaged.line).toContain('already under IaC management');

    const leftovers = adoptStepState(
      makeGraph({ groups: [makeGroup({ adoptable: false })] }),
    );
    expect(leftovers.kind).toBe('none');
    expect(leftovers.line).toContain('not adoptable types yet');
    expect(leftovers.line).not.toContain('already under IaC management');
  });
});
```

**Step 2: Run to verify failure**

Run: `cd frontend && npx vitest run tests/unit/tour.test.ts`
Expected: FAIL — `TOUR_STEPS` etc. not exported.

**Step 3: Implementation** (append to `frontend/src/lib/tour.ts`)

```ts
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
    `DriftScribe watches ${subject} and helps you bring it under ` +
    'infrastructure-as-code management. It explains what it sees, proposes ' +
    'changes as pull requests you can read, and applies an infrastructure ' +
    'change only after you approve it.'
  );
}

/** Step 2 — live totals, or an honest loading/degraded line (T3). */
export function estateLine(graph: InfraGraph | null): string {
  if (graph === null) {
    return (
      'Your estate is still loading — the Infrastructure panel below will ' +
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
  const { resources, managed, drift } = graph.totals;
  const pct = coveragePercent(managed, resources);
  const pctPart = pct === null ? '' : ` (${pct}%)`;
  return (
    `${resources} resources indexed — ${managed} under IaC management` +
    `${pctPart}, ${drift} not yet. The coverage meter below tracks your ` +
    'migration.'
  );
}

// Step 3 — honesty T2: the always-gated claim is scoped to INFRASTRUCTURE
// edits; Propose + Apply is allowed to finish routine dependency updates.
export const CONTROLS_LINE =
  'You decide how much the agent does on its own: Observe (it only watches ' +
  'and reports), Propose (it drafts changes for your review), or Propose + ' +
  'Apply (it may also complete routine dependency updates end-to-end). At ' +
  'every setting, infrastructure edits pass your explicit approval gate — ' +
  'and the Pause button suspends all agent activity in one click.';

// Step 5 — what sending the prefilled request actually does, and how to
// reopen the tour. Honesty T6 (Codex MF1): scoped to THIS adopt request —
// a blanket "nothing is applied until you approve" would overclaim, since
// Propose + Apply may merge dependency PRs on its own.
export const NEXT_LINE =
  'When you send this adopt request, the agent drafts it as a GitHub pull ' +
  'request with a plan you can read in plain language — what it changes, ' +
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
    const node = g.nodes.find(
      (n) => !n.managed && normalizeForPrompt(n.label, 254) !== '',
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
        'imports a resource into IaC exactly as it is — a zero-change ' +
        'import that goes through the same review and approval as any ' +
        `other change.${hint ? ` ${hint}` : ''}`,
      prefill: adoptPrefill(g.label, node.label, node.location),
    };
  }
  return graph.totals.drift === 0
    ? {
        kind: 'none',
        line:
          'Everything in your estate is already under IaC management — ' +
          'there is nothing left to adopt. You are ahead of this tour.',
      }
    : {
        kind: 'none',
        line:
          'Your remaining unmanaged resources are not adoptable types yet. ' +
          'The Infrastructure panel lists them, and you can ask about any ' +
          'of them in chat.',
      };
}
```

**Step 4: Run to verify pass**

Run: `cd frontend && npx vitest run tests/unit/tour.test.ts`
Expected: PASS.

**Step 5: Commit**

```bash
git add frontend/src/lib/tour.ts frontend/tests/unit/tour.test.ts
git commit -m "feat(ui): tour steps, honesty-pinned copy, adopt-step state machine"
```

---

### Task 3: `TourBanner.svelte`

**Files:**
- Create: `frontend/src/components/TourBanner.svelte`
- Test: `frontend/tests/unit/TourBanner.test.ts`

**Step 1: Write the failing tests**

```ts
// frontend/tests/unit/TourBanner.test.ts
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import TourBanner from '../../src/components/TourBanner.svelte';

afterEach(cleanup);

describe('TourBanner', () => {
  it('renders the offer copy', () => {
    const { getByTestId } = render(TourBanner, { props: {} });
    expect(getByTestId('tour-banner').textContent).toContain('5-minute tour');
  });

  it('Start fires onStart; Dismiss fires onDismiss', async () => {
    const onStart = vi.fn();
    const onDismiss = vi.fn();
    const { getByTestId } = render(TourBanner, { props: { onStart, onDismiss } });
    await fireEvent.click(getByTestId('tour-banner-start'));
    expect(onStart).toHaveBeenCalledTimes(1);
    await fireEvent.click(getByTestId('tour-banner-dismiss'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
```

**Step 2: Run to verify failure** — `npx vitest run tests/unit/TourBanner.test.ts` → FAIL (component missing).

**Step 3: Implementation**

```svelte
<!-- frontend/src/components/TourBanner.svelte -->
<script lang="ts">
  // TourBanner — the first-visit tour offer (item 14). Shown by App only when
  // shouldOfferTour() said yes; dismissing marks the tour done (the header
  // Tour button remains the permanent reopen path).
  let {
    onStart,
    onDismiss,
  }: {
    onStart?: () => void;
    onDismiss?: () => void;
  } = $props();
</script>

<div class="ds-card tour-banner" data-testid="tour-banner" role="note">
  <div class="tour-banner__text">
    <p class="tour-banner__lead">New here? Take the 5-minute tour.</p>
    <p class="ds-subtle tour-banner__sub">
      See your estate, the controls you hold, and how to adopt your first
      resource into IaC.
    </p>
  </div>
  <div class="tour-banner__actions">
    <button
      class="ds-btn ds-btn--approve"
      type="button"
      data-testid="tour-banner-start"
      onclick={() => onStart?.()}>Start the tour</button
    >
    <button
      class="ds-btn ds-btn--ghost"
      type="button"
      data-testid="tour-banner-dismiss"
      onclick={() => onDismiss?.()}>Dismiss</button
    >
  </div>
</div>

<style>
  .tour-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-4) var(--ds-sp-5);
  }
  .tour-banner__text {
    min-width: 0;
  }
  .tour-banner__lead {
    margin: 0;
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-bold);
  }
  .tour-banner__sub {
    margin: var(--ds-sp-1) 0 0;
  }
  .tour-banner__actions {
    display: inline-flex;
    gap: var(--ds-sp-2);
    flex: none;
  }
</style>
```

**Step 4: Run to verify pass.** **Step 5: Commit** (`feat(ui): tour offer banner`).

---

### Task 4: `TourCard.svelte` — navigation, copy rendering, adopt button

**Files:**
- Create: `frontend/src/components/TourCard.svelte`
- Test: `frontend/tests/unit/TourCard.test.ts`

**Step 1: Write the failing tests**

```ts
// frontend/tests/unit/TourCard.test.ts
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import TourCard from '../../src/components/TourCard.svelte';
import type { InfraGraph } from '../../src/lib/infra_graph';

// jsdom does not implement scrollIntoView — the spotlight effect calls it.
// Fresh mock per test (Codex should-fix: a shared beforeAll mock leaks call
// history across cases).
beforeEach(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

function graphWithTarget(): InfraGraph {
  return {
    generated_at: null,
    project: 'driftscribe-hack-2026',
    caveat: '',
    degraded: false,
    degraded_reason: null,
    totals: { resources: 12, managed: 9, drift: 3 },
    groups: [
      {
        asset_type: 'storage.googleapis.com/Bucket',
        label: 'Storage bucket',
        count: 1,
        managed: 0,
        drift: 1,
        sensitive: false,
        adoptable: true,
        adopt_rank: 1,
        adopt_hint: 'Buckets are the simplest first adoption.',
        nodes: [
          {
            id: 'g0n0',
            label: 'demo-bucket',
            asset_type: 'storage.googleapis.com/Bucket',
            managed: false,
            location: 'asia-northeast1',
          },
        ],
      },
    ],
    edges: [],
  };
}

async function advanceTo(getByTestId: (id: string) => HTMLElement, clicks: number) {
  for (let i = 0; i < clicks; i++) await fireEvent.click(getByTestId('tour-next'));
}

describe('TourCard — navigation', () => {
  it('starts at step 1 of 5 with the welcome copy and a disabled Back', () => {
    const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
    expect(getByTestId('tour-progress').textContent).toContain('1 of 5');
    expect(getByTestId('tour-body').textContent).toContain(
      'the GCP project driftscribe-hack-2026',
    );
    expect((getByTestId('tour-back') as HTMLButtonElement).disabled).toBe(true);
  });

  it('Next/Back walk the steps; the estate step shows live totals', async () => {
    const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
    await advanceTo(getByTestId, 1);
    expect(getByTestId('tour-progress').textContent).toContain('2 of 5');
    expect(getByTestId('tour-body').textContent).toContain('12 resources indexed');
    await fireEvent.click(getByTestId('tour-back'));
    expect(getByTestId('tour-progress').textContent).toContain('1 of 5');
  });

  it('the last step shows Finish (no Next) and Finish fires onClose', async () => {
    const onClose = vi.fn();
    const { getByTestId, queryByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onClose },
    });
    await advanceTo(getByTestId, 4);
    expect(queryByTestId('tour-next')).toBeNull();
    await fireEvent.click(getByTestId('tour-finish'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('the close button fires onClose from any step', async () => {
    const onClose = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onClose },
    });
    await fireEvent.click(getByTestId('tour-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('TourCard — adopt step (T4: prefill, never send)', () => {
  it('offers the prefill button and fires onAdoptPrefill with the exact prefill, then advances', async () => {
    const onAdoptPrefill = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), onAdoptPrefill },
    });
    await advanceTo(getByTestId, 3);
    expect(getByTestId('tour-progress').textContent).toContain('4 of 5');
    expect(getByTestId('tour-body').textContent).toContain('demo-bucket');
    await fireEvent.click(getByTestId('tour-adopt-btn'));
    expect(onAdoptPrefill).toHaveBeenCalledWith(
      'Adopt the Storage bucket `demo-bucket` in asia-northeast1 into IaC management.',
    );
    expect(getByTestId('tour-progress').textContent).toContain('5 of 5');
  });

  it('respects adoptDisabled (same condition as the panel buttons)', async () => {
    const onAdoptPrefill = vi.fn();
    const { getByTestId } = render(TourCard, {
      props: { graph: graphWithTarget(), adoptDisabled: true, onAdoptPrefill },
    });
    await advanceTo(getByTestId, 3);
    expect((getByTestId('tour-adopt-btn') as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(getByTestId('tour-adopt-btn'));
    expect(onAdoptPrefill).not.toHaveBeenCalled();
  });

  it('shows no button when there is nothing to adopt (T5)', async () => {
    const g = graphWithTarget();
    g.totals = { resources: 9, managed: 9, drift: 0 };
    g.groups = [];
    const { getByTestId, queryByTestId } = render(TourCard, { props: { graph: g } });
    await advanceTo(getByTestId, 3);
    expect(queryByTestId('tour-adopt-btn')).toBeNull();
    expect(getByTestId('tour-body').textContent).toContain('already under IaC management');
  });

  it('stays honest when the graph never loaded (T3)', async () => {
    const { getByTestId, queryByTestId } = render(TourCard, { props: { graph: null } });
    await advanceTo(getByTestId, 3);
    expect(queryByTestId('tour-adopt-btn')).toBeNull();
    expect(getByTestId('tour-body').textContent).toContain('not available yet');
  });

  it('final step shows the busy note only while chat is disabled (Codex MF3)', async () => {
    const busy = render(TourCard, {
      props: { graph: graphWithTarget(), adoptDisabled: true },
    });
    await advanceTo(busy.getByTestId, 4);
    expect(busy.getByTestId('tour-busy-note').textContent).toContain('busy');
    cleanup();

    const idle = render(TourCard, { props: { graph: graphWithTarget() } });
    await advanceTo(idle.getByTestId, 4);
    expect(idle.queryByTestId('tour-busy-note')).toBeNull();
  });
});
```

**Step 2: Run to verify failure.**

**Step 3: Implementation**

```svelte
<!-- frontend/src/components/TourCard.svelte -->
<script lang="ts">
  // TourCard — the docked, NON-modal step card for the onboarding tour
  // (roadmap item 14). It spotlights the REAL panels (scrollIntoView + a
  // .tour-spotlight outline on the matching [data-tour] element) instead of
  // re-rendering their data in a modal — the operator learns the actual UI.
  //
  // The adopt step routes through the SAME prefill bridge as the panel's
  // Adopt buttons (App.handleAdopt): prefilled, NEVER auto-sent (T4), and
  // disabled under the same chatDisabled condition.
  import {
    TOUR_STEPS,
    welcomeLine,
    estateLine,
    CONTROLS_LINE,
    NEXT_LINE,
    adoptStepState,
  } from '../lib/tour';
  import type { InfraGraph } from '../lib/infra_graph';

  let {
    graph = null,
    adoptDisabled = false,
    onAdoptPrefill,
    onClose,
  }: {
    /** Lifted /infra/graph payload (InfraDiagram.onGraph); null until loaded. */
    graph?: InfraGraph | null;
    /** Same condition that disables ChatForm/Adopt (busy / historical replay). */
    adoptDisabled?: boolean;
    /** Routes through App.handleAdopt — prefills the composer, never sends. */
    onAdoptPrefill?: (prefill: string) => void;
    /** Close/Finish — App marks the tour done and unmounts this card. */
    onClose?: () => void;
  } = $props();

  let stepIndex = $state(0);
  const step = $derived(TOUR_STEPS[stepIndex]);
  const adoptState = $derived(adoptStepState(graph));

  // Spotlight the current step's target: toggle .tour-spotlight on the
  // matching [data-tour] element and scroll it into view. The effect cleanup
  // removes the class on step change and on unmount, so a closed tour never
  // leaves an outline behind.
  $effect(() => {
    const target = step.target;
    if (target === null) return;
    const el = document.querySelector(`[data-tour="${target}"]`);
    if (!(el instanceof HTMLElement)) return;
    el.classList.add('tour-spotlight');
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return () => el.classList.remove('tour-spotlight');
  });

  function next(): void {
    if (stepIndex < TOUR_STEPS.length - 1) stepIndex += 1;
  }
  function back(): void {
    if (stepIndex > 0) stepIndex -= 1;
  }
  function prefillAdopt(): void {
    if (adoptDisabled || adoptState.kind !== 'target') return;
    onAdoptPrefill?.(adoptState.prefill);
    next(); // flow straight into "what happens next" with the composer spotlit
  }
</script>

<aside class="ds-card tour-card" data-testid="tour-card" aria-label="Guided tour">
  <header class="tour-card__head">
    <span class="ds-label tour-card__title">{step.title}</span>
    <span class="ds-subtle tour-card__progress" data-testid="tour-progress"
      >{stepIndex + 1} of {TOUR_STEPS.length}</span
    >
    <button
      class="ds-btn ds-btn--ghost tour-card__close"
      type="button"
      aria-label="Close tour"
      data-testid="tour-close"
      onclick={() => onClose?.()}>×</button
    >
  </header>

  <p class="tour-card__body" data-testid="tour-body" aria-live="polite">
    {#if step.id === 'welcome'}{welcomeLine(graph)}
    {:else if step.id === 'estate'}{estateLine(graph)}
    {:else if step.id === 'controls'}{CONTROLS_LINE}
    {:else if step.id === 'adopt'}{adoptState.line}
    {:else}{NEXT_LINE}{/if}
  </p>

  {#if step.id === 'adopt' && adoptState.kind === 'target'}
    <div class="tour-card__action">
      <button
        class="ds-btn ds-btn--approve"
        type="button"
        data-testid="tour-adopt-btn"
        disabled={adoptDisabled}
        title={adoptDisabled
          ? 'Unavailable while the chat is busy or reviewing a past trace.'
          : undefined}
        onclick={prefillAdopt}>Prefill the request</button
      >
      <p class="ds-subtle tour-card__note">
        This only prefills the chat — nothing is sent until you press Send.
      </p>
    </div>
  {/if}

  {#if step.id === 'next' && adoptDisabled}
    <!-- Honesty (Codex MF3): the copy says "when you send" but Send is
         disabled right now (busy stream / historical replay) — say so. -->
    <p class="ds-subtle tour-card__note" data-testid="tour-busy-note">
      The chat is busy or showing a past trace right now — sending becomes
      available when it finishes.
    </p>
  {/if}

  <footer class="tour-card__nav">
    <button
      class="ds-btn ds-btn--ghost"
      type="button"
      data-testid="tour-back"
      disabled={stepIndex === 0}
      onclick={back}>Back</button
    >
    {#if stepIndex < TOUR_STEPS.length - 1}
      <button class="ds-btn" type="button" data-testid="tour-next" onclick={next}
        >Next</button
      >
    {:else}
      <button class="ds-btn" type="button" data-testid="tour-finish" onclick={() => onClose?.()}
        >Finish</button
      >
    {/if}
  </footer>
</aside>

<style>
  .tour-card {
    position: fixed;
    right: var(--ds-sp-4);
    bottom: var(--ds-sp-4);
    z-index: 50; /* below the AuthPanel modal (100) — auth always wins */
    width: min(360px, calc(100vw - 2 * var(--ds-sp-4)));
    padding: var(--ds-sp-4) var(--ds-sp-5);
    box-shadow: var(--ds-shadow-lg);
  }
  .tour-card__head {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }
  .tour-card__title {
    flex: 1 1 auto;
    margin: 0;
  }
  .tour-card__progress {
    flex: none;
    font-variant-numeric: tabular-nums;
  }
  .tour-card__close {
    flex: none;
    padding: 0 0.5em;
    font-size: var(--ds-fs-2);
    line-height: 1.4;
  }
  .tour-card__body {
    margin: var(--ds-sp-3) 0;
    font-size: var(--ds-fs-2);
    color: var(--ds-fg-soft);
  }
  .tour-card__action {
    margin: 0 0 var(--ds-sp-3);
  }
  .tour-card__note {
    margin: var(--ds-sp-2) 0 0;
  }
  .tour-card__nav {
    display: flex;
    justify-content: space-between;
    gap: var(--ds-sp-3);
  }

  /* The spotlight outline lives here (:global — it lands on App-owned
     wrappers). ds-ok tokens: confidence-green, matching the Start-here chip. */
  :global(.tour-spotlight) {
    outline: 2px solid var(--ds-ok);
    outline-offset: 3px;
    border-radius: var(--ds-radius);
  }
</style>
```

**Step 4: Run to verify pass.** **Step 5: Commit** (`feat(ui): docked tour step card with prefill-never-send adopt step`).

---

### Task 5: TourCard spotlight behavior test

**Files:**
- Test: `frontend/tests/unit/TourCard.test.ts` (append)

**Step 1: Write the failing-or-passing test** (behavior already implemented in Task 4 — this pins it)

```ts
describe('TourCard — spotlight', () => {
  it('toggles .tour-spotlight on the matching [data-tour] element per step', async () => {
    const estate = document.createElement('div');
    estate.setAttribute('data-tour', 'estate');
    document.body.appendChild(estate);
    try {
      const { getByTestId } = render(TourCard, { props: { graph: graphWithTarget() } });
      // step 1 (welcome): no target
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
      await fireEvent.click(getByTestId('tour-next')); // → estate
      expect(estate.classList.contains('tour-spotlight')).toBe(true);
      expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();
      await fireEvent.click(getByTestId('tour-next')); // → controls (absent in DOM)
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
    } finally {
      estate.remove();
    }
  });

  it('removes the spotlight on unmount', async () => {
    const estate = document.createElement('div');
    estate.setAttribute('data-tour', 'estate');
    document.body.appendChild(estate);
    try {
      const { getByTestId, unmount } = render(TourCard, {
        props: { graph: graphWithTarget() },
      });
      await fireEvent.click(getByTestId('tour-next'));
      expect(estate.classList.contains('tour-spotlight')).toBe(true);
      unmount();
      expect(estate.classList.contains('tour-spotlight')).toBe(false);
    } finally {
      estate.remove();
    }
  });
});
```

**Step 2-4: Run** — expected PASS immediately (pin, not drive). If it fails, the $effect cleanup is wrong — fix until green.

**Step 5: Commit** (`test(ui): pin tour spotlight class lifecycle`).

---

### Task 6: `InfraDiagram.onGraph` lift

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte` (props block ~line 41-66; `refresh()` ~line 218)
- Test: `frontend/tests/unit/InfraDiagram.test.ts` (append)

**Step 1: Write the failing test** (append; reuse the file's existing graph/`call` mock helpers — adapt names to what is already there)

```ts
it('reports each applied graph to onGraph (tour lift, item 14)', async () => {
  const onGraph = vi.fn();
  const graph = makeGraph(); // the file's existing builder
  const call = vi.fn(async () => jsonResponse(graph)); // the file's existing response helper
  render(InfraDiagram, { props: { call, onGraph } });
  await waitFor(() => expect(onGraph).toHaveBeenCalledTimes(1));
  expect(onGraph.mock.calls[0][0].totals).toEqual(graph.totals);
});
```

**Step 2: Run to verify failure** — unknown prop is ignored by Svelte, so the spy is never called → FAIL on `toHaveBeenCalledTimes(1)`.

**Step 3: Implementation** — add to the props destructure + type literal:

```ts
    onGraph,
```
```ts
    /**
     * Called with each successfully-applied /infra/graph payload (item 14):
     * App lifts the graph to the onboarding TourCard so the tour reads the
     * SAME data as this panel — no duplicate fetch, no second source of truth.
     */
    onGraph?: (g: InfraGraph) => void;
```

and in `refresh()`, immediately after `graph = body; error = null;`:

```ts
      onGraph?.(body);
```

**Step 4: Run the whole file** — `npx vitest run tests/unit/InfraDiagram.test.ts` → PASS.

**Step 5: Commit** (`feat(ui): InfraDiagram onGraph lift for the tour`).

---

### Task 7: App.svelte wiring — banner, card, header button, data-tour wrappers

**Files:**
- Modify: `frontend/src/App.svelte`

The wiring itself is pinned by Task 8's new App smoke test (Codex MF5). Changes:

**Step 1: Script additions**

```ts
  import TourBanner from './components/TourBanner.svelte';
  import TourCard from './components/TourCard.svelte';
  import { tourDone, markTourDone, shouldOfferTour } from './lib/tour';
  import type { InfraGraph } from './lib/infra_graph';
```

After the `chatPrefill` block (App.svelte:~109):

```ts
  // Onboarding tour (item 14). The offer is decided ONCE at boot — before
  // onMount strips the intent params — and the header Tour button is the
  // permanent reopen path. Closing OR dismissing marks the tour done; the
  // flag is a UI preference, so localStorage (not sessionStorage) is right.
  let tourGraph = $state<InfraGraph | null>(null);
  let tourOpen = $state(false);
  let tourOffered = $state(shouldOfferTour(window.location.search, tourDone()));
  function startTour(): void {
    tourOffered = false;
    tourOpen = true;
  }
  function dismissTourOffer(): void {
    tourOffered = false;
    markTourDone();
  }
  function closeTour(): void {
    tourOpen = false;
    markTourDone();
  }
```

**Step 2: Template changes**

Header — wrap the right side so the Tour button sits beside the token pill:

```svelte
<header class="app-header">
  <h1 class="app-title">DriftScribe <span class="app-title__sub">— Reasoning Timeline</span></h1>
  <div class="app-header__actions">
    <button
      class="ds-btn ds-btn--ghost app-tour-btn"
      type="button"
      data-testid="tour-open"
      onclick={startTour}>Tour</button
    >
    <TokenStatus state={tokenState} onChange={onChangeToken} />
  </div>
</header>
```

Chat area — banner first; wrappers carry the spotlight markers (`.chat-area > :global(*)` reaches only direct children, so the controls wrapper needs the `* + *` rule below to keep PauseControl/AutonomyControl spaced):

```svelte
  <section id="chat-area" class="chat-area" aria-label="Chat and reasoning timeline">
    {#if tourOffered && !tourOpen}
      <TourBanner onStart={startTour} onDismiss={dismissTourOffer} />
    {/if}
    <div class="tour-target" data-tour="controls">
      <PauseControl {call} />
      <AutonomyControl {call} />
    </div>
    <div class="tour-target" data-tour="estate">
      <InfraDiagram
        {call}
        {appliedEpoch}
        {previewPr}
        onExitPreview={exitPreview}
        onAdopt={handleAdopt}
        adoptDisabled={chatDisabled}
        onGraph={(g) => (tourGraph = g)}
      />
    </div>
    <CapabilityCard {call} />
    <div class="tour-target" data-tour="composer">
      <ChatForm disabled={chatDisabled} onSubmit={submitChat} prefill={chatPrefill} />
    </div>
    ...rest unchanged...
  </section>
```

After `<AuthPanel … />`:

```svelte
{#if tourOpen}
  <TourCard
    graph={tourGraph}
    adoptDisabled={chatDisabled}
    onAdoptPrefill={handleAdopt}
    onClose={closeTour}
  />
{/if}
```

**Step 3: Style additions** (App.svelte `<style>`)

```css
  .app-header__actions {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
  }
  .app-tour-btn {
    padding: 0.3em 0.85em;
    font-size: var(--ds-fs-1);
  }
  /* Wrappers exist only as [data-tour] spotlight targets. flow-root makes
     each wrapper a BFC so child margins cannot collapse outside it — the
     spotlight outline must hug the real panels (Codex MF4). The `* + *`
     rule restores the inter-component spacing the children lost by no
     longer being .chat-area direct children. */
  .tour-target {
    display: flow-root;
  }
  .tour-target > :global(* + *) {
    margin-top: var(--ds-sp-4);
  }
```

Also add `flex-wrap: wrap;` to the existing `.app-header` rule (Codex should-fix: the action cluster must wrap cleanly on narrow screens now that it holds two elements).

**Step 4: Verify**

Run: `cd frontend && npm run check && npm run test:unit && npm run build`
Expected: svelte-check clean, full vitest suite green (490 baseline + new), build succeeds.

**Step 5: Commit** (`feat(ui): wire onboarding tour — banner, header button, spotlight targets`).

---

### Task 8: App smoke test — tour wiring (Codex MF5)

**Files:**
- Test: `frontend/tests/unit/App.test.ts` (NEW — the repo's first App-level test)

The riskiest tour behavior lives in App: boot-time intent suppression (decided *before* `onMount` strips `ask_pr`), flag marking on dismiss/close, header reopen, and the `onGraph` lift feeding the card. Every panel fetches on mount through `App.call → apiFetch → fetch`, so the smoke stubs global `fetch` with minimal 200 payloads (each component is defensive about shape, best-effort on failure — extend the URL map only if a component actually throws).

**Step 1: Write the failing test**

```ts
// frontend/tests/unit/App.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  history.replaceState(null, '', '/');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/graph'))
        return okJson({
          generated_at: null,
          project: 'demo-proj',
          caveat: '',
          degraded: false,
          degraded_reason: null,
          totals: { resources: 1, managed: 0, drift: 1 },
          groups: [],
          edges: [],
        });
      return okJson({});
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — tour wiring (smoke)', () => {
  it('offers the banner on a fresh profile; Start opens the card; close marks done', async () => {
    const { getByTestId, queryByTestId } = render(App);
    expect(getByTestId('tour-banner')).toBeTruthy();
    await fireEvent.click(getByTestId('tour-banner-start'));
    expect(queryByTestId('tour-banner')).toBeNull();
    expect(getByTestId('tour-card')).toBeTruthy();
    await fireEvent.click(getByTestId('tour-close'));
    expect(queryByTestId('tour-card')).toBeNull();
    expect(window.localStorage.getItem('driftscribe_tour_done')).toBe('1');
  });

  it('dismissing the banner marks done; the header button reopens the tour', async () => {
    const { getByTestId, queryByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-banner-dismiss'));
    expect(queryByTestId('tour-banner')).toBeNull();
    expect(window.localStorage.getItem('driftscribe_tour_done')).toBe('1');
    await fireEvent.click(getByTestId('tour-open'));
    expect(getByTestId('tour-card')).toBeTruthy();
  });

  it('suppresses the banner when arriving with ?ask_pr intent', () => {
    history.replaceState(null, '', '/?ask_pr=102');
    const { queryByTestId, getByTestId } = render(App);
    expect(queryByTestId('tour-banner')).toBeNull();
    // The permanent reopen path still exists.
    expect(getByTestId('tour-open')).toBeTruthy();
  });

  it('lifts the fetched graph into the tour (welcome step names the project)', async () => {
    const { getByTestId } = render(App);
    await fireEvent.click(getByTestId('tour-banner-start'));
    await waitFor(() =>
      expect(getByTestId('tour-body').textContent).toContain('demo-proj'),
    );
  });
});
```

**Step 2: Run** — `npx vitest run tests/unit/App.test.ts`. Before Task 7's wiring lands this FAILS (no banner testids); after Task 7 it must PASS. (If Tasks 7/8 are built together, write this test FIRST and watch it fail, then wire App.)

**Step 3-4: Make it pass** — fix wiring, not the test, unless a stubbed payload shape is genuinely wrong.

**Step 5: Commit** (`test(ui): App smoke for tour wiring — boot suppression, flag, reopen, graph lift`).

---

### Task 9: Full verification

- `cd frontend && npm run check && npm run test:unit && npm run build` — all green.
- `cd /home/adi/driftscribe && .venv/bin/pytest -q` — **2824 passed, unchanged** (no backend files touched; this run proves it).
- `.venv/bin/ruff check --no-cache .` — clean (no Python changes).
- `git status` clean; commit any test-snapshot strays consciously, never blindly.

---

## Ship checklist (standard pipeline, after Codex plan GO)

1. Feature branch `feat/onboarding-tour`, tasks 1-9 as individual commits.
2. PR → CI watch (`while gh pr checks N | grep -qE "pending|in_progress|queued"; do sleep 20; done`; plan-builder check "skipping" is expected).
3. Codex completed-work review on the same thread; fold should-fixes.
4. Squash-merge → coordinator rebake (`gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=<short-sha>`) → find revision **by image digest** → `update-traffic --to-revisions=<rev>=100`. **No tofu-editor rebake** (no gate/denylist change).
5. Live verify: (a) incognito/fresh profile → banner shows; (b) Start → card step 1 names project `driftscribe-hack-2026`; (c) estate step spotlights the Infrastructure panel with real totals; (d) adopt step offers "Prefill the request" → composer prefilled with the live start-here resource, workload pill = Provision, nothing sent; (e) Finish → `localStorage.driftscribe_tour_done === '1'`, reload → no banner; (f) header Tour button reopens; (g) `?ask_pr=102` arrival in a fresh profile → NO banner (intent suppression).
6. Memory + closing report.

## Out of scope (explicit)

- No backend/first-run endpoint (`/onboarding/status` rejected — the SPA already fetches every needed piece).
- No PR-lifecycle tracking in the tour (ends at prefill by design).
- No changes to the approval page, prompts, gates, or any Python.
