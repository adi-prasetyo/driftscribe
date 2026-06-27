# Chat-feature user-facing copy — Implementation Plan

> **For Claude:** small, self-contained frontend + docs change. TDD where there's logic; copy + README are direct edits with a test pin on the changed string.

**Goal:** Add user-facing copy that explains the two chat features that shipped functionally but undocumented — multi-turn persisted/resumable conversations, and cross-crew "team memory" — so an operator (and a judge) can discover what they are without reading the code.

**Architecture:** Frontend + docs only. No backend, no `capabilities.py` (the `read_conversations` / `read_team_log` tool descriptions already exist there from P3). The narrative home for chat is the `ConversationsRail` (where chat history lives in the UI) plus the README Status section. We do NOT invent a new SPA block for "team memory" — that would be scope creep and muddy the CapabilityCard's "generated from enforcement code" provenance. The rail's existing `flex-wrap: wrap` header + `HelpHint`'s `flex-basis:100%` inline panel already support a header hint (same pattern as `DecisionsRail`'s PR-numbering hint).

**Tech Stack:** Svelte 5 SPA, vitest + @testing-library/svelte, existing `HelpHint.svelte` component.

**Voice:** de-AI'd house voice (memory `de_ai_home_copy`): no em dashes, plain conversational sentences, periods. Honest framing — "look back / read-only", never overstate.

---

## Copy (final strings)

**Rail HelpHint** (`testid="conversations-help"`, `ariaLabel="About conversations"`), shown always in the header after the title:
> Your chats are saved here, so you can reopen any thread and pick up where you left off. Each conversation stays with the crew that started it, and crews can look back at each other's recent chats as shared team memory.

**Empty state** (replaces "No conversations yet."):
> No conversations yet. Chats you start are saved here, so you can reopen any thread and keep going.

**README Status bullet** (new bullet under `## Status`):
> - **Multi-turn chat + team memory:** operator chats with each crew are persisted and resumable from a history rail in the operator UI. Crews can also read each other's recent conversations as shared, read-only "team memory" (turn text is secret-redacted and snippet-capped), so a question asked of one crew can inform the others.

---

### Task 1: Rail HelpHint + richer empty state

**Files:**
- Modify: `frontend/src/components/ConversationsRail.svelte`
- Test: `frontend/tests/unit/ConversationsRail.test.ts`

**Step 1 — Update the existing empty-state test + add a HelpHint test (red).**
- Change the empty-state assertion to the new string (substring match on "No conversations yet." via a function matcher, so the longer copy still matches).
- Add: renders a `conversations-help` button; it is present even with zero conversations; clicking it reveals a panel whose text contains "saved here" and "team memory".

**Step 2 — Run, expect fail** (`conversations-help` not found yet).

**Step 3 — Implement.**
- `import HelpHint from './HelpHint.svelte';`
- In `.rail-header`, after the `<h2>`, add:
  `<HelpHint testid="conversations-help" ariaLabel="About conversations" text="Your chats are saved here, so you can reopen any thread and pick up where you left off. Each conversation stays with the crew that started it, and crews can look back at each other's recent chats as shared team memory." />`
- Replace the empty-state `<p>` text with the richer copy.

**Step 4 — Run the file green.**

**Step 5 — `npx svelte-check` clean; full `npx vitest run` green** (catch any other test asserting the old empty string — smoke fixtures, App.conversations).

**Step 6 — Commit.**

### Task 2: README Status bullet

**Files:** Modify `README.md` (`## Status` section, add the bullet above or below the existing two).

**Step 1 — Add the bullet.** No test (docs). Visual read for voice + accuracy.

**Step 2 — Commit.**

---

## Verification
- `npx vitest run` — all unit green.
- `npx svelte-check` — 0 errors.
- `npx playwright test` (smoke) — resume-after-reload still green (no behavior change, but the rail markup changed).
- Manual read of all three strings against the de-AI voice.

## Addendum — Codex review (thread 019f0919), folded

Codex reviewed the plan; adopted four points:
1. **HelpHint split into clearer sentences + signal redaction** so it never implies crews get raw transcripts. Final: "Your chats are saved here, so you can reopen any thread and pick up where you left off. Each conversation stays with the crew that started it. Crews can also look back at redacted snippets of each other's recent chats as shared team memory."
2. **README softened** "can inform" → "can help inform" (the breadcrumb is a nudge; deeper context needs the crew to call `read_conversations`).
3. **Add one bullet to `docs/OVERVIEW.md` §7 "A few good-to-know facts"** — README points new readers to OVERVIEW as the plain-English tour, and `## Status` is buried. Final: "**Chat has memory, and so does the crew.** Conversations with each crew are saved and resumable from a history rail in the UI, and each thread stays with the crew that started it. Crews can also read redacted snippets of each other's recent chats as shared, read-only \"team memory,\" so context carries across the team."
4. **Strengthen the HelpHint test** to pin the trust boundary: assert non-status aria label, panel collapsed by default, and the opened panel mentions both the resume story ("reopen") and the redaction/team-memory boundary ("redacted" + "team memory").

ProtoPedia stays out (post-submission churn; keep the change small).

## Out of scope (deliberate)
- (b) giving Explore/crews a whole-system overview — a **separate agent** owns this.
- New SPA "team memory" panel / Tour step — not proportionate; CapabilityCard already lists the tools, rail HelpHint + README carry the narrative.
- `capabilities.py` edits — already shipped in P3.
