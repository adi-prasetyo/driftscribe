# Migration Coverage Meter Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Turn the Infrastructure panel's existing `{managed}/{resources} managed` count into a migration *progress treatment* — a percentage, a progress bar, and a headline ("26% of your infrastructure is under IaC management") — so the ClickOps→IaC migration reads as a journey with a number that goes up.

**Architecture:** Frontend-only (roadmap Wave 1 item 2, `docs/plans/2026-06-10-clickops-audience-roadmap.md`). A new pure helper `frontend/src/lib/coverage.ts` computes the display percentage from the `GET /infra/graph` totals the SPA already fetches; a new presentational component `frontend/src/components/CoverageMeter.svelte` renders headline + progress bar + detail line; `InfraDiagram.svelte` embeds the meter at the top of the expanded panel body and appends the percentage to the collapsed-summary count. No backend, no new endpoint, no new data.

**Tech Stack:** Svelte 5 (runes), TypeScript, vitest + @testing-library/svelte (jsdom), scoped component styles on the existing `--ds-*` design tokens.

**Ships via:** coordinator rebake (`agent/static/` is gitignored — the SPA bundle is built inside `Dockerfile.agent` at deploy; nothing but source files is committed).

---

## Context for implementers (read once)

- The SPA's Infrastructure panel is `frontend/src/components/InfraDiagram.svelte` — a `<details>` card. On mount it fetches `GET /infra/graph` (cheap JSON) via the injected `call` prop; Mermaid only loads when the panel is first expanded. **Do not touch the fetch/render concurrency guards or the RefreshScheduler wiring.**
- The graph DTO type is `InfraGraph` in `frontend/src/lib/infra_graph.ts`. The piece we consume: `totals: { resources: number; managed: number; drift: number }`. A `degraded: true` graph means CAI inventory was unavailable — never show a meter for it.
- Component tests use `@testing-library/svelte` v5 on the jsdom environment configured in `vitest.config.ts` — see `frontend/tests/unit/DecisionsRail.test.ts` for the established pattern (`render`, `cleanup` in `afterEach`).
- Important `<details>` fact the InfraDiagram test relies on: content inside a closed `<details>` **is present in the DOM** (just not visible), so tests can assert on the panel body **without opening the panel** — and not opening it means Mermaid is never imported in tests.
- Design tokens live in `frontend/src/styles/tokens.css` (`--ds-ok-surface`, `--ds-ok-border`, `--ds-neutral-surface`, `--ds-border-strong`, `--ds-muted`, `--ds-fs-1/2`, `--ds-sp-*`, `--ds-radius-sm`). Use scoped `<style>` in the new component (the meter is SPA-only; `base.css` is shared with the Jinja approval pages and must NOT grow SPA-only classes).
- Run tests from `frontend/`: `npm run test:unit` (vitest), `npm run check` (svelte-check). Run `npm run build` once at the end to prove the bundle compiles.
- Commit style: conventional commits, e.g. `feat(ui): …`, `test(ui): …`.

### Display-percentage rules (the product contract)

The number is a *trust* number — it must never lie in either direction:

1. `resources <= 0` (or any non-finite input) → **no percentage** (`null`): a 0-resource estate has no meaningful coverage, and `0/0` must not render "0%" or NaN.
2. `managed` is clamped into `[0, resources]` before computing (defensive against a server bug; never show −4% or 104%).
3. Exact endpoints are honest: `managed === resources` → exactly `100`; `managed === 0` → exactly `0`.
4. Otherwise round to the nearest integer, then clamp into `[1, 99]`:
   - `199/200` (99.5%) must show **99**, not 100 — "100%" is reserved for *actually done*.
   - `1/1000` (0.1%) must show **1**, not 0 — the first adopted resource must visibly move the number off zero.

---

## Task 1: Pure percent helper `coverage.ts`

**Files:**
- Create: `frontend/src/lib/coverage.ts`
- Test: `frontend/tests/unit/coverage.test.ts`

**Step 1: Write the failing tests**

```ts
// frontend/tests/unit/coverage.test.ts
import { describe, it, expect } from 'vitest';
import { coveragePercent } from '../../src/lib/coverage';

// Display-percentage contract (plan §Display-percentage rules): the number is
// a trust number — exact 0/100 only when literally true, [1,99] otherwise,
// null when there is nothing to measure.
describe('coveragePercent', () => {
  it('computes a plain rounded percentage', () => {
    expect(coveragePercent(13, 50)).toBe(26);
    expect(coveragePercent(1, 3)).toBe(33);
    expect(coveragePercent(2, 3)).toBe(67);
  });

  it('returns null when there is nothing to measure', () => {
    expect(coveragePercent(0, 0)).toBeNull();
    expect(coveragePercent(5, 0)).toBeNull();
    expect(coveragePercent(5, -1)).toBeNull();
  });

  it('returns null on non-finite input', () => {
    expect(coveragePercent(Number.NaN, 10)).toBeNull();
    expect(coveragePercent(3, Number.NaN)).toBeNull();
    expect(coveragePercent(3, Number.POSITIVE_INFINITY)).toBeNull();
    expect(coveragePercent(Number.POSITIVE_INFINITY, 10)).toBeNull();
    expect(coveragePercent(Number.NEGATIVE_INFINITY, 10)).toBeNull();
  });

  it('is exact at the endpoints', () => {
    expect(coveragePercent(10, 10)).toBe(100);
    expect(coveragePercent(0, 10)).toBe(0);
  });

  it('never rounds up to 100 or down to 0', () => {
    expect(coveragePercent(199, 200)).toBe(99); // 99.5% — not done ⇒ not 100
    expect(coveragePercent(1, 1000)).toBe(1); // 0.1% — first adoption moves the needle
  });

  it('clamps out-of-range managed counts instead of lying', () => {
    expect(coveragePercent(-3, 10)).toBe(0);
    expect(coveragePercent(15, 10)).toBe(100);
  });
});
```

**Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/unit/coverage.test.ts`
Expected: FAIL — cannot resolve `../../src/lib/coverage`.

**Step 3: Write the implementation**

```ts
// frontend/src/lib/coverage.ts
// coverage.ts — display percentage for the migration coverage meter.
//
// The percentage is a TRUST number for the ClickOps→IaC audience (roadmap Wave
// 1 item 2): "100%" must mean literally every resource is managed, and the
// first adopted resource must visibly move the number off zero. Hence the
// exact-endpoint + [1,99] clamp rules rather than naive rounding.

/**
 * Percentage of `resources` covered by `managed`, shaped for display:
 *  - `null` when there is nothing to measure (resources <= 0, non-finite input)
 *  - exactly 100 / 0 only when literally complete / literally zero
 *  - otherwise rounded, then clamped into [1, 99]
 */
export function coveragePercent(managed: number, resources: number): number | null {
  if (!Number.isFinite(managed) || !Number.isFinite(resources)) return null;
  if (resources <= 0) return null;
  const m = Math.min(Math.max(managed, 0), resources);
  if (m === resources) return 100;
  if (m === 0) return 0;
  return Math.min(99, Math.max(1, Math.round((m / resources) * 100)));
}
```

**Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run tests/unit/coverage.test.ts`
Expected: PASS (6 tests).

**Step 5: Commit**

```bash
git add frontend/src/lib/coverage.ts frontend/tests/unit/coverage.test.ts
git commit -m "feat(ui): coveragePercent display helper for the migration coverage meter"
```

---

## Task 2: `CoverageMeter.svelte` presentational component

**Files:**
- Create: `frontend/src/components/CoverageMeter.svelte`
- Test: `frontend/tests/unit/CoverageMeter.test.ts`

**Step 1: Write the failing tests**

```ts
// frontend/tests/unit/CoverageMeter.test.ts
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import CoverageMeter from '../../src/components/CoverageMeter.svelte';

afterEach(cleanup);

const totals = (managed: number, resources: number, drift: number) => ({
  managed,
  resources,
  drift,
});

describe('CoverageMeter', () => {
  it('renders headline, progressbar and detail line', () => {
    const { getByTestId, getByRole } = render(CoverageMeter, {
      props: { totals: totals(13, 50, 37) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('26%');
    expect(getByTestId('coverage-meter').textContent).toContain(
      'of your infrastructure is under IaC management',
    );
    const bar = getByRole('progressbar');
    expect(bar.getAttribute('aria-valuenow')).toBe('26');
    expect(bar.getAttribute('aria-valuemin')).toBe('0');
    expect(bar.getAttribute('aria-valuemax')).toBe('100');
    expect(getByTestId('coverage-detail').textContent).toContain('13 of 50 resources managed');
    expect(getByTestId('coverage-detail').textContent).toContain('37 not yet in IaC');
  });

  it('renders the fill at the percentage width', () => {
    const { getByTestId } = render(CoverageMeter, {
      props: { totals: totals(13, 50, 37) },
    });
    const fill = getByTestId('coverage-fill') as HTMLElement;
    expect(fill.style.width).toBe('26%');
  });

  it('renders nothing when totals is null', () => {
    const { queryByTestId } = render(CoverageMeter, { props: { totals: null } });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('renders nothing for a zero-resource estate', () => {
    const { queryByTestId } = render(CoverageMeter, {
      props: { totals: totals(0, 0, 0) },
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('omits the "not yet in IaC" segment at 100%', () => {
    const { getByTestId } = render(CoverageMeter, {
      props: { totals: totals(7, 7, 0) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('100%');
    expect(getByTestId('coverage-detail').textContent).toContain('7 of 7 resources managed');
    expect(getByTestId('coverage-detail').textContent).not.toContain('not yet in IaC');
  });

  it('shows an honest 0% when nothing is managed yet', () => {
    const { getByTestId, getByRole } = render(CoverageMeter, {
      props: { totals: totals(0, 12, 12) },
    });
    expect(getByTestId('coverage-pct').textContent).toBe('0%');
    expect(getByRole('progressbar').getAttribute('aria-valuenow')).toBe('0');
  });
});
```

**Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/unit/CoverageMeter.test.ts`
Expected: FAIL — cannot resolve the component.

**Step 3: Write the component**

```svelte
<!-- frontend/src/components/CoverageMeter.svelte -->
<script lang="ts">
  // CoverageMeter — the migration-progress treatment of the infra totals
  // (roadmap Wave 1 item 2). Purely presentational: parent passes the
  // /infra/graph `totals`; percentage shaping lives in lib/coverage.ts.
  import { coveragePercent } from '../lib/coverage';

  let {
    totals = null,
  }: {
    totals: { resources: number; managed: number; drift: number } | null;
  } = $props();

  const pct = $derived(totals ? coveragePercent(totals.managed, totals.resources) : null);
</script>

{#if totals && pct !== null}
  <div class="coverage" data-testid="coverage-meter">
    <p class="coverage__headline">
      <strong class="coverage__pct" data-testid="coverage-pct">{pct}%</strong>
      of your infrastructure is under IaC management
    </p>
    <div
      class="coverage__bar"
      role="progressbar"
      aria-label="IaC coverage"
      aria-valuemin="0"
      aria-valuemax="100"
      aria-valuenow={pct}
    >
      <div class="coverage__fill" data-testid="coverage-fill" style:width="{pct}%"></div>
    </div>
    <p class="coverage__detail" data-testid="coverage-detail">
      {totals.managed} of {totals.resources} resources managed{#if totals.drift > 0}
        · {totals.drift} not yet in IaC{/if}
    </p>
  </div>
{/if}

<style>
  .coverage {
    margin: 0 0 var(--ds-sp-4);
  }
  .coverage__headline {
    margin: 0 0 var(--ds-sp-2);
    font-size: var(--ds-fs-2);
    color: var(--ds-muted);
  }
  .coverage__pct {
    color: var(--ds-fg);
    font-variant-numeric: tabular-nums;
  }
  .coverage__bar {
    height: 0.5rem;
    border-radius: var(--ds-radius-sm);
    background: var(--ds-neutral-surface);
    border: 1px solid var(--ds-border-strong);
    overflow: hidden;
  }
  .coverage__fill {
    height: 100%;
    background: var(--ds-ok-surface);
    /* No border on the fill: at 0% a zero-width div would still paint a 1px
       border sliver — the track's border alone frames the bar. */
    transition: width var(--ds-dur-fast) var(--ds-ease);
  }
  .coverage__detail {
    margin: var(--ds-sp-2) 0 0;
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
```

Notes for the implementer:
- The `style:width` directive sets `element.style.width` via the CSSOM — allowed even under a strict CSP (it is not an inline `style="…"` attribute in served HTML).
- All token names used above are verified to exist in `frontend/src/styles/tokens.css` (`--ds-fg` is the ink/text token). Do not invent new tokens.
- A 0% fill renders as a 0-width div inside the track — that is the intended look (empty bar), no special-casing.

**Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run tests/unit/CoverageMeter.test.ts`
Expected: PASS (6 tests).

**Step 5: Commit**

```bash
git add frontend/src/components/CoverageMeter.svelte frontend/tests/unit/CoverageMeter.test.ts
git commit -m "feat(ui): CoverageMeter component — headline, progress bar, detail line"
```

---

## Task 3: Integrate into `InfraDiagram.svelte`

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte`
- Test: `frontend/tests/unit/InfraDiagram.test.ts` (new)

**What changes in `InfraDiagram.svelte` (three small edits, nothing else):**

1. Imports + derived percent — add to the existing `<script>`:

```ts
import { coveragePercent } from '../lib/coverage';
import CoverageMeter from './CoverageMeter.svelte';
```

and after the existing `const renderable = …` line:

```ts
const pct = $derived(totals ? coveragePercent(totals.managed, totals.resources) : null);
```

2. Collapsed-summary count gains the percentage (replace the existing count `<span>` at the current line ~186):

```svelte
<span class="infra-summary__count" data-testid="infra-coverage-count"
  >{totals.managed}/{totals.resources} managed{pct === null ? '' : ` · ${pct}%`}</span
>
```

3. The meter sits at the top of the expanded body — insert immediately AFTER the closing `</div>` of `.infra-toolbar` (before the `{#if error}` block):

```svelte
{#if graph && !degraded}
  <CoverageMeter totals={graph.totals} />
{/if}
```

Do NOT touch: `refresh()`, `renderDiagram()`, the scheduler `$effect`s, `onToggle`, or the existing badges (`infra-drift-badge` stays exactly as is).

**Step 1: Write the failing component test**

```ts
// frontend/tests/unit/InfraDiagram.test.ts
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import InfraDiagram from '../../src/components/InfraDiagram.svelte';
import type { InfraGraph } from '../../src/lib/infra_graph';

// Renders InfraDiagram with a stubbed `call` prop (the component's only data
// dependency) and asserts the coverage treatment end-to-end. The panel is
// NEVER opened, so Mermaid is never imported: <details> content is in the DOM
// even while closed, which lets us assert on the body without paying for the
// diagram.

afterEach(cleanup);

function graphWith(totals: InfraGraph['totals'], degraded = false): InfraGraph {
  return {
    generated_at: null,
    project: 'demo',
    caveat: 'test caveat',
    degraded,
    degraded_reason: degraded ? 'cai_unavailable' : null,
    totals,
    groups: [],
    edges: [],
  };
}

function callWith(graph: InfraGraph, paths: string[] = []): (path: string) => Promise<Response> {
  return async (path: string) => {
    paths.push(path);
    return new Response(JSON.stringify(graph), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
}

describe('InfraDiagram — coverage treatment', () => {
  it('shows the percentage in the collapsed summary count and the meter in the body', async () => {
    const paths: string[] = [];
    const { getByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 50, managed: 13, drift: 37 }), paths) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-coverage-count').textContent).toBe('13/50 managed · 26%');
    });
    expect(paths).toContain('/infra/graph');
    expect(getByTestId('coverage-meter')).toBeTruthy();
    expect(getByTestId('coverage-pct').textContent).toBe('26%');
  });

  it('keeps the plain count (no percentage, no meter) for a zero-resource estate', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 0, managed: 0, drift: 0 })) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-coverage-count').textContent).toBe('0/0 managed');
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
  });

  it('shows no meter when the graph is degraded', async () => {
    const { getByTestId, queryByTestId } = render(InfraDiagram, {
      props: { call: callWith(graphWith({ resources: 0, managed: 0, drift: 0 }, true)) },
    });
    await waitFor(() => {
      expect(getByTestId('infra-drift-badge').textContent).toBe('unavailable');
    });
    expect(queryByTestId('coverage-meter')).toBeNull();
    expect(queryByTestId('infra-coverage-count')).toBeNull();
  });
});
```

(If the global `Response` constructor is unavailable in the jsdom environment, fall back to a minimal stub: `({ ok: true, status: 200, json: async () => graph }) as unknown as Response` — note this in the test with a one-line comment.)

**Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/InfraDiagram.test.ts`
Expected: FAIL — `infra-coverage-count` testid not found / no `coverage-meter`.

**Step 3: Apply the three edits above**

**Step 4: Run the full frontend gates**

Run: `cd frontend && npm run test:unit && npm run check`
Expected: all suites PASS (including the three pre-existing suites that touch InfraDiagram indirectly), 0 svelte-check errors.

**Step 5: Commit**

```bash
git add frontend/src/components/InfraDiagram.svelte frontend/tests/unit/InfraDiagram.test.ts
git commit -m "feat(ui): coverage meter + summary percentage in the Infrastructure panel"
```

---

## Post-review deltas (as shipped)

The two-stage review per task folded the following changes — the code blocks
above are the *original* spec; the shipped code differs in exactly these ways:

- **Task 1 tests:** + midpoint-rounding pin (`coveragePercent(3, 8) → 38` — kills a
  `round→floor` mutation), + `coveragePercent(3, Number.NEGATIVE_INFINITY) → null`
  symmetry case, + a comment on `(15, 10)` attributing the 100 to the
  exact-endpoint guard (7 tests total).
- **Task 2 component:** the detail-line separator is rendered via an explicit
  `{' '}` expression tag — Svelte trims literal leading whitespace at `{#if}`
  boundaries (even inline), so the spec's template rendered `managed· 37`
  glued; an expression tag is never trimmed and is prettier-proof. Also added:
  `aria-valuetext="{pct}% — {managed} of {resources} resources managed"` on the
  progressbar (screen readers otherwise announce a bare number), and a JSDoc
  line on the `totals` prop.
- **Task 2 tests:** detail assertion is a single glued string (pins the
  separator spacing), + `aria-valuetext` exact assertion, + `0%` fill-width pin
  in the honest-0% case.
- **Task 3:** `<CoverageMeter {totals} />` (derived alias, not `graph.totals`)
  for template consistency; the degraded test uses NON-ZERO totals
  (`{resources: 5, managed: 3, drift: 2}`) so suppression provably comes from
  the degraded branch, not an empty estate.

---

## Task 4: Full verification gates

**Step 1:** `cd frontend && npm run test:unit && npm run check && npm run build` — all green, bundle compiles.

**Step 2:** From repo root: `python -m pytest -q` — backend untouched, suite must stay green (sanity only).

**Step 3:** `ruff check .` from repo root (should be a no-op for a frontend-only change).

No commit (nothing should change). If anything fails, fix forward with the implementer before proceeding.

---

## Task 5: PR → CI → Codex completed-work review → merge (controller)

1. Push `feat/coverage-meter`, open PR titled `feat(ui): migration coverage meter in the Infrastructure panel`.
2. Wait for CI green.
3. Codex completed-work review on the plan-review thread (`mcp__codex__codex-reply`); fold any must-fix.
4. Squash-merge (deploy autonomy applies — no ask).

## Task 6: Deploy + live verify (controller)

1. `gcloud builds submit --config=infra/cloudbuild.coordinator-update.yaml --substitutions=_TAG=$(git rev-parse --short HEAD) --project=driftscribe-hack-2026`
2. Traffic is PINNED: `gcloud run services update-traffic driftscribe-agent --to-revisions=<new-rev>=100 --region=asia-northeast1`
3. Live verify on the direct run.app URL (bare domain is behind Cloudflare Access): the SPA loads, the Infrastructure summary shows `7/7 managed · 100%` (current estate is fully managed per [[infra_graph_phase3]] — exact numbers may differ; assert the `· N%` suffix and, in the served JS bundle, the presence of the `coverage-meter` testid string).
4. Update memory (`clickops_audience_initiative.md` + MEMORY.md index).
