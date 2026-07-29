# Composite UI Redesign Implementation Plan (Finalist Pitch) — rev 2, post-Codex

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rebuild the SPA's front door as the composite direction — instrument band + approval desk (three states, incl. a designed resting state), 判子 stamp, ledger strip, estate view, and trace beats — answering the judges' 「UI/UXが非常に見づらく、フロントエンドが後付けのようである」 before the self-imposed ~8/3 UI freeze (see the deadline chain below — 8/3 is our own backstop, not an organizer date).

**Architecture:** Chat stops being the landing view. A new `?view=` client-side view state (following the existing `deeplink.ts` pattern — no router library) switches between `desk`, `estate`, and `chat`. A new **App-level overview store** becomes the single owner of `/infra/graph` + `/infra/pending-approvals` + `/decisions` refresh (mount + focus + poll) so the desk works without `InfraDiagram` mounted. The desk ships **behind an explicit `?view=desk` first**; the bare-URL default flips to desk only after visual verification (freeze-safe). All data comes from existing GETs already in `DEMO_ALLOWLIST`; the one backend change is enriching the existing `/decisions` serialization (same route — no allowlist change). The visual world is the mockup's paper/navy (deck palette `#0e1b5f`/`#4285f4`), vermilion reserved for the seal.

**Tech Stack:** Svelte 5 (runes) + Vite 6, vitest + @testing-library/svelte, Playwright (smoke + local visual rig), FastAPI coordinator serving `agent/static/`.

**Design reference:** the interactive mockup — https://claude.ai/code/artifact/a37ac46f-b770-4485-a7a6-bbbf0395530a (copy at `docs/plans/2026-07-28-composite-mockup.html`). Match its states, copy, and proportions; its CSS is the spec.

---

## Context an implementer must know (verified 2026-07-28; Codex-reviewed)

- **Judge feedback (verbatim, the only negative):** 「UI/UXが非常に見づらく、フロントエンドが後付けのようであるという懸念もあり」. The redesign is the pitch's answer; a before/after beat goes in the deck.
- **Deadline chain:** UI frozen **~8/3 (self-imposed)** → video re-shot 8/4–8/6 → deck due **8/10 10:00 (organizer)** → pitch 8/19 (organizer). **Only 8/10 10:00 and 8/19 come from the organizer.** 8/3 is our own backstop, derived by working backwards from the re-shoot window: the deck's embedded demo video cannot be paused mid-playback, so whatever the video shows is what judges see — the UI has to stop moving before recording starts. The real gate is the video, not the date. Anything not landed before recording does not exist for the pitch; if the re-shoot slips, 8/3 slips with it.
- **`main` ≠ prod.** Prod (rev `00179-mrt`) runs the i18n branch; **PR #245 is a DRAFT, unmerged**, and `main` has since moved ahead (`5cbf890`) — the branch needs a refresh merge before landing. There is no `frontend/src/lib/i18n.ts` on `main` today. Locale catalogs on the branch are **namespace files** (`frontend/src/locales/header.ts`, `infra.ts`, `timeline.ts`, …) — NOT `en.ts`/`ja.ts`; new strings go in a new `desk.ts` namespace following that pattern.
- **Deploy prerequisite (from merged-but-undeployed PR #244):** any coordinator deploy from `main` requires the **infra-reader worker first** (L2 v3→v4), then the coordinator build, then `update-traffic` (traffic is pinned).
- **Data ownership today (the trap rev 1 of this plan fell into):** `/infra/graph` and `/infra/pending-approvals` are fetched *inside* `InfraDiagram.svelte` (~lines 108, 246), not by App. Decisions load on mount and after chat turns only — **no poll, no focus refetch** (`App.svelte:359`, `:926`). A desk view that unmounts InfraDiagram therefore has NO graph/pending data, and an approval completed on the separate approval page never surfaces without a refetch. Phase 3.0 fixes this with an overview store.
- **Decision/approval field reality:**
  - Rollback decisions expose `approval: { approval_id, approval_url, expires_at }` — **no status field today** (`types.ts:8`, `agent/main.py:1333`). Server-side approval states are `pending → used | denied` (`agent/approvals.py:3`). Task 3.0b enriches the serializer.
  - IaC rows carry `apply_status` (+ `applied_at` as the application timestamp) — **not** `status`, and `created_at` can be refreshed by merge reconciliation, so never use it as "when applied" (`types.ts:61`).
  - The pending-approvals DTO has **no `created_at`** (`infra_graph.ts:316`); the backend returns IaC PRs newest-first but without timestamps (`agent/main.py:2903`).
  - `safeApprovalHref(rawUrl)` already exists in `approval.ts:32` (takes a raw URL, not a Decision); `isExpired` is in `approval.ts:79`. Superseded-target *resolution* is component-local in `DecisionsRail.svelte:99` — `iacApprovalHref()` alone does not do it.
- **App shell:** `frontend/src/App.svelte` — single component, no router. Query params: `?reasoning=`, `?conversation=` (deeplink.ts), plus `?ask_pr=` (composer prefill seed, `workloads.ts:23`, stripped on mount at `App.svelte:931`) and `?preview_pr=` (InfraDiagram ghost overlay, `App.svelte:96`). **All four are chat-view intents** — the view resolver must honor them.
- **Reused endpoints all already in the demo worker's `DEMO_ALLOWLIST`** (`infra/cloudflare/worker/src/proxy.js:46`). Enriching `/decisions` output changes no route. If any task adds a NEW endpoint, it must join the allowlist + `proxy.test.js` (#208 lesson).
- **Tour coupling:** `[data-tour="estate"]` (App.svelte:1048) anchors TWO steps — "Your estate" *and* "Adopt your first resource" (`tour.ts:71`); `[data-tour="composer"]` at App.svelte:1062. Phase 4 retargets both estate-anchored steps at the estate view.
- **Focus styling:** global `:where(...):focus-visible` rule at `base.css:89` (deliberately zero specificity). New components inherit it or override locally — both accepted.
- **Smoke tests are pinned to the EN locale**; new smoke/visual specs must set locale explicitly. `styles.test.ts` reads the tokens file into a variable named **`tokens`** — extend that file in its own idiom.
- **Vertex omitted-summaries is a real prod case** (PR #241): traces can have FEW OR ZERO `llm_thought` events. Any beats design must survive that (Phase 5 spec handles it; it is also first on the cut list).

**Phase priority if time runs short:** 0 → 1 → 2 → 3 are non-negotiable. Cut order (Codex-agreed): ① trace beats; ② custom EstateView (fallback: render existing InfraDiagram full-width under the band as the estate view); ③ ledger supersession niceties (keep a short honest strip); ④ if the serializer enrichment slips, cut rollback *stamping* (never fake it) — iac `apply_status` stamping still works.

---

## Phase 0 — Land the baseline (PR #245)

### Task 0.1: Refresh + merge the i18n draft PR

**Step 1:** Refresh the branch over current main and confirm green:
```bash
cd /home/adi/driftscribe && git fetch origin
git checkout feat/i18n-japanese-localization && git merge origin/main   # main moved to 5cbf890 after b9fe5b5
cd frontend && npm ci && npm run build && npm run check && npm run test:unit && cd ..
git push origin feat/i18n-japanese-localization
gh pr checks 245
```

**Step 2:** Un-draft + squash-merge:
```bash
gh pr ready 245 && gh pr merge 245 --squash --delete-branch
git checkout main && git pull
ls frontend/src/lib/i18n.ts frontend/src/locales/
```

**Step 3 (moved up from Phase 6 on Codex's advice): prod-vs-main source check NOW.** Confirm rev `00179-mrt`'s Cloud Build source commit is an ancestor of post-merge main (nothing hot-patched onto prod that main lacks):
```bash
gcloud run revisions describe driftscribe-agent-00179-mrt --region=asia-northeast1 \
  --project=driftscribe-hack-2026 --format='value(metadata.labels)' | tr ',' '\n' | grep -i commit
git merge-base --is-ancestor <that_commit> main && echo OK
```
If NOT an ancestor: stop, diff, reconcile before any further work. **Do not deploy anything yet.**

---

## Phase 1 — Tokens (PR "redesign-tokens")

### Task 1.1: Composite palette + Mincho token

**Files:** Modify `frontend/src/styles/tokens.css`; extend `frontend/tests/unit/styles.test.ts`.

**Step 1 (failing test):** in `styles.test.ts`'s existing idiom (source variable is `tokens`):
```ts
it('defines the composite redesign tokens', () => {
  for (const t of ['--ds-paper:', '--ds-navy:', '--ds-drift-amber:', '--ds-seal:', '--ds-font-mincho:'])
    expect(tokens).toContain(t);
});
it('reserves vermilion for the seal only', () => {
  expect(tokens.match(/#c0392b/gi)?.length).toBe(1);
});
```
**Step 2:** run `npx vitest run tests/unit/styles.test.ts` → FAIL.
**Step 3:** add to `tokens.css` (values from the mockup's `.mX` world):
```css
/* Composite redesign world (2026-07-28 mockup) */
--ds-paper:#fbfaf8; --ds-paper-ink:#12151c; --ds-paper-ink-2:#535c6b;
--ds-paper-mut:#8a9099; --ds-paper-rule:#e6e3dd;
--ds-navy:#0e1b5f; --ds-gblue:#4285f4;
--ds-drift-amber:#9a5b12; --ds-ok-green:#1a6b52;
--ds-seal:#c0392b; /* 判子 ONLY */
--ds-font-mincho:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",Georgia,serif;
```
**Step 4:** tests PASS (run the full unit suite — other pins may count tokens). **Step 5:** commit.

*(Self-hosting Noto woff2s stays OPTIONAL, only after Phase 5 — the video is filmed on the operator machine where Yu Mincho renders.)*

---

## Phase 2 — View state + intents (PR "view-routing")

### Task 2.1: `viewFromSearch` honoring ALL chat intents

**Files:** Modify `frontend/src/lib/deeplink.ts`; extend `frontend/tests/unit/deeplink.test.ts`.

**Step 1 (failing tests):**
```ts
describe('viewFromSearch', () => {
  it('defaults to chat until the flip, then desk (see Task 3.6)', () => { /* start: default 'chat' */ });
  it('accepts the allowlist', () => {
    expect(viewFromSearch('?view=desk')).toBe('desk');
    expect(viewFromSearch('?view=estate')).toBe('estate');
    expect(viewFromSearch('?view=chat')).toBe('chat');
  });
  it('rejects unknown values → default', () => { expect(viewFromSearch('?view=admin')).toBe(DEFAULT_VIEW); });
  it('every chat intent forces chat, beating an explicit view param', () => {
    for (const q of ['?reasoning=' + 'a'.repeat(32), '?conversation=abc123', '?ask_pr=42', '?preview_pr=42'])
      expect(viewFromSearch('?view=desk&' + q.slice(1))).toBe('chat');
  });
});
```
**Step 3 (implement):**
```ts
export type AppView = 'desk' | 'estate' | 'chat';
const VIEWS: readonly AppView[] = ['desk', 'estate', 'chat'];
export let DEFAULT_VIEW: AppView = 'chat';   // flipped to 'desk' in Task 3.6 — one-line change

/** Chat-view intents: any of these params means the URL's purpose lives in chat. */
function hasChatIntent(params: URLSearchParams, search: string): boolean {
  return Boolean(
    reasoningTraceFromSearch(search) || conversationIdFromSearch(search) ||
    params.get('ask_pr') || params.get('preview_pr'),
  );
}
export function viewFromSearch(search: string): AppView {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  if (hasChatIntent(params, search)) return 'chat';
  const raw = params.get('view');
  return (VIEWS as readonly string[]).includes(raw ?? '') ? (raw as AppView) : DEFAULT_VIEW;
}
```
**Steps 4–5:** PASS; commit.

### Task 2.2: Wire views into App + header nav + the leave-chat invariant

**Files:** Modify `frontend/src/App.svelte`; `frontend/src/lib/tour.ts`; new locale namespace `frontend/src/locales/desk.ts` (registered like `header.ts`); extend `frontend/tests/unit/App.test.ts`.

**Navigation invariant (Codex finding #2 — test it):** switching AWAY from chat clears every chat-intent param (`reasoning`, `conversation`, `ask_pr`, `preview_pr`) in the same `replaceState` write that sets `view`. A copied desk URL must reload as the desk. Switching TO chat restores nothing.

**Step 1 (failing tests):** render with `?view=estate` → estate testid present, composer absent; navigate desk→ while a replay is open → `location.search` contains neither `reasoning` nor `view=chat` contradiction.

**Step 3 (implement):**
- `let view = $state(viewFromSearch(location.search))`; `navigate(v: AppView)` sets state + one URL write implementing the invariant.
- Header nav: `デスク / 推定図 / チャット` buttons via `$t()`, `aria-current` on active. Desk/estate branches render placeholder sections (`data-testid="approval-desk"` / `"estate-view"`) — filled by Phases 3–4. Chat branch = today's exact layout, untouched.
- `openTrace()` / conversation-open call `navigate('chat')` first.
- Tour: composer step unchanged; both estate-anchored steps keep working for now because chat (default) still mounts InfraDiagram — full retarget happens in Phase 4 when the estate view exists.

**Steps 4–6:** unit + `npm run check` + smoke locally; commit.

---

## Phase 3 — Data contract + desk (PR "approval-desk") — THE CORE

### Task 3.0a: `overviewStore` — single owner of graph/pending/decisions refresh

**Files:** Create `frontend/src/lib/overviewStore.ts`; test `frontend/tests/unit/overviewStore.test.ts`.

Factory-pattern store like `autonomyStore.ts`/`pauseStore.ts` (`createOverviewStore(call)`, instantiated once in App):
- State: `{ graph, pendingApprovals, decisions, lastError }` — typed with existing `infra_graph.ts` / `types.ts` types.
- `refresh(reason)` fetches the three GETs in parallel (each soft-fails independently).
- Refresh triggers: store creation; `visibilitychange`→visible and window `focus` (this is what makes the stamped state appear after returning from an approval page — Codex blocker fix); a poll via `RefreshScheduler` (`lib/infra_refresh.ts`) with its existing ladder; and an explicit bump App calls after chat turns (replacing today's post-turn decisions reload so there is exactly ONE decisions owner).
- Single-flight + monotonic-seq stale guard (port the pattern from `autonomyStore.ts` — do not invent a new one).

Tests: focus event triggers refetch; overlapping refreshes collapse; a failed graph fetch keeps prior graph and sets `lastError`.

InfraDiagram is NOT rewired this phase (it keeps its internal fetching for the chat view; the store serves desk/estate). One redundant fetch pair exists only for users who visit both views in one session — acceptable; noted for post-pitch cleanup.

**Commit.**

### Task 3.0b: Enrich `/decisions` rollback rows with approval status (backend, same route)

**Files:** Modify `agent/main.py` (~line 1333, decision serialization); modify `frontend/src/lib/types.ts:8`; Python test beside the existing decisions-route tests.

**Step 1 (failing Python test):** a decision whose approval doc is `used` serializes `approval.status == "used"` and `approval.resolved_at == <used timestamp>`; a `pending` one serializes `status == "pending"`, `resolved_at == None`.

**Step 3:** join the approval doc's `status` (`pending|used|denied`, `agent/approvals.py:3`) + its resolution timestamp into the existing `approval` sub-object. Read-only enrichment of an existing allowlisted route — no worker change. TS type gains `status?: 'pending'|'used'|'denied'; resolved_at?: string`.

**Steps 4–5:** `pytest` target file PASS; commit. **If this task slips: cut rollback stamping (cut-list item ④), not the desk.**

### Task 3.1: `deskModel` selection logic

**Files:** Create `frontend/src/lib/desk.ts`; test `frontend/tests/unit/desk.test.ts`.

Selection rules (Codex finding #3 fix — explicit priority, no fictional timestamps):
1. **Pending rollbacks first:** decisions with `approval?.approval_url` that pass `safeApprovalHref()` (`approval.ts:32` — pass the RAW url), `approval.status === 'pending'` (absent status ⇒ treat as pending for pre-enrichment compatibility), not `isExpired(approval.expires_at)` (`approval.ts:79`). Newest by decision `created_at` (fine here — creation ordering, not application time).
2. **Else first item of the pending-approvals payload** (backend already returns newest-first; DTO has no timestamp — do not pretend otherwise). Href via the numeric `/iac-approvals/<n>` builder. No cross-kind recency claim anywhere in copy.
3. **Stamped:** nothing pending AND (newest iac row with `apply_status === 'applied'` and `applied_at` within 10 min) OR (rollback whose `approval.status === 'used'` with `resolved_at` within 10 min). Expose `stampedUntil` so the component can schedule decay.
4. Else **resting**.

Fixtures use the REAL field names (`apply_status`, `applied_at`, `approval.status: 'pending'|'used'`). Tests: each rule, expired approval ignored, absent-status compatibility, decay boundary.

**Commit.**

### Task 3.2: `SealStamp.svelte`
As rev 1: port the mockup's `.seal` CSS (3px `var(--ds-seal)` circle, rotate(-11deg), `stampIn` overshoot keyframe, `prefers-reduced-motion` off-switch), `size: 'lg'|'sm'`, `animate` prop (desk only). Test: renders 承認, `role="img"`, size class. **Commit.**

### Task 3.3: `InstrumentBand.svelte`
Props `{ managed, drift, awaiting, onNavigate }` — numbers computed in App from the overview store via existing `scopeTotals()`/actionable-drift helpers in `lib/infra_graph.ts` (import, never re-derive). 44px `tabular-nums` numerals (navy/amber/gblue), meter `flex: managed/drift`, stats are `<button>`s → `onNavigate('estate')`. Renders on desk AND estate (compact prop = stretch goal). Test: numbers render; `onNavigate` fires. **Commit.**

### Task 3.4: `LedgerStrip.svelte` + `ledgerRows()`
`ledgerRows(decisions, max=4)` pure function in `frontend/src/lib/ledger.ts`: applied rows (`apply_status==='applied'` or `approval.status==='used'`) → ✓ + mini stamp; open proposals → ◍; else ⬤; newest first; times via `format.ts` (JST HH:mm).

> **Deviation, deliberate (ds-mml):** `format.ts` follows the BROWSER locale, not a pinned JST. It matches the existing `DecisionsRail` convention and is right for an operator reading their own wall clock; on the JST machine where the video is shot the rendered output is identical to the line above. A judge browsing from another timezone sees their own local time, which is the better default for them too. Recorded here because the plan text says JST literally and the implementation does not.
 Supersession treatment = plain text via existing helpers where trivially available; anything clever is cut-list item ③. The 定期点検 quiet-scan row stays **deferred** (scan runs aren't persisted — do not fake). Tests: mapping, ordering, cap. **Commit.**

### Task 3.5: `ApprovalDesk.svelte` + wire the desk view
**Files:** Create `frontend/src/components/ApprovalDesk.svelte`; modify App desk branch; locale namespace `desk.ts` (JA copy verbatim from the mockup); test `ApprovalDesk.test.ts`.

Per-state tests (as rev 1) with two Codex corrections baked in:
- resting watch line uses `graph.generated_at` (`infra_graph.ts:129`), falls back to 「走査時刻 取得中」 when null — calm must never look dead;
- stamped state schedules its own decay: `setTimeout` to `stampedUntil` so it falls back to resting **without needing another data event** (clear the timer on unmount; jsdom fake-timer test).

Layout mirrors the mockup (deskwrap max-width 780px, min-height against state jumps, Mincho h3 31px). Diff via existing `DriftDiffCard` (restyle, don't fork). CTA = `<a>` to `deskModel` href (approve primary, reject ghost). `<LedgerStrip>` below in every state. Data exclusively from the overview store — no fetches in this component. **Commit.**

### Task 3.6: Visual verification, THEN flip the default
1. `frontend/tests/visual/desk.visual.ts` (pattern: `composer-lock.visual.ts`): route-mock the three GETs per state (resting / pending / stamped), screenshot 1280×800, EN + JA. Local run; attach to PR.
2. Only after eyeballing against the mockup: flip `DEFAULT_VIEW` to `'desk'` (one line from Task 2.1) + update the deeplink test + `transparency.smoke.ts` (bare URL → desk; composer flows via `?view=chat`). **Separate commit — this is the moment the redesign goes live-by-default, keep it revertable.**

---

## Phase 4 — Estate view (PR "estate-view")

### Task 4.1: `EstateView.svelte`
As rev 1 (list rows over `resourceCards()`/`splitCards()` from `lib/infra_graph.ts`, hollow amber rings, `<details>` system fold, adopt chip → `navigate('chat')` + existing prefill bridge, Mermaid never imported) — but fed by the **overview store**, and with the tour fix: retarget BOTH estate-anchored steps (`tour.ts:71` — "Your estate" AND "Adopt your first resource") at the estate view; the adoption step spotlights the first adoptable row, falling back to the nav button when none. Tests: row ordering, adopt callback, PR-open chip, fold, tour target present. **Commit + estate visual screenshot.**

**Fallback if time is short:** estate view = `InstrumentBand` + existing `InfraDiagram` rendered full-width. Ugly-but-honest beats absent.

---

## Phase 5 — Trace beats (PR "trace-beats") — FIRST ON THE CUT LIST

### Task 5.1: `beatsOf()` — spec tightened per Codex finding #5

**Files:** Modify `frontend/src/lib/timeline.ts`; extend `frontend/tests/unit/timeline.test.ts`.

Contract (precise, survives zero-thought traces):
- Input: chronological `TraceEvent[]`. Fields are the REAL ones: `thought_text` (thoughts), `response_preview` (final) — see `sse.ts:12`.
- Candidate beats = all `llm_thought` events + the `final_response`. `llm_usage` is invisible (never a beat, never counted in skips, never numbered).
- **Cap: 5 beats** — always keep first thought + final; sample middle thoughts evenly when over cap.
- Skip markers between beats count **logical tool calls** (reuse the pairing notion from `pairToolEvents`, `timeline.ts:147` — a call+result pair is ONE step), `mcp_call` counts as one.
- Step numbers = 1-based index over (thoughts + logical tool calls + mcp + final), consistent with the skip arithmetic. Worked example in the test: thought, call+result, thought, call+result, mcp, thought, usage, final → beats at steps 1, 3, 6, 8 wait — recompute in the test fixture and assert the actual arithmetic; the TEST is the contract.
- **Zero or one thought (Vertex omitted-summaries case, PR #241):** return `[]`; the component then renders the existing full accordions and the omitted-summaries note — beats silently absent, never fabricated.

TDD: write the fixture, hand-compute expected steps in a comment, then implement. **Commit.**

### Task 5.2: `TraceBeats.svelte` + Timeline default mode
As rev 1: `Timeline` gains `mode: 'beats'|'full'`, default `beats` **only when `beatsOf` returned ≥2 beats**, else full; 「全 N ステップを表示」 toggle; accordions and `reconcileBackfill`'s never-overwrite invariant untouched; omitted-note renders in both modes. Tests + beats visual screenshot. **Commit.**

---

## Phase 6 — Verify + ship

### Task 6.1: Full-suite pass
```bash
cd frontend && npm run build && npm run check && npm run test:unit && npm run test:smoke
cd .. && python -m pytest tests/ -k decisions -q   # serializer enrichment
```
Confirm no new endpoints: `git diff <pre-redesign-main> --stat -- infra/cloudflare/` is empty and the only `agent/` change is the Task 3.0b serializer.

### Task 6.2: Live verification (driftscribe-live-probe skill, Path B)
Desk resting renders with real `generated_at`; band matches `/infra/graph`; approve a staged rollback on the approval page, return, watch the focus-refetch produce the stamp; estate adopt chip round-trips to chat prefill; `?reasoning=` lands on chat + beats (or full, if the trace sheds summaries). Browser turns write prod — cleanup rules per the skill; never touch the KEEPER conversation.

### Task 6.3: Deploy (driftscribe-deploy skill)
Order: **infra-reader worker (v4) → coordinator build → `update-traffic` → rollback worker LAST**. The rollback worker goes after the coordinator, not before: its `claim_pending` writes `apply_audit` into the approval doc the moment a judge clicks Approve, and an older coordinator reading that doc raises `TypeError` → 500 on the approval page for the rest of the window (ds-wjw; full reasoning table in `2026-07-28-rollback-outcome-honesty.md` §Deploy order). The read-side field projection landed with that fix, so a wrong order now degrades to nothing — follow it anyway. Autonomy rule: CI-green + Codex-SHIP → merge and redeploy without asking.

### Task 6.4: Freeze + record
`git tag ui-freeze-2026-08-03` (the date is the self-imposed backstop, not an organizer deadline — retag if the re-shoot moves); capture desk×3 / estate / beats screenshots for the deck's before/after slide. Video re-shoot (8/4–8/6, `creating-demo-videos` skill) is out of scope; the desk's three-state click-through is its storyboard.

---

## Risks & non-goals

- **Risk — rollback stamping depends on Task 3.0b.** If the serializer enrichment slips, ship without rollback stamping (iac stamping still works); never infer approval state client-side.
- **Risk — dual fetchers (overview store + InfraDiagram)** until post-pitch cleanup: harmless (different views), documented here so nobody "fixes" it mid-freeze.
- **Non-goal:** deleting chat-view accordions / InfraDiagram / CapabilityCard — demotion, not deletion, until after 8/19.
- **Non-goal:** in-app approve POSTs — approval stays on the HMAC-gated pages by design.
- **Non-goal:** the 定期点検 persisted-scan row (needs the unbuilt periodic-check backend).
