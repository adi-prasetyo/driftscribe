# Decision-Artifact Links in the Operator UI — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Surface the GitHub PR/issue that a drift-detection decision produces as a safe, clickable link in the operator UI (closes audit gap #1), with two optional follow-on phases for the dry-run flag (#3) and structured env diffs (#2).

**Architecture:** The coordinator already persists `decision.github.url` (an absolute `https://github.com/...` URL from PyGithub's `html_url`, or `null` on dry-run/no-op) on every `docs_pr` / `drift_issue` / `escalation` decision (`agent/main.py:1394`, `driftscribe_lib/github.py:100-103`). (`upgrade_pr` would too, but the upgrade workload is unimplemented in `/recheck` in this build — `agent/main.py:1139` — so no such decision exists yet; the UI gate lists it for forward-compat only.) The UI never renders any of these links. We add a **pure, host-allowlisted URL validator** (`safeGithubHref`) in `frontend/src/lib/approval.ts` — mirroring the existing `safeApprovalHref` / `iacApprovalHref` security pattern — and wire an action-gated "View PR/issue →" link into `DecisionsRail.svelte`, the always-visible surface that already hosts the rollback "Approve →" and iac "Open approval page →" links. No backend change.

**Tech Stack:** Svelte 5 (runes) + Vite + TypeScript SPA; Vitest unit tests (lib-only, pure functions); Playwright smoke tests with `page.route` JSON mocks.

---

## Background — why the rail, not the decision card

A drift decision (`drift_issue` / `docs_pr` / `escalation`) carries `rationale`, so on "open trace →" replay `App.svelte:306` sets `finalReply = rationale ?? rendered_body` (non-null). That makes `DecisionSummary` **never render** for these decisions (its guard is `finalReply == null`, `App.svelte:368`). Therefore the github link MUST live in `DecisionsRail` to be reachable. The rail is also always-visible (no replay needed) and already the home of every other per-decision action link.

**Security context (carry forward from `lib/decision.ts` header):** GET `/trace` and `/decisions` return the decision doc **unredacted**. We never trust a raw URL field. `safeGithubHref` defends in depth: it rejects any non-`https`, any host other than `github.com`, and anything that doesn't parse — so a poisoned `github.url` cannot smuggle `javascript:` / `data:` / an open-redirect. Unlike `safeApprovalHref` (which returns a *relative* same-origin href), this returns the *absolute* `github.com` URL by design (it is an intentional off-origin external link) and the anchor uses `target="_blank" rel="noopener noreferrer"`.

---

## Phase 1 — Safe GitHub PR/issue link in the rail (CORE)

### Task 1: `safeGithubHref` URL validator (pure lib, TDD)

**Files:**
- Modify: `frontend/src/lib/approval.ts` (append a new export)
- Test: `frontend/tests/unit/approval.test.ts` (append a new `describe`)

**Step 1: Write the failing tests** — add `safeGithubHref` to the **existing** `'../../src/lib/approval'` import at the top of `frontend/tests/unit/approval.test.ts` (don't add a second import line — Codex style nit), then append:

```ts
describe('safeGithubHref — canonical github.com artifact allowlist', () => {
  it('accepts a canonical github.com issue URL (returns absolute, unchanged)', () => {
    const u = 'https://github.com/acme/ops/issues/42';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('accepts a github.com PR URL', () => {
    const u = 'https://github.com/acme/ops/pull/7';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('accepts owner/repo names with dots/dashes', () => {
    const u = 'https://github.com/acme-co/ops.infra/issues/3';
    expect(safeGithubHref(u)).toBe(u);
  });
  it('rejects http (non-TLS)', () => {
    expect(safeGithubHref('http://github.com/acme/ops/issues/42')).toBeNull();
  });
  it('rejects a look-alike / off-allowlist host', () => {
    expect(safeGithubHref('https://github.com.evil.example/acme/ops/issues/42')).toBeNull();
    expect(safeGithubHref('https://raw.githubusercontent.com/x/y/issues/1')).toBeNull();
    expect(safeGithubHref('https://gitlab.com/acme/ops/issues/42')).toBeNull();
  });
  it('rejects userinfo smuggling (user@host, user:pass@host)', () => {
    expect(safeGithubHref('https://evil@github.com/acme/ops/issues/1')).toBeNull();
    expect(safeGithubHref('https://github.com@evil.example/acme/ops/issues/1')).toBeNull();
    expect(safeGithubHref('https://u:p@github.com/acme/ops/issues/1')).toBeNull();
  });
  it('rejects a non-default port', () => {
    expect(safeGithubHref('https://github.com:444/acme/ops/issues/1')).toBeNull();
  });
  it('rejects whitespace / control chars / backslashes in the raw string', () => {
    expect(safeGithubHref('https://github.com/acme/ops/issues/1\t')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops/iss\nues/1')).toBeNull();
    expect(safeGithubHref('https://github.com\\acme/ops/issues/1')).toBeNull();
  });
  it('rejects a non-artifact github.com path (settings, bare repo, root)', () => {
    expect(safeGithubHref('https://github.com/settings/profile')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops')).toBeNull();
    expect(safeGithubHref('https://github.com/')).toBeNull();
    expect(safeGithubHref('https://github.com/acme/ops/issues/notanumber')).toBeNull();
  });
  it('rejects javascript: / data: smuggling', () => {
    expect(safeGithubHref('javascript:alert(1)')).toBeNull();
    expect(safeGithubHref('data:text/html,<script>1</script>')).toBeNull();
  });
  it('rejects null / non-string / empty / unparseable', () => {
    expect(safeGithubHref(null)).toBeNull();
    expect(safeGithubHref(undefined)).toBeNull();
    expect(safeGithubHref(123 as unknown)).toBeNull();
    expect(safeGithubHref('')).toBeNull();
    expect(safeGithubHref('not a url')).toBeNull();
  });
});
```

**Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/approval.test.ts`
Expected: FAIL — `safeGithubHref is not exported` / `is not a function`.

**Step 3: Implement** — append to `frontend/src/lib/approval.ts`:

```ts
// Canonical PyGithub artifact path: /<owner>/<repo>/(issues|pull)/<number>.
// PyGithub's html_url only ever emits this shape, so we pin to it (defence in
// depth — the /trace + /decisions decision docs are UNREDACTED).
const GITHUB_ARTIFACT_PATH = /^\/[^/]+\/[^/]+\/(?:issues|pull)\/\d+$/;

/**
 * External-link guard for a decision's `github.url` (the PR/issue the agent
 * opened). Unlike `safeApprovalHref` (relative, same-origin), this is a
 * DELIBERATE off-origin link, so it returns the ABSOLUTE url — but only after a
 * strict allowlist: https, host EXACTLY `github.com` (no port, no userinfo), and
 * a canonical issue/PR pathname. Rejects every other host, non-TLS schemes,
 * `javascript:` / `data:` smuggling, look-alike hosts (`github.com.evil`,
 * `user@github.com`), and any raw string carrying whitespace / control chars /
 * backslashes (which a real html_url never does). Callers still gate on an
 * allowlisted `action`, and the anchor uses `rel="noopener noreferrer"`.
 */
export function safeGithubHref(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw === '') return null;
  // Reject up front so no URL-parser normalization trick slips a control char,
  // newline, tab, space, or backslash through (the \u0000-\u001f range covers all C0 controls; \s whitespace).
  if (/[\u0000-\u001f\s\\]/.test(raw)) return null;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:') return null;
  if (u.hostname !== 'github.com') return null;
  if (u.port !== '') return null;
  if (u.username !== '' || u.password !== '') return null;
  if (!GITHUB_ARTIFACT_PATH.test(u.pathname)) return null;
  return u.href;
}
```

**Step 4: Run to verify it passes**

Run: `cd frontend && npx vitest run tests/unit/approval.test.ts`
Expected: PASS (all new + existing approval tests green).

**Step 5: Commit**

```bash
git add frontend/src/lib/approval.ts frontend/tests/unit/approval.test.ts
git commit -m "feat(ui): safeGithubHref — host-allowlisted external-link guard for decision github.url"
```

---

### Task 2: Type the `github` field on `Decision`

**Files:**
- Modify: `frontend/src/lib/types.ts`

**Step 1: Add the interface + field** — in `frontend/src/lib/types.ts`, add above `Decision`:

```ts
/** The PR/issue side-channel on a drift/docs/upgrade decision
 *  (GET /decisions → decision.github). `url` is an absolute github.com URL or
 *  null (dry-run / no-op); always routed through `safeGithubHref` before href. */
export interface DecisionGithub {
  url?: string | null;
  dry_run?: boolean;
}
```

and add the field to the `Decision` interface body:

```ts
  github?: DecisionGithub | null;
```

**Step 2: Typecheck**

Run: `cd frontend && npm run check`
Expected: PASS (no new type errors).

**Step 3: Commit**

```bash
git add frontend/src/lib/types.ts
git commit -m "feat(ui): type Decision.github (url + dry_run) for the rail link"
```

---

### Task 3: Wire the link into `DecisionsRail.svelte`

**Files:**
- Modify: `frontend/src/components/DecisionsRail.svelte`

**Step 1: Add the import** — extend the existing approval import (line 2):

```ts
  import { safeApprovalHref, iacApprovalHref, isExpired, safeGithubHref } from '../lib/approval';
```

**Step 2: Add local resolvers** — after `iacApproveLabel` (line 40), mirroring the existing local-helper pattern:

```ts
  // Resolve the GitHub PR/issue link for a drift/docs decision. Gated on an
  // allowlisted `action` (so we never read github.url off an unrelated/iac
  // decision) AND host-allowlisted via safeGithubHref. Returns null otherwise.
  //
  // IMPORTANT: use Object.hasOwn, NOT the `in` operator — `'toString' in obj`
  // (and other prototype keys) is true, so `in` would let an unexpected action
  // string slip the gate (Codex review). Object.hasOwn is own-key-only.
  const GITHUB_LINK_LABEL: Record<string, string> = {
    drift_issue: 'View issue →',
    escalation: 'View issue →',
    docs_pr: 'View PR →',
    // `upgrade_pr` is NOT emitted by /recheck in this build (the upgrade
    // workload is unimplemented — agent/main.py:1139), so no such decision
    // currently persists a github.url. Listed for forward-compat only: it
    // renders nothing today and lights up automatically if a future build
    // starts persisting upgrade_pr decisions with a github.url.
    upgrade_pr: 'View PR →',
  };
  function githubHref(d: Decision): string | null {
    if (!Object.hasOwn(GITHUB_LINK_LABEL, d.action)) return null;
    return safeGithubHref(d.github?.url);
  }
  function githubLabel(d: Decision): string {
    return Object.hasOwn(GITHUB_LINK_LABEL, d.action)
      ? GITHUB_LINK_LABEL[d.action]
      : 'View on GitHub →';
  }
```

**Step 3: Render the link** — inside `.row-actions` (after the `iacApproveHref` block, line 109), add:

```svelte
            {#if githubHref(d)}
              {@const ghHref = githubHref(d)}
              <a
                class="past-approve-btn"
                data-testid="decision-github-link"
                href={ghHref}
                target="_blank"
                rel="noopener noreferrer">{githubLabel(d)}</a>
            {/if}
```

**Step 4: Typecheck + existing unit suite**

Run: `cd frontend && npm run check && npx vitest run`
Expected: PASS (no type errors; all existing unit tests still green).

**Step 5: Commit**

```bash
git add frontend/src/components/DecisionsRail.svelte
git commit -m "feat(ui): clickable PR/issue link on drift/docs/upgrade decisions in the rail"
```

---

### Task 4: Smoke-test the wiring (positive + malicious-rejected)

**Files:**
- Modify: `frontend/tests/smoke/fixtures.ts` (extend `decisionsResponse`)
- Modify: `frontend/tests/smoke/transparency.smoke.ts` (update one count + add a dedicated test)

> **Codex must-fix:** the existing test `'malicious off-origin approval URL renders NO link'` asserts `toHaveCount(3)` at `transparency.smoke.ts:187` ("two rollbacks + one iac_apply"). Adding rows to the shared `decisionsResponse` fixture WILL break it. You MUST update that count and its comment in the same change.

**Step 1: Add fixture rows** — in `decisionsResponse(origin)` (`fixtures.ts:133`), add two decisions to the `decisions` array (a valid github.com issue link + a malicious `javascript:` one, mirroring the existing same-origin/evil approval pair):

```ts
      {
        decision_id: 'd-drift-1',
        trace_id: 'aa11bb22cc33dd44ee55ff6600112233',
        action: 'drift_issue',
        created_at: '2026-06-08T01:00:00+00:00',
        github: { url: 'https://github.com/acme/ops/issues/99', dry_run: false },
      },
      {
        decision_id: 'd-drift-evil',
        trace_id: 'bb11bb22cc33dd44ee55ff6600112233',
        action: 'drift_issue',
        created_at: '2026-06-08T01:01:00+00:00',
        github: { url: 'javascript:alert(document.cookie)', dry_run: false },
      },
```

**Step 2: Fix the now-stale count** — in `transparency.smoke.ts:186-187`, update the existing assertion and comment from 3 to 5:

```ts
    // five seeded decisions render (two rollbacks + one iac_apply + two drift_issue)
    await expect(page.locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)).toHaveCount(5);
```

**Step 3: Add a dedicated github-link test** — append a new `test(...)` in the same `describe` block:

```ts
  test('decision github.url: valid github.com link renders, javascript: url does not', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    // Exactly one safe github link — the valid github.com issue. The
    // javascript: row is rejected by safeGithubHref and renders no anchor.
    const ghLinks = page.getByTestId('decision-github-link');
    await expect(ghLinks).toHaveCount(1);
    await expect(ghLinks.first()).toHaveAttribute('href', 'https://github.com/acme/ops/issues/99');
    await expect(ghLinks.first()).toHaveAttribute('rel', 'noopener noreferrer');
    await expect(ghLinks.first()).toHaveAttribute('target', '_blank');
    // Belt-and-suspenders: no anchor anywhere carries the javascript: payload.
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  });
```

**Step 4: Run the smoke suite**

Run: `cd frontend && npm run build && npm run test:smoke` (run the full suite — the shared-fixture count change above touches a sibling test, so don't `-g`-filter to just the new one)
Expected: PASS — the github-link test is green AND the updated `toHaveCount(5)` rollback test still passes.

> If the smoke harness needs the built bundle copied into `agent/static/`, follow the existing build step the smoke config already performs (check `playwright.smoke.config.ts` `webServer` / build hook before running).

**Step 4: Commit**

```bash
git add frontend/tests/smoke/fixtures.ts frontend/tests/smoke/transparency.smoke.ts
git commit -m "test(ui): smoke-assert decision github link renders (github.com) and rejects javascript:"
```

---

### Task 5: Full verification before PR

**Step 1:** `cd frontend && npm run check` → PASS (typecheck)
**Step 2:** `cd frontend && npx vitest run` → PASS (all unit tests)
**Step 3:** `cd frontend && npm run build` → PASS (production bundle builds)
**Step 4:** `cd frontend && npm run test:smoke` → PASS (full smoke suite)
**Step 5:** Rebuild the bundle into `agent/static/` per the repo's existing deploy step (the Svelte build output is served by the coordinator; confirm the manifest/hashed asset names update). Verify `git status` shows the regenerated `agent/static/*` if that is how this repo ships the SPA.

> **DO NOT** claim Phase 1 complete until Steps 1–4 all show PASS in captured output (superpowers:verification-before-completion).

---

## Phase 2 — Dry-run pill in the rail (OPTIONAL, trivial)

**Value today:** low — prod does not run `DRY_RUN`. Include only if you want the operator to distinguish a real action from a preview at a glance (e.g. before re-enabling dry-run for a demo). Same surface (rail), same pattern.

### Task 6: Render a "dry-run" pill on decisions where `dry_run === true`

**Files:**
- Modify: `frontend/src/components/DecisionsRail.svelte`
- Test: `frontend/tests/smoke/transparency.smoke.ts`

**Step 1:** In `DecisionsRail.svelte`, in `.row-summary` (after the `row-action` span, `DecisionsRail.svelte:75`), add:

```svelte
            {#if d.dry_run === true || d.github?.dry_run === true}
              <span class="ds-pill ds-pill--warn dry-run-pill" data-testid="decision-dry-run">dry-run</span>
            {/if}
```

(Add `dry_run?: boolean` to the `Decision` interface in `types.ts` first.)

**Step 2:** Smoke-assert: a fixture decision with `dry_run: true` shows the `decision-dry-run` pill; a normal one does not.

**Step 3:** Verify (`npm run check && npx vitest run && npm run test:smoke`) → PASS, then commit:

```bash
git commit -am "feat(ui): dry-run pill on preview decisions in the rail"
```

---

## Phase 3 — Structured env-diff sub-card (OPTIONAL, NEEDS A REDACTION DECISION FIRST)

**⚠️ Do NOT implement blind.** `decision.diffs[]` carries `{name, expected, live, contract_status, ...}` where `expected`/`live` are **env-var values**. The `/trace` + `/decisions` decision docs are **unredacted**, and a drifted env var could be a secret value. Today this content is *already* visible via `rendered_body` prose on replay, but rendering it as a structured, always-present table is a deliberate increase in surface and must be a conscious decision.

**Required pre-work (separate brainstorm, not this plan):**
1. Decide the redaction rule for diff *values*. Options: (a) render **name + contract_status only**, never the value; (b) reuse the backend `secret_guard.should_redact(name, value)` logic client-side or have the backend ship a `redacted: bool` per diff; (c) show values only when `contract_status` proves non-secret.
2. Decide where it renders. `DecisionSummary` won't show for drift decisions (they have prose → `finalReply != null`), so this needs either a new always-visible sub-card under the hero on replay (new `App.svelte` plumbing of `historicalDecision.diffs`) or a new dedicated panel.

**Recommendation:** Park Phase 3. Ship Phase 1 (and optionally Phase 2), then run `superpowers:brainstorming` on the diff-redaction question before writing a Phase-3 plan. Option (a) — name + `contract_status` badge, no raw value — is the safe default and likely sufficient (the operator can open the PR/issue via the Phase-1 link to see full detail).

---

## Open questions for review

1. **Host allowlist scope.** `safeGithubHref` pins `hostname === 'github.com'`. This deployment uses github.com (`s.github_repo`). If GH Enterprise (a custom host) is ever in scope, this needs the configured host injected. Acceptable to hardcode `github.com` now? (Recommend: yes, with a comment.)
2. **Link placement.** Phase 1 puts the link only in the rail. Should it *also* appear as a button under the hero (`FinalResponse`) when replaying a decision that has a valid `github.url`? (Recommend: rail-only for v1; the rail is always visible and matches the existing link pattern. Add the hero button later if operators ask.)
3. **`upgrade_pr` inclusion (RESOLVED).** Codex confirmed the upgrade workload is **not** wired into `/recheck` in this build (`agent/main.py:1139`), so no `upgrade_pr` decision currently persists a `github.url`. Keeping it in `GITHUB_LINK_LABEL` is harmless forward-compat (renders nothing today). Decision: keep it, with the explanatory comment in Task 3. If you'd rather not carry dead config, drop the `upgrade_pr` line — Phase 1 is unaffected either way.

---

## Review status

This plan was reviewed by Codex (read-only, thread `019ea498`) before hand-off. All four must-fixes folded in:
- `safeGithubHref` tightened — reject userinfo (`user@github.com`), non-default ports, control chars/whitespace/backslashes; pin a canonical `/<owner>/<repo>/(issues|pull)/<n>` pathname (Task 1).
- Action gate uses `Object.hasOwn`, not `in` (no prototype-key bypass) (Task 3).
- `upgrade_pr` framing corrected — forward-compat only, not currently backed (above + Task 3 comment).
- Smoke `toHaveCount(3)` → `(5)` so the shared-fixture rows don't break the sibling rollback test (Task 4).

A post-implementation Codex review of the completed work against this plan should follow (per the operator's standard loop).

---

## Execution handoff

Phase 1 is 5 bite-sized tasks, no backend change, all TDD-able against pure lib functions + one smoke assertion. Phases 2–3 are independent opt-ins.
