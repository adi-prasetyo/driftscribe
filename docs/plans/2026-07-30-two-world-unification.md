# Two-World Unification (ds-qbo) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** One visual world. The chat view, estate internals, header chrome, and the two server-rendered approval pages adopt the composite paper world the desk already speaks, and the duplicate token vocabulary is retired — zero `--ds-paper*`/`--ds-gblue`/`--ds-ok-green`/`--ds-drift-amber` tokens left when done.

**Architecture:** Re-value the *legacy* design tokens to the paper-world values in `tokens.css` (one commit re-grounds every legacy component AND the Jinja approval pages, which `<link>` the built CSS via `ds_css_href()`), then run small structural passes only where token values can't carry the change (header nav, card elevation, display-face headings). Finish with a zero-visual-delta retirement pass that rewrites the ~80 paper-token references in the five desk components to the canonical names and deletes the duplicate block.

**Tech Stack:** Svelte 5 (runes), Vite, Vitest + @testing-library/svelte (jsdom), Playwright (smoke + hand-run visual), Jinja templates styled by the same built CSS, pytest for template suites.

---

## Why now, and the deadline shape

Judges' only negative: UI/UX 見づらい・後付け. PR #272 (merged, live at coordinator `00189-j2w`) fixed hierarchy *within* screens; the remaining 後付け signal is *between* screens — navigating desk→chat flips the entire visual language, and the money-shot video walks straight through that flip (desk → approval page → back to desk).

- **Deck + embedded video are hard-due 8/10 10:00.** Video re-shoot needs the UI frozen, so **Phases 1–2 must be merged, deployed, and live by ~8/4–8/5.**
- **8/10–8/19 stays open for polish** (user decision 2026-07-30). Phase 3 (retirement) is zero-visual-delta by construction, so it may trail into that window if squeezed — but it is NOT optional; low tech debt is an explicit success criterion.
- 8/8 is lost to an unrelated hackathon. Do not plan work on it.

## The two worlds, precisely (audited 2026-07-30, post-#272 main @ e3ed992)

Paper-world components (already done, become the *reference*, untouched until Phase 3):
`ApprovalDesk.svelte` (27 paper refs), `EstateView.svelte` shell (26), `InstrumentBand.svelte` (11), `LedgerStrip.svelte` (10), `SealStamp.svelte` (5).

Legacy-world surfaces (the work): `base.css` (81 legacy refs — shared by SPA *and* Jinja), `InfraDiagram.svelte` (75), `DecisionsRail.svelte` (40), `AutonomyPill.svelte` (38), `CapabilityCard.svelte` (23), `PauseBanner.svelte` (18), `ChatForm.svelte`/`Timeline.svelte` (17), `App.svelte` shell+header (16), `ConversationsRail.svelte` (16), `ConversationThread.svelte`/`FinalResponse.svelte` (15), `TraceBadge.svelte` (13), `PausePill.svelte` (12), and ~15 smaller components. Plus `agent/templates/approval.html` and `iac_approval.html`: **zero paper tokens today.**

The good news, and the whole strategy: the two grounds are near-identical (`--ds-bg` #fcfcfb vs `--ds-paper` #fbfaf8; `--ds-border` #e7e6e1 vs `--ds-paper-rule` #e6e3dd). What actually differs is ink (warm vs cool-navy), accent hue (link-blue #1f6feb vs Google blue #4285f4 / navy #0e1b5f), accent greens/ambers, elevation habits (shadowed cards vs rules), and the display face (mincho). All but elevation and mincho are **pure token values**.

## Design decisions (settled — do not re-litigate during implementation)

1. **Canonical names are the legacy names.** `--ds-bg/--ds-fg/--ds-border/--ds-ok/--ds-warn/--ds-stream` survive with paper-world *values*; the `--ds-paper*` block dies. Rationale: ~80 paper refs vs several hundred legacy refs — rewrite the smaller side. `--ds-navy`, `--ds-seal`, `--ds-font-mincho` are unique vocabulary (not duplicates) and get **promoted** into the main palette section under their own names. `--ds-gblue` dies into `--ds-stream`.
2. **Token value mapping** (Phase 1 — this is the normative table):

   | Token | Old | New | Note |
   |---|---|---|---|
   | `--ds-bg` | `#fcfcfb` | `#fbfaf8` | = paper |
   | `--ds-surface` | `#ffffff` | *(unchanged)* | raised sheets stay white |
   | `--ds-surface-2` | `#f6f5f1` | *(unchanged)* | wells; already harmonious |
   | `--ds-fg` | `#1a1a18` | `#12151c` | = paper-ink |
   | `--ds-fg-soft` | `#3a3a36` | `#535c6b` | = paper-ink-2 |
   | `--ds-muted` | `#6b6b66` | `#6a7180` | cool grey; deliberately darker than paper-mut — see (3). Codex measured the earlier `#6f7581` at 4.437:1, under the floor; the implementer MUST verify this value ≥4.5:1 on `#fbfaf8` and darken one step if not |
   | `--ds-faint` | `#9a9a93` | `#8a9099` | = paper-mut |
   | `--ds-border` | `#e7e6e1` | `#e6e3dd` | = paper-rule |
   | `--ds-border-strong` | `#d8d7d1` | `#d6d2ca` | derived: one step heavier than rule |
   | `--ds-ok` | `#1f8a4c` | `#1a6b52` | = ok-green |
   | `--ds-ok-ink` | `#176b3b` | `#14523f` | derived darker |
   | `--ds-ok-surface/-border` | — | `#e9f3ef` / `#bfdcd1` | re-derived tints of the new hue |
   | `--ds-warn` | `#9a6b00` | `#9a5b12` | = drift-amber |
   | `--ds-warn-ink` | `#7d5700` | `#7c490e` | derived darker |
   | `--ds-warn-surface/-border` | — | `#f8f0e3` / `#e8d3b4` | re-derived tints |
   | `--ds-danger*` | — | *(unchanged)* | keeps distance from seal vermilion `#c0392b` — the "vermilion for the seal only" pin must keep passing |
   | `--ds-stream` | `#1f6feb` | `#4285f4` | = gblue, the deck palette blue |
   | `--ds-stream-ink` | `#1858c0` | `#2a63c9` | derived |
   | `--ds-stream-surface/-border` | — | `#ecf3fe` / `#c7dcfb` | re-derived tints |
   | `--ds-ring` | `rgba(31,111,235,.28)` | `rgba(66,133,244,.30)` | follows stream |
   | `--ds-shadow-sm/-/-lg` | `rgba(26,26,24,…)` | `rgba(18,21,28,…)` same alphas | the shadow tint derives from ink; ink went cool, the shadows follow (Codex #9) |
   | `--ds-crew-*` | — | *(unchanged, all four)* | identity, not status. Anchor `#1f6feb` no longer matching stream is fine — the token comment anticipated exactly this |
   | `--ds-navy` / `--ds-seal` / `--ds-font-mincho` | — | *(values unchanged)* | promoted to the main palette section in Phase 3 |

   Derived hexes marked "derived"/"re-derived" are proposals; the implementer may tune ±1 step **if** a contrast check (below) demands it, and must record any change in the PR body.
3. **Contrast floor:** any *meaningful* text that reads through `--ds-muted` must keep **≥4.5:1** on `--ds-bg`; paper-mut `#8a9099` is 3.08:1 and fails, which is why the retirement mapping is CONTEXTUAL, not blanket. The desk family uses `--ds-paper-mut` for two different things (Codex blocker #2):
   - **Meaningful text** — the desk's unresolved-outcome paragraph (`ApprovalDesk.svelte` markup ~:439–457, style ~:649–652, inherits 15px body), the estate's loading/degraded status (`EstateView.svelte` ~:289–295) and back-to-desk link (~:278–284), the ledger timestamps (`LedgerStrip.svelte` ~:131–135). These get re-inked to **`--ds-paper-ink-2`** in **Phase 2** (task 2.4) — a visible, deliberate readability improvement that lands before the video.
   - **Decorative/tiny labels** (11–12px meta whose meaning aria already carries) — these keep the light grey and map to **`--ds-faint`** in Phase 3.
   Verify with a contrast checker on: muted/bg, faint/bg, fg-soft/bg, stream-ink/bg, white/navy.
3b. **Blue is three jobs, not one** (Codex blocker #1 — `#4285f4` with white text is 3.56:1, sub-AA): **filled controls** (the composer's Send button, `ChatForm.svelte` ~:318–325, and any other solid-blue fill) move to **`--ds-navy`** — matching the desk CTA and the deck palette; **text-level blue** (links, inline accents) reads through **`--ds-stream-ink`** (`#2a63c9`, 5.4:1 on paper); raw **`--ds-stream`** (`#4285f4`) is reserved for **non-text accents** — borders, meters, glows, the awaiting numeral. Grep `var(--ds-stream)` consumers and classify each into one of the three during Phase 2.
4. **Elevation:** rules, not boxes. `.ds-card` drops `box-shadow` to `none` (hairline + radius carry it). The **composer keeps `--ds-shadow`** — it earned visual primacy in #272 and is the page's acting surface. `Modal` keeps `--ds-shadow-lg` (it floats by definition). Everything else that carries `--ds-shadow-sm` for "card-ness" loses it; anything that pops OVER content (menus, dropdowns like AutonomyPill's) keeps a shadow.
5. **Display face:** `.ds-h1` gains `font-family: var(--ds-font-mincho)` — one rule gives the approval pages (and any SPA h1) the desk hero's serif. `.ds-h2` and below stay gothic. Do NOT sprinkle mincho anywhere else; the desk uses it exactly once and that restraint is the look.
6. **Header nav active state goes navy-filled** (`background: var(--ds-navy); color: #fff`), replacing the white-surface + border treatment. The #272 comment deferred this *because* the control had to serve both worlds; this plan removes that reason. Hover on inactive segments: `color: var(--ds-fg)`, unchanged shape. The header itself re-grounds to `var(--ds-bg)` with a plain rule border-bottom, dropping `--ds-shadow-sm` and `background: var(--ds-surface)`.
7. **Approve button stays green.** Green = decision/ok across the whole system and the templates' pytest suites assert semantics, not hue. It shifts hue automatically via the `--ds-ok` re-value. Do not make it navy; navy is navigation/identity, the seal is the approval moment.
8. **No DOM changes.** This is a stylesheet-and-tokens plan. Every `data-testid`, element tag, and `[data-tour]` target stays put; the tour needs no retargeting. No new endpoints → DEMO_ALLOWLIST untouched.

## Tests that MUST break (update them deliberately, do not "fix" the CSS back)

`frontend/tests/unit/styles.test.ts` (Codex verified this list against `e3ed992` — finding #4):
- `pins the warm-neutral page background per design §3 (#fcfcfb)` (~:129–131) → the ONLY Phase-1 failure in this file. Update pin to `#fbfaf8`, reword title.
- `declares the warm-neutral color palette` (~:59–69) → checks token NAMES only, not values. Passes unchanged; do not touch.
- `defines the composite redesign tokens` (~:133–146) → Phase 1: still passes (block still present). Phase 3: rewrite to assert the paper block is GONE and `--ds-navy`/`--ds-seal`/`--ds-font-mincho` live in the main palette.
- `reserves vermilion for the seal only` → must keep passing untouched in every phase. If it fails you introduced `#c0392b` somewhere — remove it, never relax the test.
- `renders .ds-btn--reject as outline-danger` → must keep passing untouched.

`frontend/tests/unit/infra_graph.test.ts` (~:377–381) pins the Mermaid ghost-node hexes — it breaks when Task 2.4 re-values the `classDef` palette in `infra_graph.ts`, and the new hexes go into the test deliberately.

Nothing else pins paint: `header.visual.ts` has no assertions (screenshots only), smoke asserts structure/layout, and the pytest template suites assert DOM contracts (`tests/unit/test_transparency_template_testids.py` even pins the "no inline styles" rule that makes this whole plan work). Everything else (1647 unit tests, 28 smoke, template pytest suites) must pass unchanged.

---

## Phase 0 — Setup

### Task 0.1: Worktree + baseline

**Step 1:** From `/home/adi/driftscribe` (⚠ shared main worktree — do not work here): verify `git status` shows an **empty index** (untracked files are fine). If anything is staged, stop and report.

**Step 2:**
```bash
git -C /home/adi/driftscribe fetch origin main
git -C /home/adi/driftscribe worktree add .worktrees/two-world -b ui/two-world-unification origin/main
cd /home/adi/driftscribe/.worktrees/two-world/frontend && npm ci
```

**Step 3:** Baseline gates (must be green before any change):
```bash
npx svelte-check --tsconfig ./tsconfig.json   # expect 0 errors
npx vitest run                                 # expect 1647 passed (or more)
```

**Step 4:** Copy this plan into the worktree and commit it as the branch's first commit:
```bash
cp /home/adi/driftscribe/docs/plans/2026-07-30-two-world-unification.md \
   /home/adi/driftscribe/.worktrees/two-world/docs/plans/
git add docs/plans/2026-07-30-two-world-unification.md
git commit -m "docs(plan): two-world unification (ds-qbo)"
```

**Step 5:** Capture BEFORE screenshots for later comparison — run the existing hand-run visual suites so you have a reference set:
```bash
VISUAL_OUT=/tmp/two-world-before npx playwright test \
  --config tests/visual/playwright.visual.config.ts
```
(These specs mock every endpoint; they need `npm run build` only if the config serves the built app — check `tests/visual/playwright.visual.config.ts` and follow it.)

## Phase 1 — The ground shift (one commit, whole-app re-value)

### Task 1.1: Update the styles.test.ts pins FIRST (TDD — they are the spec)

**Files:** `frontend/tests/unit/styles.test.ts`

**Step 1:** Change the background pin to `#fbfaf8`. Update any other hex pins per the mapping table. Run `npx vitest run tests/unit/styles.test.ts` — expect the updated assertions to FAIL against the unchanged tokens.css.

### Task 1.2: Re-value tokens.css

**Files:** `frontend/src/styles/tokens.css`

**Step 1:** Apply the normative mapping table to the legacy token block. Do not touch the `--ds-paper*` composite block yet (desk still reads it — Phase 3 retires it). Rewrite the file-header comment: the direction paragraph should describe ONE world (paper ground, cool ink, navy/gblue accents, rules over boxes, mincho display face). Also fix the stale composite-block comment claiming the paper tokens are "Not yet consumed by any component" — five components consume them (list them), until Phase 3 removes the block.

**Step 2:** `npx vitest run tests/unit/styles.test.ts` — PASS. Then the full suite: `npx vitest run` — investigate ANY other failure; it means a test pins a hue somewhere unexpected (grep before overriding: the #216 lesson — a value renders via more paths than you think).

**Step 3:** Visual sanity: `npm run dev`, walk desk / estate / chat. Expect: everything slightly cooler ink, Google-blue accents, deeper greens. Expect NO layout shifts. (The Jinja approval pages are NOT visible through `npm run dev` — Vite serves the SPA only; they read the BUILT stylesheet, so their inspection waits for Task 2.5's `npm run build`. Codex #12.)

**Step 4:** Commit: `feat(tokens): one world — legacy tokens take the paper values (ds-qbo)`

### Task 1.3: Literal-hex sweep

**Step 1:** Grep for orphaned literals of the OLD values that dodge the token layer — **`*.ts` included** (Codex #3/#8: the first draft's grep missed TypeScript and thus the Mermaid palette):
```bash
grep -rn "1f6feb\|1f8a4c\|9a6b00\|fcfcfb\|e7e6e1\|1a1a18\|rgba(31,\|rgba(26," \
  frontend/src agent/templates --include="*.svelte" --include="*.css" --include="*.ts" --include="*.html"
```
Known hits and their dispositions:
- `frontend/src/lib/infra_graph.ts` ~:202–219 — Mermaid `classDef` colors are hard-coded BY NECESSITY (custom properties don't work in classDef). Do NOT inline vars; re-value the literals per the mapping table in **Task 2.4** (it has its own test to update).
- `frontend/src/components/HistoricalBanner.svelte` ~:72–92 — the amber rgba pulse derives from the OLD warn hue; re-derive from `#9a5b12`.
- `agent/templates/transparency_legacy.html` — **EXEMPT.** A deliberately reachable fallback page (served at `agent/main.py` ~:4820–4829, pinned by `tests/integration/test_ui_transparency.py` ~:119–125) that does NOT link the token bundle; restyling it is out of scope and touching it risks the integration pin. Leave every literal in it alone.
- Crew-token declarations in tokens.css are the one legitimate `#1f6feb`.
Rewrite anything else to the appropriate `var()`.

**Step 2:** `npx vitest run` — PASS. Commit: `fix(styles): sweep hard-coded legacy hexes behind tokens (transparency_legacy exempt)`

## Phase 2 — Structural passes (each task = verify + commit; ALL must land before the video)

### Task 2.1: Header on paper

**Files:** `frontend/src/App.svelte` (styles only, ~lines 1995–2180)

**Step 1:** `.app-header`: `background: var(--ds-bg)` (was `--ds-surface`), keep the 1px border-bottom (now a rule via re-valued `--ds-border`), delete `box-shadow: var(--ds-shadow-sm)`.

**Step 2:** `.app-header__nav-btn.is-active`: `background: var(--ds-navy); color: #fff; border-color: var(--ds-navy);` and delete its `box-shadow`. Remove the now-false "Still no navy fill — serves both design worlds" comment and say why it changed. Keep the transparent-border trick on every segment (prevents the 2px jiggle — #272).

**Step 3:** Check `.app-header__nav` track (`--ds-surface-2` pill well) still reads on the new ground; if it disappears, use `--ds-border`-tinted well instead. Judgment call, verify visually.

**Step 4:** Unit tests + visual: re-run `header.visual.ts` at all 8 widths × 2 locales × desk+chat; compare against `/tmp/two-world-before`. Nav must read as THE primary control; nothing may collide at 390/861/1440.

**Step 5:** Commit: `feat(header): paper ground, navy-filled active nav (ds-qbo)`

### Task 2.2: Card elevation — rules, not boxes

**Files:** `frontend/src/styles/base.css` (`.ds-card`), then grep `shadow` across `frontend/src/components/*.svelte`

**Step 1:** `.ds-card { box-shadow: none; }` (keep border, radius, padding). This reaches the Jinja approval pages too — that is the point.

**Step 2:** Component sweep for `--ds-shadow`: keep shadows ONLY on (a) `ChatForm` composer (`--ds-shadow`, earned in #272), (b) `Modal` (`--ds-shadow-lg`), (c) genuine popovers (AutonomyPill dropdown, DemoNoticeBell popover, HelpHint if floating). Everything else styled as a resting card loses it. List every removal in the commit body.

**Step 3:** `npx vitest run` (the #272 chat-hierarchy pins may reference shadows — if one fails, read it; the test may be RIGHT and the surface genuinely a popover). Visual: `chat-hierarchy.visual.ts` re-run + eyeball.

**Step 4:** Commit: `feat(styles): cards rest on rules, not shadows (ds-qbo)`

### Task 2.3: Display face on H1

**Files:** `frontend/src/styles/base.css` (`.ds-h1`)

**Step 1:** Add `font-family: var(--ds-font-mincho);` to `.ds-h1`. Grep `.ds-h1` consumers (SPA + templates) and eyeball each: approval page titles should now match the desk hero's face. If any SPA consumer looks wrong in mincho (e.g., a modal form heading), give THAT consumer a scoped gothic override rather than weakening the rule.

**Step 2:** Template pytest (from repo root, in the worktree). The suites live under **`tests/unit/` and `tests/integration/`** (there is NO `agent/tests/` — Codex #12): `tests/unit/test_transparency_template_testids.py`, `test_iac_approval_template.py`, plus the integration suites `test_approvals.py`, `test_iac_approval_get.py`, `test_iac_approval_post.py` and the pause/autonomy gate suites (~99 tests total across the set #272 ran). Expect PASS — they assert structure, not fonts.

**Step 3:** Commit: `feat(type): mincho display face for page titles (ds-qbo)`

### Task 2.4: Bounded audit — the enumerated fixes, then a timeboxed walkthrough

The token shift does 90%; this task is the audited 10%, and it is **bounded** (Codex #13: an open-ended "fix whatever the walkthrough flags" was the plan's largest schedule risk). Two halves:

**Half A — MANDATORY, enumerated (no judgment needed):**

1. **Mermaid palette** — `frontend/src/lib/infra_graph.ts` ~:202–219: re-value the hard-coded `classDef` hexes per the mapping table (managed green → `#1a6b52` family, drift amber → `#9a5b12` family, ghost/hidden per their mapped neutrals). Update the pinned hexes in `frontend/tests/unit/infra_graph.test.ts` ~:377–381 to match. CSS custom properties cannot reach `classDef` — the literals are correct as literals.
2. **Send button → navy** — `ChatForm.svelte` ~:318–325 per design decision 3b. White on `--ds-navy` is ~15:1.
3. **`--ds-stream` consumer classification** — grep `var(--ds-stream)` across `frontend/src`; move text-colored uses to `--ds-stream-ink`, filled controls to `--ds-navy`, leave non-text accents. List the classification in the commit body.
4. **Desk-family readable text off paper-mut** — per design decision 3: `ApprovalDesk.svelte` unresolved-outcome paragraph, `EstateView.svelte` status + back link, `LedgerStrip.svelte` timestamps → `--ds-paper-ink-2`. (This is what makes Phase 3's remaining paper-mut→faint mapping safe.)
5. **HistoricalBanner pulse** — already covered in Task 1.3 if done there; verify.

**Half B — TIMEBOXED walkthrough (max half a day):** with `npm run dev`, walk BOTH locales: chat (empty / thread open / historical replay), estate (populated + degraded), desk (resting / drift / approved). Fix ONLY concrete screenshot defects — leftover old-world reads, `--ds-surface` panels floating on the warmed ground, doubled borders. Do NOT restructure layouts (#272 settled hierarchy), and do NOT do subjective refinement of `InfraDiagram`/`DecisionsRail`/`AutonomyPill` internals without a screenshot defect to point at — file a follow-up bead instead and move on. The deadline outranks polish that the video won't show.

**Step: verify + commit** — `npx vitest run`; commit per component touched, message pattern: `fix(<area>): paper-world audit — <what>`

### Task 2.5: Approval pages verified against the real stylesheets

**Step 1:** `npm run build` (regenerates `agent/static/`), then run the smoke config's local uvicorn (the rig `tests/smoke/playwright.smoke.config.ts` starts it — check how, reuse it) or the template pytest fixtures to render `approval.html` and `iac_approval.html`. Screenshot: normal, paused, autonomy-locked, expired states (the #272 reject-outline verification recipe — reuse it; locked states matter because Reject is the sole live control there and is now outline-danger on a new ground).

**Step 2:** Acceptance: an approval page and the desk side by side must read as the same product. If the page body sits on `--ds-bg` with a white `.ds-card` sheet and mincho h1, it does.

**Step 3:** Commit anything fixed: `fix(approval-pages): paper-world audit`

## Phase 3 — Retirement (zero visual delta; may trail past 8/10 but MUST ship by 8/19)

### Task 3.1: Rewrite paper refs to canonical names

**Files:** `ApprovalDesk.svelte` (~25 refs), `EstateView.svelte` (~24), `InstrumentBand.svelte` (~9), `LedgerStrip.svelte` (~10) — **68 retiring references in FOUR components** at `e3ed992` (Codex #11). `SealStamp.svelte` is EXEMPT: it reads only `--ds-seal`, which survives. Catch stragglers with: `grep -rn "ds-paper\|ds-gblue\|ds-ok-green\|ds-drift-amber" frontend/src` (Task 2.4 may have shifted counts slightly — the grep is authoritative, the counts are orientation).

**Step 1:** Mechanical rewrite per mapping, **longest name first** — `--ds-paper-ink-2` before `--ds-paper-ink` before `--ds-paper`, or an ordered string replacement manufactures garbage like `--ds-bg-ink` (Codex #11): `--ds-paper-ink-2`→`--ds-fg-soft`, `--ds-paper-ink`→`--ds-fg`, `--ds-paper-mut`→`--ds-faint` (safe ONLY because Task 2.4 already moved meaningful text to ink-2 — see design decision 3), `--ds-paper-rule`→`--ds-border`, `--ds-paper`→`--ds-bg`, `--ds-ok-green`→`--ds-ok`, `--ds-drift-amber`→`--ds-warn`, `--ds-gblue`→`--ds-stream`.

**Step 2:** Delete the composite block from tokens.css except `--ds-navy`, `--ds-seal`, `--ds-font-mincho` — move those three into the main palette with a short comment each. Delete the BEWARE-two-worlds comment entirely; it is no longer true, and that is the whole point.

**Step 3:** Update the `defines the composite redesign tokens` test per the "tests that MUST break" section.

**Step 4:** Prove zero delta: rebuild, re-run `desk.visual.ts` (all three states) + the estate/wayfinding specs, and diff against screenshots taken just before this task. Any pixel diff beyond antialiasing noise = a mapping mistake; fix the mapping, not the screenshot.

**Step 5:** `npx vitest run` + `npx svelte-check` — green. Commit: `refactor(tokens): retire the paper-world duplicates — one vocabulary (ds-qbo)`

## Phase 4 — Ship

### Task 4.1: Full gates
```bash
cd frontend && npx svelte-check --tsconfig ./tsconfig.json && npx vitest run
npm run build && npm run test:smoke        # ui-smoke is a REQUIRED CI gate; run it locally first
# repo root: the approval-template pytest suites (same set #272 ran, ~99 tests)
```

### Task 4.2: Acceptance matrix (visual, hand-verified, both locales)

| Surface | States | Accept when |
|---|---|---|
| Desk | resting / drift / approved | unchanged from pre-plan except token-value drift ≤ Phase-1 shift |
| Estate | populated / degraded | diagram + shell read as one sheet |
| Chat | empty / thread / historical replay | no white-on-white floats; composer is still the primary; stream accents are gblue |
| Header | 390–1600px, both locales | navy-filled active segment; no collisions; no shadow |
| approval.html | normal / paused / locked / expired | same product as the desk; Reject legible in locked state |
| iac_approval.html | pending / trusted / failed / done | same |

### Task 4.3: PR → Codex → merge → deploy

1. Push, open PR titled `feat(ui): two-world unification — one paper world (ds-qbo)`. Body: before/after screenshots, the mapping table, every deviation.
2. CI green (required: `ui-smoke`, GitGuardian, lint-test, frontend, static-gate).
3. Codex review via `mcp__codex__codex` (sandbox `workspace-write`, `approval-policy: "never"`, no model param). Findings are advisory — verify claims against the code before acting (standing lesson: five of the last six reviews found something real, but each also made at least one wrong claim).
4. Merge; deploy the coordinator per the **driftscribe-deploy skill** — build lands at 0% traffic, `update-traffic` is a REQUIRED second step; verify `cpu-throttling=false` survives (eventarc fast-ack invariant, #268).
5. Live-verify on prod (driftscribe-live-probe skill recipes), then: `git status` must show an EMPTY INDEX before `bd close ds-qbo` (mutating bd commits whatever is staged).
6. **Announce the video re-shoot is unblocked** — that is the downstream consumer of this work.

## Hard constraints (carried from the repo + memory — violating any is a review-blocker)

- **Never work in `/home/adi/driftscribe` main worktree** — it is shared. Everything in `.worktrees/two-world`.
- **Empty git index before ANY mutating `bd` command.** Read-only `bd show/list/ready` are safe.
- Beads is local-only here: never `bd dolt push`.
- Use `mv` (move-aside), never `rm`.
- Vermilion `#c0392b` appears exactly once, on the seal. The pin stays.
- No DOM/testid/`[data-tour]` changes. No new endpoints (DEMO_ALLOWLIST untouched).
- Subagents, if used, get an explicit cheaper model (`sonnet` default).
- JA is the default locale; every visual check runs in both.

## Ship order under the deadline

Phases 0–2 are the video-blocking slice — target merged+deployed by **8/4**, leaving 8/5–8/7 for the re-shoot. Phase 3 rides the same PR if time allows (preferred: one review cycle); if the schedule pinches, split it into a follow-up PR that must merge before 8/19. Phase 3 is pure rename — it must never change a rendered pixel, which is also why it is safe to trail the video. Task 2.4's Half B is hard-timeboxed; the escape valve for everything subjective is a follow-up bead, never schedule creep.

## Codex review notes (2026-07-30, thread `019fb101-c3c3-7e13-8038-c47ae6bb9309`)

Plan reviewed pre-handoff; **all 13 findings accepted, none rejected**, all folded in above. The two blockers reshaped design decisions 3/3b: (1) the draft's contrast arithmetic was wrong — `#6f7581` muted missed its own 4.5:1 floor and Google blue as a filled control with white text is 3.56:1, hence the three-jobs-of-blue split and navy fills; (2) the blanket `paper-mut→faint` retirement would have frozen real sub-AA body text on the desk/estate/ledger, hence the contextual Phase-2 re-ink. Should-fixes: Mermaid `classDef` literals + their test pin (the diagram does NOT follow the token re-value), `*.ts` in the literal sweep, `transparency_legacy.html` exemption, shadow tint re-derivation, corrected pytest paths, corrected Phase-3 counts (68 refs / 4 files, SealStamp exempt) + longest-name-first ordering, and the Task 2.4 timebox. For the post-implementation review, continue THIS thread via `mcp__codex__codex-reply` so Codex can judge the work against its own findings.
