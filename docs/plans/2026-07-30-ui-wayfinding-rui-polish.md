# UI Wayfinding + Refactoring-UI Polish Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or
> superpowers:subagent-driven-development) to implement this plan task-by-task.

**Goal:** Fix the "clicked a desk numeral, landed on the estate view, couldn't
find my way back" wayfinding failure, and apply the Refactoring-UI critique
items (header emphasis inflation, chat border monotony, rail meta noise,
solid-red reject buttons, bare empty states) — all small-diff, before the
self-imposed ~8/3 freeze for the 8/19 finalist pitch (UI/UX 見づらい・後付け
was the judges' only negative).

**Architecture:** All SPA work lives in `frontend/src` (Svelte 5, no router —
view state is a pure function over `location.search`, see
`frontend/src/lib/deeplink.ts`). The header, view switching, and layout live in
`frontend/src/App.svelte`. Shared `.ds-*` classes in
`frontend/src/styles/base.css` are consumed by BOTH the SPA and the
server-rendered Jinja approval pages (`agent/templates/approval.html`,
`agent/templates/iac_approval.html`) — a `.ds-btn--reject` restyle lands on
both automatically. Tokens in `frontend/src/styles/tokens.css` are the single
source of truth; add no new hex values.

**Tech stack:** Svelte 5 (runes), Vite, Vitest + @testing-library/svelte
(jsdom), Playwright for local smoke/visual. Frontend gates: `npm run check` +
`npm run test:unit` (CI); `npm run test:smoke` and `tests/visual/` are LOCAL
gates (memory: `frontend_smoke_suite_is_a_separate_gate`).

**Out of scope (filed as a backlog bead, Task 14):** full two-world
unification (restyling chat/estate components into the paper/navy/mincho
composite world). Phase B here removes the header's share of the seam; the
rest is post-judging work.

---

## Context a fresh engineer needs

- **Views:** `desk` (default front door), `estate` (infra map, UI label
  インフラ), `chat`. Switcher = three-button nav in the header
  (`App.svelte:1652`). View is written to `?view=` with
  `history.replaceState` ONLY (`App.svelte:235`) — there is **no popstate
  listener**, so browser Back exits the app. That is bug #1.
- **InstrumentBand** (`frontend/src/components/InstrumentBand.svelte`): the
  three big numerals, rendered by BOTH `ApprovalDesk.svelte:277` and
  `EstateView.svelte:101`. All three stats call `onNavigate('estate')` —
  including あなたの承認待ち whose content (the approval queue) is on the
  *desk*. The component's own header comment flags this ("Task 3.5 may
  revisit the routing"). That is bug #2.
- **navigate()** (`App.svelte:226`) also tears down chat state (historical
  replay, open conversation, handoff chip) when leaving chat, and clears
  chat-intent params (`CHAT_INTENT_PARAMS` in `lib/deeplink.ts`) from the
  URL. The tour calls `navigate(v, { preserveChatState: true })` and must NOT
  spam history (it borrows views mid-tour).
- **i18n:** every user-visible string goes through the `$t` store with keys in
  `frontend/src/locales/*.ts` (JA + EN both required). Aria strings are
  separate catalog keys (see `desk.band.*Aria`).
- **`bd` gotcha:** mutating `bd` commands commit whatever is staged. `git
  status` must show an EMPTY INDEX before any `bd create/close/update`.
- **Shared worktree warning:** multiple agents share `/home/adi/driftscribe`.
  Do the implementation in a side worktree under `.worktrees/`.

Run all frontend commands from `frontend/`. Run `npm run test:unit -- <file>`
for a single test file.

---

## Phase A — Wayfinding

### Task 1: History-aware view navigation (pushState + popstate)

**Files:**
- Modify: `frontend/src/App.svelte` (`navigate()` at ~226; add popstate
  effect near the other `$effect`/onMount wiring; extract the chat teardown
  block at ~242-262 into a named function)
- Test: `frontend/tests/unit/App.test.ts` (extend existing suite)

**Design (revised after Codex review — all four points below are Codex
findings, verified against the cited lines):**

- `navigate(v, opts)` gains `opts.history?: 'push' | 'replace'`, default
  `'push'`, but a push happens ONLY on an actual **view transition**:

```ts
const fromView = view;            // capture BEFORE `view = v`
const shouldPush =
  opts.history !== 'replace' &&
  v !== fromView &&
  u.href !== window.location.href;
```

  Rationale: URL inequality alone is NOT enough. (a) `openConversation()` /
  `openTrace()` call `navigate('chat')` internally (App.svelte:643, :1469),
  including from the boot continuation (:1626) — on a `/?conversation=c1`
  boot the URL "changes" only by gaining `view=chat`, and pushing there
  stacks a duplicate chat entry so Back appears dead. (b) Clicking デスク
  while on `?view=desk` canonicalizes to `/` — URL differs, view doesn't;
  must not push.
- ALL programmatic/boot-time `navigate('chat')` calls inside
  `openConversation`/`openTrace`/boot restoration pass
  `history: 'replace'` explicitly — deep-link restoration continues the
  current entry, it doesn't create one. Grep every `navigate(` call site and
  classify: user-gesture call sites (header nav, band, estate back link,
  adopt bridge) push; programmatic ones replace.
- The tour's `tourNavigate` and its close-restore
  (`navigate(tourReturnView, …)` at ~:493) pass `history: 'replace'`.
- **Teardown must cancel async chat work, not just clear fields.** Extract
  ~:242-262 into `teardownChatSurface()` and extend it with the cancellation
  half of `newChat()`'s pattern (App.svelte:1570): `++runSeq` (else a
  delayed `openConversation` response passes its guards — :645/:675 — and
  repopulates `conversationTurns` after Back has left chat), plus resets for
  `busy`, `resumingConversation`, and `liveExchange`. Read `newChat()` and
  reuse — extract a shared helper if the overlap is near-total.
- Add a `popstate` listener (in `onMount`, removed on destroy). View-only
  restore + **canonicalize the restored entry** (Codex's option 2 — no
  deep-state reopen before the freeze):

```ts
function onPopstate(): void {
  const target = viewFromSearch(window.location.search);
  if (tourOpen) {
    // Back during the tour: close the overlay WITHOUT its view-restore
    // (tourReturnView = null first) — otherwise closeTour() replaces the
    // very entry the user just returned to.
    tourReturnView = null;
    closeTour();
  }
  if (target !== 'chat' && view === 'chat') teardownChatSurface();
  view = target;
  // The restored entry may still carry chat-intent params for content this
  // session deliberately tore down (e.g. ?conversation=c1 after leaving
  // chat). The UI won't reopen them (view-only restore), so the URL must
  // stop claiming them: strip CHAT_INTENT_PARAMS via replaceState. Since
  // those params are what forced view 'chat' (hasChatIntent), write
  // ?view=chat explicitly in the same rewrite or a reload would land on
  // the desk. Only rewrite when a stale param actually exists AND its
  // content is not currently open (conversationId / historicalTraceId
  // mismatch) — a live entry stays untouched.
}
```
- Keep every non-view URL write (trace sync, conversation sync, param
  stripping) on `replaceState` — they mutate the current entry, unchanged.

**Step 1: Write failing tests** in `frontend/tests/unit/App.test.ts`
(follow the suite's existing mount/fetch-mock conventions; read its top ~80
lines first). **Spy on `history.pushState`/`history.replaceState`** — do NOT
assert on `history.length` (jsdom history can't be reset between cases, and
Back leaves forward entries that later pushes replace without growing
length):
- "header nav click pushes": click `[data-testid=nav-chat]` → exactly one
  `pushState` call, URL contains `view=chat`.
- "same-view click replaces, never pushes": click `nav-desk` while on desk →
  zero `pushState` calls.
- "boot deep-link does not push": mount with `?conversation=c1` (mock the
  GET) → zero `pushState` calls during boot restoration.
- "popstate restores the view": simulate Back by setting the URL
  (`history.replaceState(null, '', '/?view=desk')` inside the test) and
  dispatching `new PopStateEvent('popstate')` → desk content
  (`[data-testid=instrument-band]`) renders.
- "popstate off chat cancels late fetches": open a conversation whose fetch
  resolves late; popstate to desk; resolve the fetch → `conversationTurns`
  stays empty (assert via the UI: no thread rendered on return to chat).
- "popstate strips stale chat-intent params": leave chat (teardown ran),
  simulate Back to a `?conversation=c1` entry → one `replaceState` whose URL
  has no `conversation` but has `view=chat`.

**Step 2:** `npm run test:unit -- tests/unit/App.test.ts` — new tests FAIL.

**Step 3:** Implement as designed above.

**Step 4:** `npm run test:unit -- tests/unit/App.test.ts` — all pass.
`npm run check` — clean.

**Step 5: Real-browser Back/Forward coverage** — jsdom cannot prove actual
history traversal. Add a case to the smoke suite
(`frontend/tests/smoke/`, see `transparency.smoke.ts` for conventions):
desk → chat → estate, `page.goBack()` twice asserting chat then desk,
`page.goForward()` asserting chat. **The smoke suite is a REQUIRED CI gate**
(`.github/workflows/ci.yml:99-144`, `ui-smoke` job) — it must be green, and
it's safe locally (mocked, boots a local uvicorn; `npm run build` first).

**Step 6:** Commit: `fix(spa): view navigation writes real history entries; Back works`

### Task 2: Per-stat routing — あなたの承認待ち goes to the desk queue

**Files:**
- Modify: `frontend/src/components/InstrumentBand.svelte`
- Modify: `frontend/src/components/ApprovalDesk.svelte:277`,
  `frontend/src/components/EstateView.svelte:101` (band call sites)
- Modify: `frontend/src/locales/desk.ts` (aria strings say the destination)
- Test: `frontend/tests/unit/InstrumentBand.test.ts`

**Design (revised after Codex review):** the band gets a
`context: 'desk' | 'estate'` prop plus
`onStat: (stat: 'managed' | 'drift' | 'awaiting') => void`. `context` drives
which stats are interactive, which hover hint each shows (Task 3), and the
aria wording — `onStat` alone cannot express per-consumer inertness/hints.

- **ApprovalDesk (`context="desk"`):** managed/drift → `onNavigate('estate')`;
  awaiting → scroll AND focus its own pending-approval card. The target is a
  non-focusable `<div>` today (ApprovalDesk.svelte:344) — give it
  `id="desk-pending"` **and `tabindex="-1"`**, then
  `el.scrollIntoView({block:'start'}); el.focus({preventScroll:true})`
  (an `id` alone moves nothing for keyboard/SR users). When `awaiting` is
  `0` or `null` there is no pending card to land on — render the awaiting
  stat **non-interactive** in that state.
- **EstateView (`context="estate"`):** managed/drift render as
  **non-interactive figures** (a `<span>`-based stat, not a disabled
  `<button>` — a disabled button drops out of keyboard navigation and helps
  nobody); awaiting → `onNavigate('desk')` (non-interactive when 0/null,
  same rule as desk).
- **Aria must say the destination** — the band's `aria-label`s override all
  descendant text (InstrumentBand.svelte:107), so Task 3's visible hints are
  invisible to screen readers unless the label carries them. Extend the
  catalog per context: `desk.band.managedAria` →
  "{n} managed by IaC — view infrastructure map" (JA:
  「IaC 管理下 {n}件 — インフラを見る」), same for drift;
  `desk.band.awaitingAriaDesk` ("…— jump to the queue below") vs
  `desk.band.awaitingAriaEstate` ("…— view on desk"). Non-interactive
  figures keep today's plain labels. Unknown-state keys unchanged.
- **Consumer tests are REQUIRED, not optional** — the band test only proves
  `onStat` fired; it cannot catch ApprovalDesk mapping awaiting back to
  estate. Extend `frontend/tests/unit/ApprovalDesk.test.ts` (its existing
  band test only clicks managed — :242) and `EstateView.test.ts`:
  - desk: awaiting click → no `onNavigate` call, pending card receives focus
  - desk: managed + drift clicks → `onNavigate('estate')`
  - desk, awaiting=0: awaiting stat is not a button
  - estate: managed/drift are not buttons; awaiting click →
    `onNavigate('desk')`

**Steps:** failing band tests (`InstrumentBand.test.ts`: per-context
interactivity + `onStat` emission) → failing consumer tests as above →
implement band + both consumers → all green:
`npm run test:unit -- tests/unit/InstrumentBand.test.ts tests/unit/ApprovalDesk.test.ts tests/unit/EstateView.test.ts`
→ `npm run check` → commit:
`fix(desk): awaiting-approval numeral stays on the desk; per-stat routing`

### Task 3: Visible hover affordance on the stat buttons

**Files:**
- Modify: `frontend/src/components/InstrumentBand.svelte` (markup + styles)
- Modify: `frontend/src/locales/desk.ts` (+ EN)

**Design:** each active stat button gets a hint line that appears on
hover/focus-visible where the label sits (label swaps to hint via opacity, or
hint appended after label — implement as a third `<span
class="instrument-band__hint">` shown on hover/focus):
- managed/drift (desk): `インフラを見る →` / `View infrastructure →`
  (key `desk.band.statHintEstate`)
- awaiting (desk): `下の承認キューへ ↓` / `To the queue below ↓`
  (key `desk.band.statHintDesk`)
- Hints render ONLY on interactive stats (Task 2's `context` prop decides);
  estate's managed/drift figures show none. Note the hint text is visual-only
  sugar — the accessible destination lives in the aria labels (Task 2), since
  the button's `aria-label` overrides descendant text for screen readers.
CSS: hint `font-size: 11.5px; color: var(--ds-paper-ink-2)`, hidden by
default (`opacity: 0; position: absolute` under the label or
`height: 0` reveal — simplest: absolutely position over the label, fade in;
respect `prefers-reduced-motion` via the existing global kill switch).
Keep the existing `:hover { opacity: .75 }` on the numeral but scope it so
the hint itself doesn't fade.

**Steps:** implement (pure presentational — no unit test beyond `npm run
check` + existing band tests still green), eyeball with
`npm run dev` (desk + estate, both locales), commit:
`feat(desk): stat numerals announce their destination on hover`

### Task 4: Estate arrival context — quiet "← デスクに戻る" link

**Files:**
- Modify: `frontend/src/components/EstateView.svelte` (top of the view,
  above/beside its existing heading; it already receives `onNavigate`)
- Modify: `frontend/src/locales/desk.ts` (key `desk.estate.backToDesk`, JA
  `← デスクに戻る`, EN `← Back to desk`)
- Test: extend `frontend/tests/unit/EstateView.test.ts` if it exists (check
  `ls frontend/tests/unit | grep -i estate`); otherwise cover via
  `App.test.ts`: on estate view, clicking `[data-testid=estate-back-desk]`
  renders the desk.

**Design:** a `.rail-more`-style quiet text button (reuse that class from
`base.css:489` — it is exactly this affordance) with
`data-testid="estate-back-desk"`, calling `onNavigate('desk')`. Always
rendered (it is a destination link, not a history pop — valid even when the
visitor deep-linked straight to `?view=estate`).

**Steps:** failing test → implement → pass → commit:
`feat(estate): quiet back-to-desk affordance`

---

## Phase B — Header demotion (the pill soup)

### Task 5: Header layout — stable 3-region grid, nav is primary

**Files:**
- Modify: `frontend/src/App.svelte` (header markup ~1640-1702, styles
  ~1878-1996)

**Design:**
- `.app-header` becomes `display: grid; grid-template-columns:
  minmax(0, 1fr) auto minmax(0, 1fr);` — brand left (justify-self: start),
  nav center, actions right (justify-self: end). `minmax(0, 1fr)`, NOT plain
  `1fr` (Codex): the wrapping actions cluster (App.svelte:1959) exerts
  min-content pressure that plain `1fr` transmits into the grid. Nav
  position stops depending on wrap/viewport (today it drifts — compare
  `desk-live-00181.png` vs `chat-1440.png`).
- `@media (max-width: 860px)`: two rows — `grid-template-columns: auto 1fr;`
  brand + actions on row 1, nav spanning row 2 (`grid-column: 1 / -1;
  justify-self: center`). Nav NEVER wraps mid-cluster.
- Nav gains weight: `.app-header__nav-btn` font-size `var(--ds-fs-2)` (15px,
  up from 13), padding `0.35em 1em`. Give **every** segment
  `border: 1px solid transparent` and the active one
  `border-color: var(--ds-border-strong)` — the base button is `border: 0`
  today (App.svelte:1934), so an active-only border would jiggle the row by
  2px on every switch (Codex). Keep the white-surface fill + shadow. Do NOT
  introduce navy fill (must serve both design worlds).

**Steps:** implement → `npm run check` → existing `App.test.ts` still green
(`npm run test:unit -- tests/unit/App.test.ts`) → eyeball at
390/760/**861/900/1024**/1440px (the 861-1024 band is where the wide actions
cluster fights the grid — Codex flagged it) in `npm run dev` → commit:
`fix(header): stable grid layout; nav reads as primary navigation`

### Task 6: Demote the utility cluster

**Files:**
- Modify: `frontend/src/components/LocaleToggle.svelte` (styles only)
- Modify: `frontend/src/components/TokenStatus.svelte`
- Modify: `frontend/src/App.svelte` (`.app-tour-btn` class usage ~1694)
- Tests: `frontend/tests/unit/LocaleToggle.test.ts`,
  `frontend/tests/unit/TokenStatus.test.ts` (if present — check; behavior
  unchanged, only assert nothing broke)

**Design (Refactoring UI: de-emphasize secondaries instead of emphasizing
everything):**
- **LocaleToggle:** drop the outer `border` + `background` pill chrome
  (`LocaleToggle.svelte:54-56`) — plain inline pair; active segment
  `color: var(--ds-fg); font-weight: var(--ds-fw-semibold)`, inactive
  `var(--ds-faint)`, hover `var(--ds-fg)`. Keep hit areas ≥ 24px and the
  existing aria.
- **Tour button:** replace `ds-btn ds-btn--ghost` with a quiet text button —
  keep the compass icon, `color: var(--ds-muted)`, hover `var(--ds-fg)`,
  no border/background. Keep `data-testid="tour-open"` (tour tests use it).
- **TokenStatus (`ok` state only):** swap `ds-pill--ok` (green fill) for a
  muted treatment. The component always renders a key icon
  (`TokenStatus.svelte:31`) — KEEP it and recolor it `var(--ds-ok)` as the
  quiet "healthy" signal (no separate dot; the icon IS the dot), with the
  label text in `var(--ds-muted)` and no filled-pill background.
  `missing`/`invalid` keep today's `ds-pill--muted`/`ds-pill--danger`
  emphasis (problems SHOULD be loud; 健全 state should be quiet). Implement
  by extending its `VARIANTS` map (`TokenStatus.svelte:22`) with a dedicated
  class rather than branching in markup. There is NO TokenStatus unit test
  today — add a minimal one (ok state gets the quiet class + keeps the
  accessible label; invalid state keeps `ds-pill--danger`).
- **AutonomyPill / PausePill / DemoNoticeBell:** unchanged (autonomy is a
  load-bearing judge-facing control; the bell is already an icon).
- Acceptance: on an all-healthy header the ONLY color-filled chips are the
  autonomy pill and the pause pill's status dot; nav is the strongest
  element.

**Steps:** run the two component test files first (green baseline) →
implement → re-run + `npm run check` → eyeball both locales → commit:
`fix(header): demote locale/tour/token utilities to quiet affordances`

---

## Phase C — Chat view hierarchy (fewer boxes, one primary)

### Task 7: `Group` quiet variant for metadata drawers

**Files:**
- Modify: `frontend/src/components/Group.svelte`
- Modify: `frontend/src/components/Timeline.svelte` (Group call sites ~167,
  ~195, ~219 — read the file to map which is reasoning vs tools vs mcp)
- Test: `frontend/tests/unit/Group.test.ts` (create if absent — check
  `ls frontend/tests/unit | grep -i group`)

**Design:** add `variant?: 'card' | 'quiet'` prop (default `'card'`, zero
visual change for existing consumers). `quiet`:
- root: no border, no background, no radius (`.group--quiet`)
- summary: `ds-label`-style muted row (13px, semibold, `var(--ds-muted)`)
  with the ▸ marker; top hairline `border-top: 1px solid var(--ds-border)`
- open content: `background: var(--ds-surface-2); border-radius:
  var(--ds-radius); padding: var(--ds-sp-3) var(--ds-sp-4)`; drop the
  `border-top` the card variant uses.
Timeline passes `variant="quiet"` for the **tools** and **mcp** groups;
**reasoning** stays `card` (it is the page's substance). The e2e contract at
`Group.svelte:8-11` (`<details id="group-{key}">` + `[data-group]` child)
must survive both variants.

**Steps:** failing test (quiet variant renders `.group--quiet`, still
`<details id="group-tools">`) → implement → pass → `npm run test:unit` (full,
Timeline tests must stay green) → commit:
`feat(chat): tools/MCP drawers demoted to quiet disclosures`

### Task 8: Demote CapabilityCard, elevate the composer

**Files:**
- Modify: `frontend/src/components/CapabilityCard.svelte` (styles; read it
  first)
- Modify: `frontend/src/components/ChatForm.svelte` (styles; read it first)
- Test: existing `CapabilityCard.test.ts` must stay green.

**Design:**
- CapabilityCard (このエージェントにできること・できないこと): restyle its
  container to match Task 7's quiet-summary look (muted 13px summary, no
  card box when closed). Content when open keeps its current inner layout on
  a `--ds-surface-2` well. Do not change its DOM contract (tests +
  tour may target it — grep `data-tour`/`data-testid` inside before editing).
- ChatForm: promote one level — container `box-shadow: var(--ds-shadow)`
  (up from `sm`/none) + `border-color: var(--ds-border-strong)`. The blue
  送信 button stays the page's single saturated element.
- Result: chat page reads composer-first; boxed cards drop from ~6 to 2
  (composer + reasoning).

**Steps:** implement → `npm run test:unit` + `npm run check` → eyeball chat
view (empty chat + replayed conversation, both locales) → commit:
`fix(chat): capability drawer quieted; composer is the visual primary`

---

## Phase D — Rail meta noise

### Task 9: Conversation rail — drop the repeated 1-count meta

**Files:**
- Modify: `frontend/src/components/ConversationsRail.svelte:95` (`turnsLabel`)
- Test: `frontend/tests/unit/ConversationsRail.test.ts` (check name via
  `ls frontend/tests/unit | grep -i conv`)

**Design:** `turnsLabel` returns `''` when the count is `1` (most rows —
repeated "・1件のメッセージ" carries zero information; counts ≥ 2 keep the
full label, which then IS information). Time stays. Check
`DecisionsRail.svelte` for the same pattern and apply the same rule if it
has one (read it; if its meta is a status, leave it).

**Steps:** the existing suite PINS the current behavior —
`ConversationsRail.test.ts:197` asserts a "1 message" label renders — so
this is a test *replacement*, not an addition: rewrite that assertion to
expect NO conv-count element at count 1, add "count 3 → label renders" →
implement (one-line change to the `< 1` guard → `<= 1`) → pass →
commit: `fix(rail): singleton message counts are noise; show from 2 up`

---

## Phase E — Approval pages: reject is not a primary action

### Task 10: `.ds-btn--reject` → outline-danger

**Files:**
- Modify: `frontend/src/styles/base.css:379-387`
- Check-only: `agent/templates/approval.html`, `agent/templates/iac_approval.html`
  (the only consumers — verified by grep), and
  `frontend/tests/unit/styles.test.ts` (NOTE: it pins selector EXISTENCE
  only, not the reject declarations — styles.test.ts:155 — so no pin blocks
  this; extend it with a declaration pin for the new outline treatment).

**Design (RUI "destructive ≠ prominent"). Semantics differ per page — Codex
correction:** the **IaC** Reject is a non-binding local decline
(agent/main.py:5905), but the **rollback** Reject is BINDING — it calls the
worker's deny operation (agent/main.py:6947) — and under pause/autonomy
lockout it is the ONLY enabled action while Approve is disabled
(approval.html:143). Outline-danger is still right (destructive ≠ loud), but
it must remain unmistakably present when it's the sole live control:

```css
.ds-btn--reject {
  background: transparent;
  border-color: var(--ds-danger-border);
  color: var(--ds-danger-ink);
}
.ds-btn--reject:hover {
  background: var(--ds-danger-surface);
  border-color: var(--ds-danger);
}
```
Approve stays the single filled action on both approval pages.

**Steps:** read `styles.test.ts` → extend it with a declaration pin
(transparent background + danger-ink color on `--reject`) → implement →
`npm run test:unit -- tests/unit/styles.test.ts` → server-render check:
`rg -n "ds-btn--reject" agent/templates/` unchanged (class names untouched)
→ run the template pytest if one covers these pages
(`cd /home/adi/driftscribe && uv run pytest tests -k "template or approval" -x`
— use the invocation `AGENTS.md`/CI uses; check `.github/workflows` if
unsure) → **visually verify the rollback approval page in its normal,
paused, and autonomy-blocked states** (approval.html:143 — Reject is the
sole enabled action in the locked states and must not read as inert
furniture) → commit:
`fix(approval): reject demoted to outline — approve is the one primary`

---

## Phase F — Empty states say what comes next

### Task 11: Actionable empty copy for the reasoning group

**Files:**
- Modify: `frontend/src/locales/timeline.ts` (or wherever
  `misc.group.emptyState` lives — grep it) + EN counterpart
- Modify: `frontend/src/components/Timeline.svelte` (pass a per-group empty
  key only for reasoning; others keep the generic)

**Design:** reasoning group empty state becomes guidance, not absence:
- JA: `質問を送ると、コーディネーターの推論がここに流れます。`
- EN: `Send a question and the coordinator's reasoning will stream here.`
Tools/MCP keep the current generic line (they are demoted drawers now).
Mechanism: `Group` already takes `empty`; add optional `emptyText?: string`
prop that overrides the generic when provided (default keeps today's
behavior for all other call sites).

**Steps:** failing test (Group renders `emptyText` when given —
extend Task 7's Group.test.ts) → implement → pass → commit:
`feat(chat): reasoning empty state tells the operator what to do`

---

## Phase G — Verification, PR, ship

### Task 12: Full local gates + visual pass

- `cd frontend && npm run build && npm run check && npm run test:unit &&
  npm run test:smoke` — ALL green. **`test:smoke` is a REQUIRED CI gate**
  (`ui-smoke` job, `.github/workflows/ci.yml:99-144`) — an earlier draft of
  this plan wrongly called it local-only. It is safe to run locally (boots a
  mocked local uvicorn, never touches prod) but `npm run build` must run
  first or it serves a stale shell.
- Screenshot pass with `npm run dev` (or the built bundle) at 1440px: desk,
  estate, chat × JA/EN — compare against `live-desk-1440.png` /
  `chat-1440.png` for the before/after.

**Acceptance matrix (from the Codex review — walk every row):**
- Back AND Forward across desk/chat/estate.
- Back from an open conversation and from a reasoning replay (URL stops
  claiming the torn-down content).
- Boot with `?conversation=`, `?reasoning=`, `?ask_pr=`, `?preview_pr=` —
  no extra history entries (Back leaves the app in one step, as before).
- Back while the tour is borrowing the estate view.
- Desk awaiting stat at 0, unknown (`null`), and positive counts.
- Jinja approval pages (rollback + IaC) in normal, paused, and
  autonomy-blocked states.
- Header at 390 / 760 / 861 / 900 / 1024 / 1440 px.

### Task 13: PR → CI → Codex → merge → deploy

- Branch `ui/wayfinding-rui-polish` from `main`, one PR, body maps changes to
  the judge feedback (見づらい・後付け) and this plan doc.
- CI green (includes GitGuardian — REQUIRED check per memory).
- Codex review via `mcp__codex__codex-reply` on the plan-review thread; treat
  findings on merits.
- On CI-green + Codex-SHIP: merge and deploy autonomously (memory
  `deploy_autonomy`) — invoke the `driftscribe-deploy` skill; coordinator
  build then `update-traffic` (traffic is pinned; two-step dance).
- Close beads (`bd close` — EMPTY INDEX first), update memory.

### Task 14: Backlog bead — two-world unification (NOT in this PR)

File a `ds-` bead: "Unify chat/estate onto the composite paper world" —
direction: chat + estate components adopt `--ds-paper*` ground, mincho
display headings where the desk uses them, navy accents; retire the legacy
editorial-blue card look; kill the near-duplicate token pairs
(`--ds-ok`/`--ds-ok-green`, `--ds-warn`/`--ds-drift-amber`) once nothing
reads the legacy side. Post-judging unless the freeze slips favorably.

---

## Ship order under the freeze (Codex recommendation, adopted)

Tasks 1-4 (wayfinding) are the **must-ship slice** — they fix the reported
failure. Then Tasks 5-6 (header) and 10 (reject outline) if visual review
stays clean. Tasks 7-9 + 11 are lower-risk polish but broaden the regression
surface — land them last, and drop them without guilt if the freeze bites.
If splitting into two PRs, split along exactly that line (1-4 first).

---

## Codex review notes (2026-07-30, thread 019fae98-895f-75b1-910b-302af98b158a)

The plan above already incorporates every accepted finding. For the record:
duplicate-push on boot deep-links and same-view canonicalization (Task 1
push guard); stale chat-intent URLs after view-only popstate (Task 1
canonicalization); incomplete async teardown — `runSeq`/`busy`/
`resumingConversation`/`liveExchange` (Task 1); tour-vs-Back interaction
(Task 1); `context` prop + non-interactive figures + `tabindex="-1"` focus
target + destination-aware aria + mandatory consumer tests (Tasks 2-3);
transparent-border layout shift + `minmax(0,1fr)` + 861-1024px band
(Task 5); TokenStatus icon-recolor spec + missing unit test (Task 6);
ConversationsRail.test.ts:197 pin replacement (Task 9); rollback Reject is
BINDING and sole-enabled under lockout + styles.test.ts pins existence only
(Task 10); `ui-smoke` is a required CI gate + no `history.length`
assertions in jsdom — spy on pushState/replaceState, prove real traversal
in Playwright (Tasks 1/12). No findings were rejected.

---

## Task → bead map (ALREADY CREATED 2026-07-30 — claim, don't re-create)

- **ds-7ag** (epic, P1): UI wayfinding + RUI polish (judge feedback: 見づらい・後付け)
  - **ds-7ag.1** (P1): Back button + history entries (plan Task 1)
  - **ds-7ag.2** (P1): per-stat routing + hover hints + estate back link
    (plan Tasks 2-4)
  - **ds-7ag.3** (P1): header grid + utility demotion (plan Tasks 5-6)
  - **ds-7ag.4** (P2): chat hierarchy — quiet drawers, composer primary
    (plan Tasks 7-8)
  - **ds-7ag.5** (P2): rail meta + reject outline + empty states
    (plan Tasks 9-11)
- **ds-qbo** (P3, standalone backlog, NOT this PR): two-world unification
  (plan Task 14)

`bd update <id> --claim` when starting a bead; `bd close` when its tasks land.
⚠ Before ANY mutating `bd` command: `git status` must show an EMPTY INDEX
(bd commits whatever is staged — see CLAUDE.md).

---

## Handoff state (as of 2026-07-30, prepared by the planning session)

- **Worktree ready:** `.worktrees/ui-polish` on branch
  `ui/wayfinding-rui-polish` (created from `main` @ 924de9c). `npm ci` done
  in its `frontend/`.
- **Green baseline verified there:** `npm run check` → 0 errors /
  0 warnings (670 files); `npm run test:unit` → 1609/1609 pass (61 files).
  Any red after your changes is yours.
- **This plan doc is UNTRACKED in the main worktree.** Copy it into the
  worktree and make it the branch's first commit, so the PR carries its own
  plan.
- **Do not work in the main worktree** (`/home/adi/driftscribe`) — multiple
  agents share it.
- A Codex review of this plan was requested; its accepted findings are folded
  into the task text above (see "Codex review notes" below if present).
