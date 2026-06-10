# Collapse duplicate `iac_apply` rail rows Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** One row per PR in the past-decisions rail for `iac_apply` decisions, expandable to the faithful per-document lifecycle — instead of 2–3 near-identical rows per PR.

**Architecture:** A pure grouping function (`frontend/src/lib/rail.ts`) folds the newest-first `/decisions` list into render items — `single` (unchanged row) or `group` (≥2 `iac_apply` docs sharing a PR number). `DecisionsRail.svelte` renders a group as today's row markup driven by the **newest** doc (the latest lifecycle state), plus a `<details>` expander listing the earlier steps with their status, time, and per-step "open trace". Frontend-only; zero backend change; supersession logic (`resolvedIacPrNumbers` / `iacApproveLabel`) is reused unchanged on the raw list.

**Tech Stack:** Svelte 5 (runes, snippets), TypeScript, Vitest + @testing-library/svelte (jsdom).

**Roadmap context:** Wave 1 item 4 of `docs/plans/2026-06-10-clickops-audience-roadmap.md` — deferred from PR #81, which kept the faithful 3-doc lifecycle visible as 3 rows and answered the operator's "why 3 duplicate rows per PR?" with per-row status tokens. This item finishes the answer: one row per PR, lifecycle on demand.

---

## Grounding facts (verified against live prod + code, 2026-06-10)

These are real observations, not assumptions. The implementer should trust them.

1. **Live `/decisions?limit=50` returns 12 docs, 11 of them `iac_apply`, across 4 PRs:**
   - PR **68**: `applied` (2026-06-05) + `waiting_for_rebake` ×2 (2026-06-04, written 7 s apart)
   - PR **66**: `applied` + `waiting_for_rebake` ×2 — same shape
   - PR **47**: `applied` + `waiting_for_rebake` ×2 — same shape
   - PR **32**: `applied` + `failed` — a failed→retried→applied lifecycle (2 docs)
   - Docs arrive **newest-first** (confirmed live; `App.svelte:128` comments the same).
2. **The two `waiting_for_rebake` docs of a create-class lifecycle share one `trace_id`** (e.g. both PR-68 waiting docs carry trace `484096827f0a…`); the `applied` doc has its own. So a 3-doc group typically has 2 distinct traces. Per-step "open trace" must therefore tolerate duplicate trace ids (two steps opening the same trace is correct and faithful).
3. **All docs in a group share `pr_number`, `head_sha`, `github.url`, and (post-backfill) `pr_title`.** `github.url` is derived at serve time from `pr_number` (`attach_iac_pr_link`), so every doc that can join a group (valid `pr_number`) also has `github.url` — no fallback needed for the PR link. `pr_title` is a **write-time** snapshot and fail-soft (`_fetch_pr_title`), so a future doc may lack it — the group face falls back to the newest sibling that has one.
4. **Current render path** (`frontend/src/components/DecisionsRail.svelte`): `{#each decisions as d (d.decision_id)}` renders every doc as a card — linked `PR #n →` title (`iacPrHref`, host-allowlisted), `pr_title` subtitle, meta line `iac_apply · <status> · ⎇ <sha>` (`iacStatusLabel`), `open trace →` button, and the `/iac-approvals/<n>` CTA labelled by `iacApproveLabel(d, resolvedPrs)`.
5. **Supersession lib** (`frontend/src/lib/approval.ts`): `resolvedIacPrNumbers(decisions)` = PR numbers with a terminal `applied` doc; `iacApproveLabel(d, resolvedPrs)` = `'Review & approve →'` only for a non-superseded `waiting_for_rebake` doc, else `'Open approval page →'`. Both operate on the **raw** list and stay untouched — `resolvedPrs` keeps being derived from `decisions`, not from grouped items.
6. **Valid-PR guard precedent:** `iacApprovalHref` accepts only a positive integer. The grouping key must mirror exactly that guard so a malformed `pr_number` can never form a group.
7. **Existing component test** `frontend/tests/unit/DecisionsRail.test.ts` (the repo's first @testing-library/svelte test, PR #81): its first case renders 3 same-PR docs and asserts **3** `iac-approve-link` elements. Collapsing makes that 1 link **by design** — that test is deliberately rewritten in Task 2 (the supersession assertion moves to the group CTA). Its third case uses PRs 68 + 71 with one doc each — those stay singles and the test still passes unchanged.
8. **e2e smoke** (`tests/e2e/ui/tests/transparency.spec.ts:46`) asserts only that ≥1 `[data-testid="past-decision-item"]` is visible — group rows must keep that testid; the reduced row count is fine.
9. **`App.svelte` is untouched**: `noteApplied` (infra-panel refresh trigger) reads the raw `decisions` array before any grouping; the rail keeps receiving the raw array as its prop.
10. **Svelte 5 whitespace rule (hard-won, PRs #83/#84):** a literal indent/newline between inline elements collapses to a render-time space, and an `{' '}` expression adds another → two spaces at a seam. Where exact text matters, **glue** the markup (`</span>{' '}<span`) so `{' '}` is the only seam whitespace, and pin with glued-exact-string assertions.
11. **Gates:** `npx vitest run` (324 tests today), `npx svelte-check` (0 errors / 0 warnings), `npm run build` — all from `frontend/`. There is no `npm test` script.

## Design decisions (settled — do not relitigate)

- **Group key:** `action === 'iac_apply'` AND `pr_number` is a positive integer (exact `iacApprovalHref` guard). Everything else — non-iac actions, iac docs with missing/invalid `pr_number` — is a `single`.
- **≥2 docs to form a group.** A PR with one doc renders exactly as today (a `single`).
- **Group position = position of its newest doc** (first occurrence in the newest-first list). Relative order of all other items preserved. Since the list is newest-first and a PR's docs are written over time, a group's docs are in practice contiguous — but the function must not assume contiguity.
- **Group face = `docs[0]`** (the newest doc): its `created_at`, `apply_status`, `head_sha`, `trace_id`, CTA label drive the collapsed row. The face IS the latest known state — faithful, not curated. Subtitle: `docs.find(d => d.pr_title)?.pr_title` (newest-first ⇒ most recent snapshot).
- **CTA semantics unchanged:** `iacApproveLabel(docs[0], resolvedPrs)`. A group containing an `applied` doc ⇒ its PR is in `resolvedPrs` ⇒ view-only label. A group that is all-`waiting` ⇒ face is waiting and not superseded ⇒ live "Review & approve →". No new label logic.
- **Expander shows the *earlier* steps only** (`docs.slice(1)`, displayed **oldest-first** so it reads chronologically), each with: status label (`iacStatusLabel`; when it returns `''` for a missing/empty status, render the deliberate neutral token `status not recorded` — never `step.action`, which would print `iac_apply` where a status belongs), compact time (`fmtCreatedAt`), and an `open trace →` button when the step has a `trace_id`. The face is not repeated inside the expander — "N earlier steps" makes the model explicit: the collapsed card IS the latest step.
- **The collapsed summary must not hide lifecycle state (Codex must-fix).** The `<summary>` carries a **status composition**, not a bare count: `2 earlier steps · awaiting re-bake ×2`, `1 earlier step · failed`. Composition = status labels grouped by label with `×k` for k≥2, ordered by first appearance oldest-first, joined with `, `. Built by a pure helper (`lifecycleSummaryLabel`, Task 1) that returns the COMPLETE string — rendered as one `{expression}`, so there are no markup seams and the exact-string test has zero whitespace risk.
- **Anomalous histories default to OPEN (Codex must-fix).** If any earlier step's `apply_status` is outside the calm set `{applied, waiting_for_rebake}` — that covers `failed`, `failed_state_suspect`, `ambiguous`, and any unknown/missing value (fail-open to visible) — the `<details>` renders with `open`. An operator must not need a click to discover a prior failure. Pure helper `hasAnomalousStep` (Task 1). The operator can still collapse it; we only set the initial state.
- **jsdom reality for `<details>` (adapting a Codex must-fix):** jsdom does not reliably fire the native summary-click → `open` toggle (see the comment block in `frontend/tests/unit/CapabilityCard.test.ts:9-12` — the repo's established workaround is setting `el.open` + dispatching `toggle` manually). The native toggle is browser behavior, not ours; what IS ours — and what the component tests pin — is the **initial open-state**: `details.open === false` for a calm group, `details.open === true` for an anomalous one, with the step nodes structurally INSIDE the `<details>` element so the native expander gates their visibility.
- **Active highlight:** a group row is `.active` when **any** of its docs has `trace_id === activeTraceId` (an operator opening an earlier step's trace should still see which card it belongs to).
- **`{#each}` keys:** `'g:' + pr` for groups, `'s:' + decision_id` for singles — stable, collision-free (decision ids are UUID-hex hyphen strings; the prefixes keep the namespaces disjoint anyway).
- **Markup reuse via a Svelte 5 `{#snippet}`:** the existing ~70-line card body becomes a snippet parameterised by `(d: Decision, subtitle: string | undefined)`; singles call it with `d.pr_title`, the group face calls it with the fallback subtitle and appends the expander. No duplicated markup to drift.
- **No new ARIA invention:** native `<details>/<summary>` (the established pattern — InfraDiagram, CapabilityCard).

## DTO/visual contract for the group row

```text
┌──────────────────────────────────────────────────────────┐
│ PR #68 →                                    Jun 5, 10:27 │   ← face = newest doc
│ infra(checkout): storefront + orders-worker Cloud Run    │
│ iac_apply · applied · ⎇ 0496b30                          │
│ open trace →   Open approval page →                      │
│ ▸ 2 earlier steps · awaiting re-bake ×2                  │   ← <details> (closed: calm history)
│     awaiting re-bake   Jun 4, 23:53   open trace →       │   (expanded, oldest→newest;
│     awaiting re-bake   Jun 4, 23:53   open trace →       │    flex gap, no text separators)
└──────────────────────────────────────────────────────────┘
```

PR-32 shape (`applied` face, prior `failed`): summary reads `1 earlier step · failed` and the `<details>` renders **open** — the failure is visible without a click.

Step rows are three sibling elements (status span, `<time>`, trace button) laid out with CSS flex `gap` — **no text-node separators**, so there are no whitespace seams to glue and tests query elements, not concatenated text.

---

### Task 1: Pure grouping lib — `groupRailDecisions`

**Files:**
- Create: `frontend/src/lib/rail.ts`
- Test: `frontend/tests/unit/rail.test.ts`

**Step 1: Write the failing tests**

`frontend/tests/unit/rail.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { groupRailDecisions } from '../../src/lib/rail';
import type { Decision } from '../../src/lib/types';

// Fixture shapes mirror live /decisions data (2026-06-10): newest-first,
// create-class lifecycles = applied + waiting_for_rebake×2 per PR, and the
// PR-32 failed→applied retry. The paired waiting docs share one trace_id
// (written seconds apart in the same request) — faithful, not a test shortcut.

function iac(id: string, pr: number | undefined, status: string, over: Partial<Decision> = {}): Decision {
  return { decision_id: id, action: 'iac_apply', pr_number: pr, apply_status: status, ...over } as Decision;
}
const other = (id: string): Decision => ({ decision_id: id, action: 'drift_issue' } as Decision);

describe('groupRailDecisions', () => {
  it('folds same-PR iac_apply docs into one group anchored at the newest doc, newest-first inside', () => {
    const a = iac('a', 68, 'applied');
    const w1 = iac('w1', 68, 'waiting_for_rebake');
    const w2 = iac('w2', 68, 'waiting_for_rebake');
    const items = groupRailDecisions([a, w1, w2]);
    expect(items).toEqual([{ kind: 'group', pr: 68, docs: [a, w1, w2] }]);
  });

  it('preserves overall newest-first order: groups sit where their newest doc sat, singles in place', () => {
    const o1 = other('o1');
    const a68 = iac('a68', 68, 'applied');
    const w68 = iac('w68', 68, 'waiting_for_rebake');
    const a32 = iac('a32', 32, 'applied');
    const f32 = iac('f32', 32, 'failed');
    const items = groupRailDecisions([a68, o1, w68, a32, f32]);
    expect(items).toEqual([
      { kind: 'group', pr: 68, docs: [a68, w68] },
      { kind: 'single', d: o1 },
      { kind: 'group', pr: 32, docs: [a32, f32] },
    ]);
  });

  it('keeps a lone iac_apply doc as a single (no 1-doc groups)', () => {
    const w = iac('w', 71, 'waiting_for_rebake');
    expect(groupRailDecisions([w])).toEqual([{ kind: 'single', d: w }]);
  });

  it('never groups iac docs with a missing/invalid pr_number (mirrors the iacApprovalHref guard)', () => {
    const bad1 = iac('b1', undefined, 'applied');
    const bad2 = iac('b2', undefined, 'applied');
    const zero1 = iac('z1', 0, 'applied');
    const zero2 = iac('z2', 0, 'applied');
    const frac1 = iac('f1', 1.5, 'applied');
    const frac2 = iac('f2', 1.5, 'applied');
    const items = groupRailDecisions([bad1, bad2, zero1, zero2, frac1, frac2]);
    expect(items.every((i) => i.kind === 'single')).toBe(true);
    expect(items).toHaveLength(6);
  });

  it('never groups non-iac actions even when they carry a pr_number (docs_pr etc.)', () => {
    const d1 = { decision_id: 'd1', action: 'docs_pr', pr_number: 9 } as Decision;
    const d2 = { decision_id: 'd2', action: 'docs_pr', pr_number: 9 } as Decision;
    expect(groupRailDecisions([d1, d2])).toEqual([
      { kind: 'single', d: d1 },
      { kind: 'single', d: d2 },
    ]);
  });

  it('tolerates null/undefined list and entries (matches resolvedIacPrNumbers style)', () => {
    expect(groupRailDecisions(null)).toEqual([]);
    expect(groupRailDecisions(undefined)).toEqual([]);
    const a = iac('a', 68, 'applied');
    const w = iac('w', 68, 'waiting_for_rebake');
    // null entries are dropped, not crashed on and not rendered.
    expect(groupRailDecisions([a, null, w] as unknown as Decision[])).toEqual([
      { kind: 'group', pr: 68, docs: [a, w] },
    ]);
  });

  it('does not assume contiguity: same-PR docs separated by other rows still fold into one group', () => {
    const a = iac('a', 47, 'applied');
    const o = other('o');
    const w = iac('w', 47, 'waiting_for_rebake');
    expect(groupRailDecisions([a, o, w])).toEqual([
      { kind: 'group', pr: 47, docs: [a, w] },
      { kind: 'single', d: o },
    ]);
  });
});

describe('lifecycleSummaryLabel', () => {
  // `earlier` is docs.slice(1) in list (newest-first) order — the helper owns
  // the oldest-first presentation ordering.
  it('renders count + status composition for the live create-class shape', () => {
    const earlier = [iac('w1', 68, 'waiting_for_rebake'), iac('w2', 68, 'waiting_for_rebake')];
    expect(lifecycleSummaryLabel(earlier)).toBe('2 earlier steps · awaiting re-bake ×2');
  });

  it('singular wording + bare label for one step (PR-32 failed→applied shape)', () => {
    expect(lifecycleSummaryLabel([iac('f', 32, 'failed')])).toBe('1 earlier step · failed');
  });

  it('multi-status composition is ordered by first appearance oldest-first', () => {
    // newest-first input: waiting (newer), failed (oldest) → oldest-first = failed first.
    const earlier = [iac('w', 9, 'waiting_for_rebake'), iac('f', 9, 'failed')];
    expect(lifecycleSummaryLabel(earlier)).toBe('2 earlier steps · failed, awaiting re-bake');
  });

  it('a missing/empty status renders the neutral token, never the action string', () => {
    expect(lifecycleSummaryLabel([iac('x', 9, '')])).toBe('1 earlier step · status not recorded');
  });
});

describe('hasAnomalousStep', () => {
  it('calm: applied / waiting_for_rebake steps are not anomalous', () => {
    expect(hasAnomalousStep([iac('w', 68, 'waiting_for_rebake'), iac('a', 68, 'applied')])).toBe(false);
  });

  it('failed / failed_state_suspect / ambiguous are anomalous', () => {
    expect(hasAnomalousStep([iac('f', 32, 'failed')])).toBe(true);
    expect(hasAnomalousStep([iac('f', 32, 'failed_state_suspect')])).toBe(true);
    expect(hasAnomalousStep([iac('f', 32, 'ambiguous')])).toBe(true);
  });

  it('fails OPEN to visible: unknown and missing statuses count as anomalous', () => {
    expect(hasAnomalousStep([iac('u', 9, 'something_new')])).toBe(true);
    expect(hasAnomalousStep([iac('m', 9, '')])).toBe(true);
    expect(hasAnomalousStep([{ decision_id: 'n', action: 'iac_apply', pr_number: 9 } as Decision])).toBe(true);
  });

  it('empty list is calm', () => {
    expect(hasAnomalousStep([])).toBe(false);
  });
});
```

(`lifecycleSummaryLabel` and `hasAnomalousStep` join the `groupRailDecisions` import.)

**Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/unit/rail.test.ts`
Expected: FAIL — `Cannot find module '../../src/lib/rail'`.

**Step 3: Implement**

`frontend/src/lib/rail.ts`:

```ts
// View-model grouping for the past-decisions rail. An iac_apply lifecycle
// writes one decision doc per apply attempt (live data: applied +
// waiting_for_rebake×2 for create-class PRs; applied + failed for a retry), so
// the rail shows 2-3 near-identical rows per PR. This folds them into ONE
// render item per PR — the newest doc is the face (latest known state), the
// earlier docs become the expandable lifecycle. Pure data → data; the
// component decides presentation.

import type { Decision } from './types';

export type RailItem =
  | { kind: 'single'; d: Decision }
  | {
      kind: 'group';
      pr: number;
      /** ≥2 docs, in list (newest-first) order; docs[0] is the face. */
      docs: Decision[];
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
 * were — e.g. `2 earlier steps · awaiting re-bake ×2`, `1 earlier step ·
 * failed`. `earlier` arrives in list (newest-first) order; composition labels
 * are ordered by first appearance oldest-first and deduped with `×k` counts.
 * Returned as ONE string so the component renders it as a single expression —
 * no markup seams, no whitespace-collapse risk.
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
```

(`iacStatusLabel` is imported from `./format` — the label vocabulary stays single-source.)

**Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run tests/unit/rail.test.ts`
Expected: 15/15 PASS. Then the full gates: `npx vitest run` (no regressions), `npx svelte-check`.

**Step 5: Commit**

```bash
git add frontend/src/lib/rail.ts frontend/tests/unit/rail.test.ts
git commit -m "feat(ui): pure rail grouping — fold same-PR iac_apply docs into one item"
```

---

### Task 2: DecisionsRail renders groups with a lifecycle expander

**Files:**
- Modify: `frontend/src/components/DecisionsRail.svelte`
- Modify: `frontend/tests/unit/DecisionsRail.test.ts` (rewrite case 1; add group cases)

**Step 1: Write/adjust the failing tests**

In `frontend/tests/unit/DecisionsRail.test.ts` — keep the existing `iacRow` helper and `afterEach(cleanup)`; **rewrite case 1** and **append a new describe block**:

```ts
// Case 1 (rewritten): the 3 same-PR docs now collapse into ONE row whose CTA
// already reflects supersession (label-only downgrade, href intact).
it('collapses a superseded lifecycle into one row with the view-only CTA', () => {
  const decisions: Decision[] = [
    iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68 }),
    iacRow({ decision_id: 'wait-68-a', apply_status: 'waiting_for_rebake', pr_number: 68 }),
    iacRow({ decision_id: 'wait-68-b', apply_status: 'waiting_for_rebake', pr_number: 68 }),
  ];
  const { getAllByTestId, getByTestId } = render(DecisionsRail, {
    props: { decisions, activeTraceId: null, onOpenTrace: noop },
  });
  // ONE rail row, ONE approval CTA for the whole lifecycle.
  expect(getAllByTestId('past-decision-item')).toHaveLength(1);
  const link = getByTestId('iac-approve-link');
  expect(link.textContent?.trim()).toBe('Open approval page →');
  expect(link.getAttribute('href')).toBe('/iac-approvals/68');
});

describe('DecisionsRail — collapsed iac_apply lifecycle groups', () => {
  it('face shows the newest doc; expander (closed for a calm history) lists earlier steps oldest-first with status + per-step open trace', async () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'applied-68', apply_status: 'applied', pr_number: 68,
               created_at: '2026-06-05T01:27:33Z', head_sha: '0496b305dead',
               trace_id: 'trace-applied', pr_title: 'infra(checkout): storefront + orders-worker' }),
      // Live-faithful: the two waiting docs share ONE trace_id.
      iacRow({ decision_id: 'wait-68-a', apply_status: 'waiting_for_rebake', pr_number: 68,
               created_at: '2026-06-04T14:53:36Z', trace_id: 'trace-waiting',
               pr_title: 'infra(checkout): storefront + orders-worker' }),
      iacRow({ decision_id: 'wait-68-b', apply_status: 'waiting_for_rebake', pr_number: 68,
               created_at: '2026-06-04T14:53:29Z', trace_id: 'trace-waiting',
               pr_title: 'infra(checkout): storefront + orders-worker' }),
    ];
    const opened: string[] = [];
    const { container, getByTestId, getAllByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: (t: string) => opened.push(t) },
    });

    // Face = newest doc: applied status on the meta line, PR link title.
    const meta = container.querySelector('.row-meta')?.textContent;
    expect(meta).toContain('applied');
    expect(meta).not.toContain('awaiting re-bake');

    // The summary carries the status COMPOSITION (exact single-expression
    // string — lifecycleSummaryLabel), never a bare count that hides state.
    const summary = getByTestId('iac-lifecycle-summary');
    expect(summary.textContent?.trim()).toBe('2 earlier steps · awaiting re-bake ×2');

    // Calm history (waiting steps only) ⇒ the expander defaults to CLOSED, and
    // the step nodes sit structurally INSIDE it so the native expander gates
    // their visibility. (jsdom can't pin the native summary-click toggle —
    // see CapabilityCard.test.ts:9-12 — the initial open-state is what's ours.)
    const details = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(details.open).toBe(false);

    // Earlier steps render oldest-first: wait-68-b (14:53:29) before wait-68-a
    // (14:53:36). Pin the actual order via the datetime attributes.
    const steps = getAllByTestId('iac-lifecycle-step');
    expect(steps).toHaveLength(2);
    expect(steps.every((s) => details.contains(s))).toBe(true);
    expect(steps.map((s) => s.querySelector('time')?.getAttribute('datetime'))).toEqual([
      '2026-06-04T14:53:29Z',
      '2026-06-04T14:53:36Z',
    ]);
    for (const s of steps) expect(s.textContent).toContain('awaiting re-bake');

    // Per-step open-trace works — both steps share the live-faithful trace id.
    const btns = getAllByTestId('lifecycle-open-trace');
    expect(btns).toHaveLength(2);
    await fireEvent.click(btns[0]);
    expect(opened).toEqual(['trace-waiting']);
  });

  it('an all-waiting group (no applied sibling) keeps the live "Review & approve →" CTA on ONE row', () => {
    // The highest-risk CTA case: collapsing must NOT eat the actionable label.
    const decisions: Decision[] = [
      iacRow({ decision_id: 'wait-90-a', apply_status: 'waiting_for_rebake', pr_number: 90 }),
      iacRow({ decision_id: 'wait-90-b', apply_status: 'waiting_for_rebake', pr_number: 90 }),
    ];
    const { getAllByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getAllByTestId('past-decision-item')).toHaveLength(1);
    const link = getByTestId('iac-approve-link');
    expect(link.textContent?.trim()).toBe('Review & approve →');
    expect(link.getAttribute('href')).toBe('/iac-approvals/90');
  });

  it('an anomalous history (prior failed step) is visible WITHOUT a click: composition in the summary + details defaults OPEN', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a32', apply_status: 'applied', pr_number: 32, trace_id: 't-a' }),
      iacRow({ decision_id: 'f32', apply_status: 'failed', pr_number: 32, trace_id: 't-f' }),
    ];
    const { container, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('iac-lifecycle-summary').textContent?.trim()).toBe('1 earlier step · failed');
    const details = container.querySelector('details.lifecycle') as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(getByTestId('iac-lifecycle-step').textContent).toContain('failed');
  });

  it('marks the group row active when an EARLIER step trace is the active trace', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68, trace_id: 't-new' }),
      iacRow({ decision_id: 'w', apply_status: 'waiting_for_rebake', pr_number: 68, trace_id: 't-old' }),
    ];
    const { getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: 't-old', onOpenTrace: noop },
    });
    expect(getByTestId('past-decision-item').classList.contains('active')).toBe(true);
  });

  it('falls back to a sibling pr_title when the newest doc lacks one (fail-soft write)', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'a', apply_status: 'applied', pr_number: 68, pr_title: undefined }),
      iacRow({ decision_id: 'w', apply_status: 'waiting_for_rebake', pr_number: 68,
               pr_title: 'infra(checkout): storefront + orders-worker' }),
    ];
    const { container } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(container.querySelector('.row-subtitle')?.textContent)
      .toBe('infra(checkout): storefront + orders-worker');
  });

  it('a lone iac_apply doc renders exactly as before — single row, no expander', () => {
    const decisions: Decision[] = [
      iacRow({ decision_id: 'w71', apply_status: 'waiting_for_rebake', pr_number: 71 }),
    ];
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('iac-approve-link').textContent?.trim()).toBe('Review & approve →');
    expect(queryByTestId('iac-lifecycle-summary')).toBeNull();
  });
});
```

(`fireEvent` joins the existing `@testing-library/svelte` import.)

**Step 2: Run tests to verify the new ones fail**

Run: `cd frontend && npx vitest run tests/unit/DecisionsRail.test.ts`
Expected: rewritten case 1 + new block FAIL (still 3 rows, no lifecycle testids); untouched cases 2–3 still PASS.

**Step 3: Implement in `DecisionsRail.svelte`**

Sketch (the implementer adapts the existing markup; key requirements below):

- Imports: add `groupRailDecisions`, `lifecycleSummaryLabel`, `hasAnomalousStep` (and `type RailItem` if useful) from `../lib/rail`.
- `const railItems = $derived(groupRailDecisions(decisions));`
- Wrap the existing `<li>` card body in a snippet so singles and group faces share one copy:

```svelte
{#snippet decisionCard(d: Decision, subtitle: string | undefined, isActive: boolean, lifecycle: Decision[] | null)}
  <li class="decision-row" data-testid="past-decision-item" class:active={isActive}>
    <!-- existing .row-summary / .row-subtitle (from `subtitle` param, not d.pr_title)
         / .row-meta / .row-actions markup, UNCHANGED in content -->
    {#if lifecycle && lifecycle.length > 0}
      <details class="lifecycle" open={hasAnomalousStep(lifecycle)}>
        <!-- ONE expression — lifecycleSummaryLabel returns the complete string,
             so this seam has no whitespace to collapse and the exact-string
             test is safe by construction. -->
        <summary data-testid="iac-lifecycle-summary">{lifecycleSummaryLabel(lifecycle)}</summary>
        <ol class="lifecycle-steps">
          {#each [...lifecycle].reverse() as step (step.decision_id)}
            {@const stepStatus = iacStatusLabel(step.apply_status)}
            <li class="lifecycle-step" data-testid="iac-lifecycle-step">
              <!-- Three sibling ELEMENTS spaced by flex gap — no text-node
                   separators, hence no seam-gluing needed (grounding fact 10
                   applies only where text nodes meet). -->
              <span class="step-status">{stepStatus || 'status not recorded'}</span>
              {#if step.created_at}<time class="row-time" datetime={step.created_at}>{fmtCreatedAt(step.created_at)}</time>{/if}
              {#if step.trace_id}
                <button class="open-trace-btn" data-testid="lifecycle-open-trace" type="button"
                  onclick={() => onOpenTrace(step.trace_id as string)}>open trace →</button>
              {/if}
            </li>
          {/each}
        </ol>
      </details>
    {/if}
  </li>
{/snippet}

{#each railItems as item (item.kind === 'group' ? 'g:' + item.pr : 's:' + item.d.decision_id)}
  {#if item.kind === 'single'}
    {@render decisionCard(item.d, item.d.pr_title, !!(item.d.trace_id && item.d.trace_id === activeTraceId), null)}
  {:else}
    {@render decisionCard(
      item.docs[0],
      item.docs.find((x) => x.pr_title)?.pr_title,
      item.docs.some((x) => x.trace_id && x.trace_id === activeTraceId),
      item.docs.slice(1),
    )}
  {/if}
{/each}
```

- `[...lifecycle].reverse()` — never mutate the prop array in place.
- The expander steps are display-only plus `open trace`; no per-step approval link (the group CTA covers the PR).
- `open={hasAnomalousStep(lifecycle)}` sets only the INITIAL state — the operator can still collapse/expand freely.
- CSS (scoped, ds-tokens): `.lifecycle` separated by a hairline (`border-top: 1px solid var(--ds-border)`), `summary` styled like a muted small-caps/`--ds-fs-1` affordance with `cursor: pointer`, `.lifecycle-step` as `list-style: none` rows of `--ds-fs-1` muted text with `display: flex; gap: var(--ds-sp-2)` (the flex gap IS the separator — see the markup comment). Match the file's existing comment density and tone.
- **Do not** change `resolvedPrs`, `approveHref`, `iacApproveHref`, `githubHref`, or any lib function in `approval.ts`/`format.ts`.

**Step 4: Run gates**

Run: `cd frontend && npx vitest run && npx svelte-check && npm run build`
Expected: all green (≈330+ tests), 0 errors / 0 warnings, build clean.

**Step 5: Commit**

```bash
git add frontend/src/components/DecisionsRail.svelte frontend/tests/unit/DecisionsRail.test.ts
git commit -m "feat(ui): collapse iac_apply lifecycle docs into one rail row per PR"
```

---

## Plan-review record (Codex thread 019eb113-ecff-7c13-a517-63b2d40cf779)

First round: **NO-GO**, 6 findings — all folded before execution:

1. Bare-count summary hid lifecycle state (a prior `failed` invisible without a click) → summary now carries the status composition via `lifecycleSummaryLabel`, AND anomalous histories default the `<details>` to open (`hasAnomalousStep`, fail-open on unknown/missing statuses).
2. The highest-risk CTA case (all-waiting group must keep `Review & approve →`) was untested → dedicated component test added.
3. The "oldest-first" test asserted nothing about order → now pins the `time[datetime]` sequence.
4. "Click summary, assert open" — adapted to jsdom reality (the repo-documented limitation, CapabilityCard.test.ts:9-12): the component tests pin the **initial** open-state (closed for calm, open for anomalous) plus structural containment of steps inside the `<details>`; the click-to-toggle itself is native browser behavior.
5. Sketch rendered `stepStatus || step.action` (could print `iac_apply` where a status belongs), contradicting the design note → neutral `status not recorded` token, used consistently in the step row and the summary helper.
6. Whitespace-seam risk in the lifecycle markup → summary is ONE expression (helper returns the complete string); step rows are sibling elements spaced by flex `gap` with no text-node separators.

## Out of scope (deliberate)

- **No backend change** — grouping is a view concern; the decision docs stay faithful 1-doc-per-attempt.
- **No expansion-state persistence** — `<details>` resets on re-render; the rail re-fetches after applies and a reset-to-collapsed is acceptable (matches InfraDiagram/CapabilityCard behaviour).
- **No grouping for non-iac actions** — rollback/drift/docs decisions are one-doc-per-decision today.
- **Legacy `/ui/transparency-legacy` untouched.**
