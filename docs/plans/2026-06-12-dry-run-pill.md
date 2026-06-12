# Dry-Run Pill Implementation Plan (ClickOps item 15 — LAST roadmap item)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A "dry run — not created on GitHub" status token on past-decisions rail rows whose GitHub side effect was skipped because the coordinator runs `DRY_RUN=true`, so an operator previewing DriftScribe in dry-run mode can tell a preview from a real action at a glance.

**Architecture:** Frontend-only. One pure predicate + one template branch in `DecisionsRail.svelte`, reusing the item-11 `rail-status` token pattern and the existing `GITHUB_LINK_LABEL` action allowlist. No backend, type, serve, or worker change. Coordinator rebake required only because the SPA is baked into the coordinator image.

**Tech Stack:** Svelte 5, @testing-library/svelte, vitest.

**Roadmap provenance:** Item 15 of `docs/plans/2026-06-10-clickops-audience-roadmap.md` — "Carried forward as previously scoped" from Phase 2 of `docs/plans/2026-06-08-decision-artifact-links.md`. This plan carries the scope forward but **corrects the original predicate**, which predates the item-11 autonomy work and contains an honesty trap (decision 1).

---

## Grounding facts (verified 2026-06-12)

1. `Settings.dry_run: bool = True` (`agent/config.py:20`). **Prod runs `DRY_RUN=false`** — on live data the pill never renders. The value is for demo/local/dry-run deployments, exactly as the original Phase-2 scoping said ("low value — prod isn't DRY_RUN"). That is why this is the last item.
2. Drift-class decisions (`drift_issue` / `escalation` / `docs_pr` / `no_op`) persist BOTH a top-level `"dry_run": s.dry_run` and a `github` sidecar (`agent/main.py:1492-1494`). Under dry-run the sidecar is `{"dry_run": true, "url": null, ...}` — **no GitHub API call was made** (`driftscribe_lib/github.py:100-103`, `agent/main.py:781-828`).
3. **Rollback decisions are NOT previews under dry-run.** `agent/main.py:1059-1067` (comment + fields): even with `DRY_RUN=true`, propose + notify both run, a REAL approval doc is minted, and the row carries `dry_run_effective: false`. The Approve link on such a row drives a real worker `/execute`. A "dry run" pill there would tell the operator "nothing happened / nothing will" while a live approval sits on the same row.
4. **Observe-suppressed drift rows carry NO `github.dry_run` key** — the sidecar is `{"suppressed_by_autonomy": "observe", "url": null, "action": ...}` (`agent/main.py:1451-1456`). So a `github.dry_run === true` predicate is false on every suppressed row by construction; the item-11 "not executed — Observe mode" token renders instead, alone.
5. `no_op` is never suppressed and its sidecar mirrors the setting (`{"dry_run": s.dry_run, "url": null, "action": "no_op"}`, `agent/main.py:786`) — but there was no action to skip, so a pill there would claim preview-ness of nothing.
6. `iac_apply` decisions cannot be created under dry-run at all — the approval POST refuses 503 before any work (`agent/main.py:3254`), and the approval page suppresses the form ("infra apply disabled (coordinator in dry-run mode)", `agent/main.py:2853`). No rail treatment needed.
7. The rail already has the exact token pattern to reuse: `suppressed_by_autonomy` renders `<span class="rail-status rail-status--muted" data-testid="autonomy-suppressed">` (`frontend/src/components/DecisionsRail.svelte:135-138`), styled at `:485-501`.
8. `DecisionGithub.dry_run?: boolean` is **already typed** (`frontend/src/lib/types.ts:21`). The original plan's "add `dry_run?: boolean` to `Decision`" is now unnecessary — the corrected predicate never reads the top-level field (decision 1), and the rail types only what it renders.
9. The rail action-gates every `github.*` read via `Object.hasOwn(GITHUB_LINK_LABEL, d.action)` (own-key-only, prototype-bypass-proof — `DecisionsRail.svelte:58-72`). The pill reuses the SAME map, so the gated action set cannot drift between the link and the pill.
10. Component tests live in `frontend/tests/unit/DecisionsRail.test.ts` (@testing-library/svelte, established by PR #81). Current baselines: **2847 pytest / 535 vitest**.

## Design decisions

1. **Predicate = `Object.hasOwn(GITHUB_LINK_LABEL, d.action) && d.github?.dry_run === true` — NOT the originally scoped `d.dry_run === true || d.github?.dry_run === true`.** The pill's claim to a ClickOps operator is "this was a preview; nothing external happened." That claim is true exactly where a GitHub action was rendered but skipped (fact 2). It is FALSE on a rollback row under dry-run (fact 3: real approval minted, real `/execute` behind the Approve link) — the original predicate's `d.dry_run === true` arm would render it there. Same honesty class as item-14's "nothing is applied until you approve" overclaim and the ranking filter's "change or import" precision: never let a comfort token overclaim safety.
2. **Action-gate with the existing `GITHUB_LINK_LABEL` map** (fact 9). Excludes `no_op` (fact 5) and any future action that doesn't perform GitHub side effects; keeps the pill's action set and the link's action set identical by construction.
3. **Reuse the item-11 `rail-status rail-status--muted` token, not the 2026-06-08 plan's `ds-pill ds-pill--warn`.** The design system moved on; dry-run working as designed is calm/muted (like the Observe token), not a warning. No new CSS.
4. **No co-occurrence handling needed** — suppressed rows can't satisfy the predicate (fact 4); pinned by a test anyway, since it's load-bearing for "one token per row" calm.
5. **Wording: `dry run — not created on GitHub`.** Says what did NOT happen, names the surface, no instruction. (The row's "View issue/PR →" link is absent on these rows — `url` is null — so the pill also explains the otherwise-bare row.) `text-transform: uppercase` comes from the token class, same as the Observe token.
6. **Frontend-only ⇒ coordinator-image-only rebake** (SPA is baked in; same as items 14). No tofu-editor / tofu-apply / infra-reader rebake (no denylist or inventory change). Live verify on prod is the NEGATIVE: prod rows (`dry_run: false`) must show zero pills, and the bundle must carry the new string.

## Out of scope

- `DecisionSummary` / hero card / trace view — the rail is the scoped surface ("as previously scoped").
- A global "coordinator is in dry-run mode" banner — different feature; the capability card + approval page already state apply-mode facts.
- Rendering `dry_run_effective` anywhere — it exists to disambiguate the rollback persist shape, not for operators.
- Typing `Decision.dry_run` / `Decision.dry_run_effective` — top-level fields are never read (fact 8).
- Backfill or backend changes of any kind.

---

### Task 1: Failing component tests — the pill and its four negative spaces

**Files:**
- Modify: `frontend/tests/unit/DecisionsRail.test.ts` (append a new describe block)

**Step 1: Write the failing tests**

```ts
describe('DecisionsRail — dry-run preview pill', () => {
  /** A drift-class decision row (github sidecar shaped like agent/main.py). */
  function driftRow(over: Partial<Decision>): Decision {
    return {
      decision_id: `d-${Math.random().toString(36).slice(2)}`,
      action: 'drift_issue',
      ...over,
    } as Decision;
  }

  // Parameterized over the full GITHUB_LINK_LABEL action set (Codex plan-review
  // nit): pins the pill's action gate to exactly the actions that perform
  // GitHub side effects, incl. the upgrade_pr forward-compat entry.
  it.each(['drift_issue', 'escalation', 'docs_pr', 'upgrade_pr'])(
    'renders the pill on a %s row whose GitHub action was dry-run-skipped',
    (action) => {
      const decisions = [
        driftRow({ action, dry_run: true, github: { url: null, dry_run: true } }),
      ];
      const { getByTestId, queryByTestId } = render(DecisionsRail, {
        props: { decisions, activeTraceId: null, onOpenTrace: noop },
      });
      expect(getByTestId('decision-dry-run').textContent?.trim()).toBe(
        'dry run — not created on GitHub',
      );
      // url is null on a dry-run row, so no GitHub link renders beside the pill.
      expect(queryByTestId('decision-github-link')).toBeNull();
    },
  );

  it('no pill when the GitHub action really ran (github.dry_run false)', () => {
    const decisions = [
      driftRow({
        dry_run: false,
        github: { url: 'https://github.com/adi-prasetyo/driftscribe/issues/99', dry_run: false },
      }),
    ];
    const { queryByTestId, getByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
    expect(getByTestId('decision-github-link')).toBeTruthy();
  });

  it('no pill on a rollback row with top-level dry_run:true — a REAL approval was minted (agent/main.py dry_run_effective)', () => {
    const decisions: Decision[] = [
      {
        decision_id: 'rb-1',
        action: 'rollback',
        dry_run: true,
        dry_run_effective: false,
        approval: { approval_url: `${location.origin}/approvals/abc?t=x` },
      } as Decision,
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });

  it('no pill on a no_op row even though its sidecar mirrors the setting (nothing was skipped)', () => {
    const decisions = [
      driftRow({ action: 'no_op', dry_run: true, github: { url: null, dry_run: true } }),
    ];
    const { queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });

  it('Observe-suppressed row shows the autonomy token, never the dry-run pill (sidecar has no dry_run key)', () => {
    const decisions = [
      driftRow({
        dry_run: true,
        suppressed_by_autonomy: true,
        autonomy_mode: 'observe',
        github: { url: null }, // agent/main.py:1451-1456 — no dry_run key when suppressed
      }),
    ];
    const { getByTestId, queryByTestId } = render(DecisionsRail, {
      props: { decisions, activeTraceId: null, onOpenTrace: noop },
    });
    expect(getByTestId('autonomy-suppressed')).toBeTruthy();
    expect(queryByTestId('decision-dry-run')).toBeNull();
  });
});
```

**Step 2: Run to verify the first test fails (pill not implemented), the four negatives pass vacuously**

Run: `cd frontend && npx vitest run tests/unit/DecisionsRail.test.ts`
Expected: 1 FAIL (`decision-dry-run` not found), rest of file green.

**Step 3: Commit**

```bash
git add frontend/tests/unit/DecisionsRail.test.ts
git commit -m "test(ui): dry-run pill — preview rows only, never rollback/no_op/suppressed (red)"
```

### Task 2: Implement the pill

**Files:**
- Modify: `frontend/src/components/DecisionsRail.svelte`

**Step 1: Add the predicate next to `githubHref` (after line 77's `githubLabel`)**

```ts
  // Dry-run preview pill: ONLY on rows whose GitHub side effect was skipped
  // because the coordinator runs DRY_RUN=true (github.dry_run === true on a
  // GitHub-action row). Deliberately NOT keyed on the decision's top-level
  // `dry_run`: on rollback rows dry_run=true does NOT suppress the worker
  // calls — a real approval is minted (agent/main.py, dry_run_effective) —
  // so a "dry run" token there would falsely say nothing happened. The
  // GITHUB_LINK_LABEL gate also excludes no_op (its sidecar mirrors the
  // setting but nothing was skipped); Observe-suppressed sidecars carry no
  // dry_run key at all, so the autonomy token renders instead, alone.
  function dryRunPill(d: Decision): boolean {
    return Object.hasOwn(GITHUB_LINK_LABEL, d.action) && d.github?.dry_run === true;
  }
```

**Step 2: Render it directly after the `suppressed_by_autonomy` block (after line 138)**

```svelte
    {#if dryRunPill(d)}
      <span class="rail-status rail-status--muted" data-testid="decision-dry-run"
        >dry run — not created on GitHub</span>
    {/if}
```

No CSS changes — `.rail-status` / `.rail-status--muted` already exist (item 11).

**Step 3: Run the component tests**

Run: `cd frontend && npx vitest run tests/unit/DecisionsRail.test.ts`
Expected: ALL PASS.

**Step 4: Commit**

```bash
git add frontend/src/components/DecisionsRail.svelte
git commit -m "feat(ui): dry-run pill on preview decisions in the rail (ClickOps item 15)"
```

### Task 3: Full gates

**Step 1:** `cd frontend && npm run test:unit` — expected 543 vitest (535 + 8: 4 parameterized positives + 4 negatives).
**Step 2:** `cd frontend && npm run check` — expected 0 errors / 0 warnings.
**Step 3:** `cd frontend && npm run build` — expected clean; grep the bundle for the pill string: `grep -rl "not created on GitHub" dist/assets/`.
**Step 4:** `.venv/bin/ruff check --no-cache . && .venv/bin/pytest -q` from repo root — expected clean / 2847 passed (no backend change; run as regression proof for the PR record).
**Step 5:** Commit anything `npm run check`/format surfaced (expected: nothing).

### Ship steps (after Codex completed-work SHIP)

1. PR off branch `feat/dry-run-pill`, CI watch, squash-merge.
2. Coordinator rebake (`infra/cloudbuild.coordinator-update.yaml`, `_TAG=<short-sha>`), find revision by image digest, `update-traffic --to-revisions=<new>=100` (traffic-pinning gotcha).
3. Live verify (negative space, prod is dry_run=false): `/decisions` rows show zero `decision-dry-run` testids; served bundle contains the pill string; rail otherwise unchanged.
4. Memory + closing report; roadmap COMPLETE.
