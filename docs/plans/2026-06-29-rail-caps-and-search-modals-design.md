# Rail caps + search/browse modals — design

Date: 2026-06-29

## Background / motivation

The left column holds two rails — Conversations (top) and Past Decisions (below).
Both render the full backend-capped list (`/conversations?limit=50`,
`/decisions?limit=50`). Because `.layout` (App.svelte) uses `min-height:
calc(100vh - 56px)` (a floor, **not** a fixed height) with `align-items: start`,
the inner `overflow-y: auto` on `#decisions-list` **never engages** — there is no
bounded ancestor height for it to scroll within. Measured live (1280×600
viewport, 22 decisions / 8 grouped rows): `.rails` column = 2496px,
`document.scrollHeight` = 2549px > 600px → **the whole page scrolls, the rail does
not self-scroll.** So as the lists grow toward the 50 cap, the column grows down
the page.

**Goal:** cap each rail to its most-recent rows, and add a "Search …" affordance
that opens a modal listing the full (already-loaded) set with a live text filter.

## Decisions (confirmed with the user)

- **Rail caps:** Conversations → newest **5**; Past Decisions → newest **10**
  (counting *grouped PR rows*, the way the operator sees them, not raw docs).
- **Chat search scope:** **title + crew only** (client-side). No message-body
  search, no backend endpoint, no extra fetch.
- **Modal list:** **full list + live filter.** No infinite scroll, no 7-day
  window.
- **Affordance visibility:** shown **only when total > cap**; label carries the
  count — "Search chats (N) →" / "Search decisions (N) →".
- **Decisions modal display:** reuse the existing grouped-by-PR rail card.
- **Decisions search fields:** PR title, `#<pr_number>` / bare number, crew +
  action (raw + friendly label), and the friendly status labels.
- **Frontend-only.** No backend changes; modal renders the same in-memory arrays
  the rails already hold (no new network call).

## Non-goals (YAGNI)

Infinite scroll; date-window filtering; message-body / full-text search; a
server-side search endpoint; bumping the `limit=50` fetch (the modal covers the
50 most-recent — trivially raisable later via a "Load more" if it ever matters).

## Architecture

### 1. Shared modal shell — `frontend/src/components/Modal.svelte` (new)

A small, reusable accessible dialog. The codebase has **no** existing modal;
`AutonomyPill.svelte` is the closest a11y reference (Esc handling, focus
restore, viewport math) but it is an *anchored popover*, not a centered modal —
so a fresh component is warranted.

- **Props:** `open: boolean`, `title: string`, `onClose: () => void`, and a
  `children` snippet for the body. (`labelledById` optional — default to a
  generated id on the `<h2>` title.)
- **Render:** nothing when `!open` (mount/unmount, cheap). When open: a fixed
  full-viewport backdrop + a centered panel.
  - Panel: `role="dialog"`, `aria-modal="true"`, `aria-labelledby` = title id; a
    header row with the `<h2>` title + a close button (`aria-label="Close"`, the
    `x` Icon glyph); a scrollable body region hosting `children`.
- **Behaviour:**
  - **Esc** closes (window keydown while open).
  - **Backdrop click** closes; clicks inside the panel do not (guard on
    `event.target === backdrop`).
  - **Focus:** on open, move focus into the panel; on close, restore focus to the
    element focused before open (the trigger). Minimal inline focus trap
    (Tab/Shift-Tab cycle across the panel's tabbables) — no new dependency.
  - **Body scroll lock** while open (`document.body` overflow hidden, restored on
    close) so the page behind doesn't scroll.
  - Wired/unwired via `$effect` keyed on `open`; the effect's teardown removes the
    listener, restores focus, and unlocks scroll (also covers unmount-while-open).
  - `prefers-reduced-motion`: any fade/scale is gated off under reduce.
- **CSS:** backdrop `position: fixed; inset: 0; z-index` above the header,
  translucent overlay (existing token or `color-mix` on `--ds-fg`),
  `display: flex; align-items: center; justify-content: center`. Panel:
  `--ds-surface`, radius, `--ds-shadow`, `width: min(92vw, 640px)`,
  `max-height: 80vh`, `display: flex; flex-direction: column`. Body region
  `overflow-y: auto; min-height: 0` — **this** modal genuinely bounds its height
  (`max-height: 80vh`), so the inner overflow works (the bug the rail has does not
  recur here).

### 2. Conversations: cap + search — `ConversationsRail.svelte`

- New prop `max: number` (default **5**). The list is already newest-first, so
  slice the newest `max` **before** bucketing:
  `groupConversations(conversations.slice(0, max), new Date())`. Keeps "5 newest
  across Today/Yesterday/Older" semantics.
- Extract the per-conversation `<button>` markup into a `{#snippet conversationButton(c)}`
  used by both the rail list and the modal (no markup duplication).
- When `conversations.length > max`: render a `<button class="rail-more">Search
  chats ({conversations.length}) →</button>` below the list (i.e. at the bottom
  of the conversations rail, above Past Decisions). Opens the modal
  (`showSearch = true`).
- ConversationsRail owns its `showSearch` + `query` state and renders `<Modal>`
  itself. Modal body: a `type="search"` input (autofocus, `aria-label="Search
  chats"`, bound to `query`), a result count, and the **full** filtered list
  grouped the same way:
  `groupConversations(conversations.filter((c) => matchesConversation(c, query)), now)`,
  rendered via the shared `conversationButton` snippet.
- New pure helper in `lib/conversations.ts`:
  `matchesConversation(c: Conversation, query: string): boolean` — case-insensitive
  substring over `c.title`, `c.workload`, and `crewName(c.workload)`. Empty /
  whitespace query → `true` (show all).
- Open-a-result: `handleOpen(id) { showSearch = false; onOpen(id); }` (closing the
  modal first; harmless when already closed in the rail path).

### 3. Decisions: cap + search — `DecisionsRail.svelte`

- New prop `max: number` (default **10**). `groupRailDecisions(decisions)` first
  (the existing fold), then the rail renders the first `max` `RailItem`s; the
  modal renders **all** filtered items.
- When `railItems.length > max`: render "Search decisions ({railItems.length}) →"
  below the list. (Count = grouped rows = what the operator perceives.)
- The `decisionCard` snippet already exists and is reused as-is in both the rail
  and the modal. Open-trace from inside the modal must close it first:
  introduce `handleOpenTrace(t) { showSearch = false; onOpenTrace(t); }` and use it
  in the snippet in place of the direct `onOpenTrace` (harmless in the rail path).
- New pure helpers in `lib/rail.ts`:
  - `matchesDecision(d: Decision, query: string): boolean` over: `pr_title`,
    `#${pr_number}` and the bare `String(pr_number)`, `action` (raw +
    `decisionActionLabel(action)`), crew (`d.workload` + `crewName`), and the
    friendly status (`iacApplyMeta(apply_status, merge_state).label`) plus raw
    `apply_status` / `merge_state`. Empty query → `true`.
  - `railItemMatches(item: RailItem, query: string): boolean` —
    single → `matchesDecision(d)`; group → `docs.some(matchesDecision)`.

### 4. Crew-name helper — `lib/workloads.ts`

Centralize `crewName(value: string | undefined): string` (value→display, e.g.
`drift`→`Anchor`) in `workloads.ts`, built off `WORKLOADS`. Replace the inline
`CREW_NAME`/`crewName` currently duplicated in `ConversationThread.svelte` so the
rail search predicate and the thread byline share one source.

### 5. App.svelte

Pass `max={5}` / `max={10}` explicitly (or rely on component defaults). No data
or fetch changes.

## Accessibility

Modal: `role="dialog"`, `aria-modal`, labelled by its title, Esc to close, focus
trap + restore, backdrop-click close, body scroll-lock, reduced-motion-safe. The
search input is `type="search"` with an accessible name and is autofocused on
open; the results region exposes a visible count.

## Testing (vitest + svelte-check; full gate)

- `matchesConversation`: title hit; crew-key hit (`drift`); crew-name hit
  (`anchor`); case-insensitivity; empty→all; no-match.
- `matchesDecision` / `railItemMatches`: `pr_title`; `#168` and `168`; action raw
  + friendly; status (`applied`, `merged`, "merge pending"); crew; group-any;
  empty→all.
- `ConversationsRail`: renders ≤`max` rows; affordance hidden when ≤`max`, shown
  with the right count when >`max`; modal opens on click; typing narrows the
  list; clicking a result calls `onOpen` and closes.
- `DecisionsRail`: ≤`max` grouped rows; affordance with count; modal reuses
  `decisionCard`; typing narrows; open-trace closes the modal.
- `Modal`: nothing rendered when closed; Esc / backdrop / close-button each call
  `onClose`; focus moves into the panel on open (focus-restore + trap as far as
  jsdom allows).
- **Regression guard:** audit existing `ConversationsRail` / `DecisionsRail`
  tests that render more than the default cap — pass a large `max` (or confirm
  their fixtures are under the cap) so the cap doesn't hide rows they assert.

## Risks / notes

- Bucketed cap for conversations uses slice-before-group → "N newest" semantics
  (not "N newest within each bucket").
- The modal is the first reusable modal shell; `AuthPanel.svelte` is an existing
  one-off `role="dialog"` div (no focus trap, no scroll lock).

## Revisions after Codex review (2026-06-29)

Codex (thread `019f133d`) reviewed the plan; deltas adopted:

1. **Modal = native `<dialog>` + `showModal()`/`close()`** (not a hand-rolled
   `role="dialog"` div). Rationale: the browser gives focus-trap, focus-restore,
   Esc, and **top-layer** rendering for free — so no z-index war with
   `AuthPanel` (z-index 100) and no hand-rolled trap (the existing AuthPanel div
   has no trap, a latent gap a list-bearing modal must not repeat).
   - Drive imperatively from `$effect` keyed on `open`: `open && !el.open →
     el.showModal()`; `!open && el.open → el.close()`. Wire the dialog's native
     `close`/`cancel` events to call `onClose()` so Esc keeps parent state in
     sync (else the effect re-opens it).
   - Backdrop click: `onclick` on the dialog, close when `e.target === dialogEl`
     (a `::backdrop` click reports the dialog as target).
   - Body scroll-lock via a tiny **ref-counted** module helper (lock depth), so an
     overlap with another modal/tour can't unlock early.
   - Focus the search input after `showModal()` (query a `[data-modal-autofocus]`
     inside the dialog) rather than relying on the `autofocus` attribute.
   - Styling mirrors AuthPanel (`ds-card` panel, translucent backdrop, rise/fade
     gated under `prefers-reduced-motion`), but on `::backdrop`.
   - **jsdom**: add `HTMLDialogElement.prototype.showModal/show/close` mocks to
     `tests/unit/setup.ts` (jsdom doesn't implement `showModal`). Consistent with
     the existing `animate`/`matchMedia` mocks. Focus-trap/backdrop are
     runtime-only (Playwright-verified), not unit-asserted.

2. **Active-pin (avoids a regression):** capping can hide the row the operator
   just opened from the modal (an older chat/trace falls outside newest-N), losing
   the active-row affordance. Cap = "newest N **plus** the active item if it falls
   outside." Pure helper, both rails: conversations → include the active
   conversation in the sliced array *before* grouping (re-buckets correctly);
   decisions → append the active `RailItem` when it's beyond the cap.

3. **Search normalization:** a `normalizeForSearch(s)` (lowercase →
   non-alphanumerics to spaces → collapse whitespace) applied to BOTH the query
   and a composed haystack string, so `iac apply` matches `iac_apply`, `docs pr`
   matches `docs_pr`, `applied merged` matches `applied & merged`, and `PR #168` /
   `pr 168` / `#168` / `168` all match. Decision haystack composes: `pr_title`,
   `PR #<n>`, `<n>`, `action` + `decisionActionLabel(action)`, crew (`workload` +
   `crewName`), `apply_status` + `merge_state` + `iacApplyMeta(...).label`.

4. **`handleOpenTrace` at BOTH open-trace sites** in `decisionCard` (the main
   button *and* each lifecycle step button) — not just the first — so opening from
   the modal always closes it.

5. **Grouped status-only match** (an older lifecycle doc matches a status query
   the face row doesn't display): no `forceOpen` param added — `pr_title`/
   `pr_number` are shared across a group so title/number queries always hit the
   face, and a status-only match is already surfaced by the existing lifecycle
   `<summary>` composition (`lifecycleSummaryLabel`). Documented, not coded.

Not adopted: relabeling the affordance to "Browse/View all" (the user chose
"Search …"); native-only with no jsdom path (we keep unit coverage via the mock).
