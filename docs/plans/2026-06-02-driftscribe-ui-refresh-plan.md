# DriftScribe Operator UI Refresh — Implementation Plan

**Date:** 2026-06-02
**Branch:** `feat/ui-refresh-svelte`
**Design:** `2026-06-02-driftscribe-ui-refresh-design.md`

This plan is written so independent subagents can execute leaf tasks without
drift. Interfaces are fixed up front (§3). Appendices A/B are the authoritative
contracts. Each task lists exact files + a verification command.

## 1. Principles

- **Preserve the Playwright contract verbatim** (Appendix B). The spec
  (`tests/e2e/ui/transparency.spec.ts`) and `playwright.config.ts` are NOT
  edited.
- **Re-home, never drop, security guards** (design §5 table).
- **Additive + reversible.** Legacy page at `/ui/transparency-legacy`. Built
  assets gitignored. Not deployed, not merged.
- **TDD for `lib/*`:** write the vitest spec from the interface in §3, then the
  implementation, per `superpowers:test-driven-development`.

## 2. Phase order (dependencies)

```
P1 Scaffold ─┬─> P2 lib/* (+ vitest)  ─┐
             └─> P3 styles/tokens       ├─> P4 components ─> P5 FastAPI shell+approval restyle ─> P6 test migration ─> P7 build/CI/Docker ─> P8 verify+PR
                                        ┘
```
P2 modules are independent of each other (parallel). P4 components depend on P2
interfaces (fixed in §3), so they parallelize once P2 interfaces exist (tests
may still be red). P5/P6/P7 are sequential. P8 is the gate.

## 3. Fixed interfaces (the anti-drift contract)

### `lib/workloads.ts`
```ts
export type Workload = 'drift' | 'upgrade' | 'explore' | 'provision';
export interface WorkloadOption { value: Workload; label: string; }
// Labels are the operator-facing text; VALUES are the /chat API contract.
export const WORKLOADS: WorkloadOption[] = [
  { value: 'drift',     label: 'Cloud Run config' },
  { value: 'upgrade',   label: 'Dependencies' },
  { value: 'explore',   label: 'Explore (read-only)' },
  { value: 'provision', label: 'Provision (infra edits)' },
];
```

### `lib/api.ts`
```ts
export const TOKEN_KEY = 'driftscribe_token';
export type TokenState = 'ok' | 'missing' | 'invalid';
// try-then-prompt: send stored token if any; on 401/403 clear + signal prompt.
export function getStoredToken(): string | null;
export function setToken(t: string): void;
export function clearToken(): void;
// Resolves with the Response. onAuthRequired() is called (once) on 401/403 so
// the UI can show AuthPanel; if it returns a token, the request is retried.
export function apiFetch(
  path: string,
  init?: RequestInit,
  onAuthRequired?: () => Promise<string | null>,
): Promise<Response>;
```

### `lib/sse.ts`
```ts
export type ChatEvent =
  | { event: 'llm_thought'; trace_id: string; workload: string; thought_text: string }
  | { event: 'tool_call'; trace_id: string; workload: string; tool_name: string; tool_args: Record<string, unknown> }
  | { event: 'tool_result'; trace_id: string; workload: string; tool_name: string; result_preview: string; result_ok: boolean }
  | { event: 'llm_usage'; trace_id: string; workload: string; prompt_token_count: number|null; candidates_token_count: number|null; thoughts_token_count: number|null; total_token_count: number|null }
  | { event: 'final_response'; trace_id: string; workload: string; response_preview: string; response_kind: 'json'|'text' };
export interface ChatMeta { trace_id: string }
export interface ChatDone { reply: string; tool_calls: string[]; session_id: string }
export interface ChatError { detail: string; status_hint?: number }
export interface SseHandlers {
  onMeta(m: ChatMeta): void;
  onEvent(e: ChatEvent): void;
  onDone(d: ChatDone): void;
  onError(e: ChatError): void;
}
// Consumes a fetch Response body (text/event-stream) and dispatches frames.
export async function consumeSse(resp: Response, h: SseHandlers): Promise<void>;
// Pure frame parser (for unit tests): splits an SSE chunk buffer into frames.
export function parseSseFrames(buffer: string): { frames: Array<{event?: string; data: string}>; rest: string };
```

### `lib/timeline.ts`
```ts
import type { ChatEvent } from './sse';
export type GroupKey = 'coordinator' | 'tools' | 'mcp';
export interface TraceEvent extends Record<string, unknown> {
  event: string; trace_id: string; workload?: string;
  insert_id?: string; timestamp?: string;
}
// Which group an event belongs to. llm_thought/llm_usage/final_response →
// coordinator; tool_call/tool_result → tools UNLESS the tool is an MCP tool →
// mcp. Matches the existing renderer's binning.
export function groupOf(e: TraceEvent): GroupKey;
// Pairs tool_call+tool_result by tool_name within a group (existing behavior).
export function groupEvents(events: TraceEvent[]): Record<GroupKey, TraceEvent[]>;
// Stable identity for <details> open-state persistence.
export function eventKey(e: TraceEvent): string; // "evt:" + insert_id (or synthetic)
// Status derivation for the status pill.
export type TimelineStatus = 'pending'|'streaming'|'complete'|'stalled'|'error';
```

### `lib/labels.ts`
```ts
// Friendly worker/tool labels (preserve exact strings the old renderer used).
export const WORKER_LABELS: Record<string, string>; // keyed by tool __name__
export function labelFor(toolName: string): string;  // falls back to raw name
// Required entries (asserted): read_live_env_tool→"Reader (drift)",
// developer_knowledge*→"Developer Knowledge MCP", upgrade_read_dependencies_tool→"Upgrade Reader",
// upgrade_propose_pr_tool→"Upgrade Docs", open_infra_pr_tool→"Open infra PR".
export const MCP_TOOL_PREFIXES: string[]; // to route MCP tools to the mcp group
```

### `lib/approval.ts`
```ts
// SECURITY: same-origin guard for HITL approval links. Accept relative
// ("/approvals/...") and absolute ("https://<coordinator>/approvals/...") that
// resolve to window.location.origin; reject off-origin / non-http(s) schemes.
// Returns the safe href string, or null if rejected.
export function safeApprovalHref(raw: string, origin?: string): string | null;
export function isExpired(expiresAtIso: string | null | undefined, now?: number): boolean;
```

### `lib/format.ts`
```ts
export function fmtTokens(usage: { total_token_count?: number|null }): string;
export function shortTrace(traceId: string): string; // first 8 chars
export function fmtPreview(s: string, max?: number): string;
```

### Component contract (Appendix B is authoritative for IDs/testids)
`App.svelte` owns state (token state, current trace, events[], decisions[],
historical mode, auth-panel visibility) and composes the children. Each child
renders the exact IDs/testids/classes/attrs in Appendix B.

## 4. Tasks

### P1 — Scaffold (sequential, 1 agent)
- Create `frontend/` with: `package.json` (svelte, vite, @sveltejs/vite-plugin-svelte,
  typescript, svelte-check, vitest, @playwright/test, jsdom/@testing-library/svelte),
  `vite.config.ts` (outDir `../agent/static`, manifest true, single entry
  `src/main.ts`), `svelte.config.js`, `tsconfig.json`, `vitest.config.ts`,
  `index.html` (dev), `src/main.ts` (mount App into `#app`), empty `src/App.svelte`.
- `package.json` scripts: `dev`, `build`, `check` (svelte-check), `test:unit`
  (vitest run), `test:smoke` (playwright, see P7).
- **Verify:** `cd frontend && npm install && npm run build` produces
  `agent/static/`; `npm run check` clean on the empty app.

### P2 — lib/* with vitest (parallel fan-out, 1 agent per module, TDD)
For each of `workloads`, `api`, `sse`, `timeline`, `labels`, `approval`,
`format`: write `tests/unit/<name>.test.ts` from §3, then `src/lib/<name>.ts`.
- **Security-critical:** `approval.test.ts` must cover: relative accepted,
  same-origin absolute accepted, off-origin rejected (→ null),
  `javascript:`/`data:` rejected, missing/garbage rejected. `api.test.ts` must
  cover: token sent when present, 401/403 clears token + invokes onAuthRequired +
  retries with returned token, CF-Access path (no token, 200, no prompt).
- **Verify:** `npm run test:unit` green.

### P3 — Design system styles (parallel with P2, 1 agent)
- `src/styles/tokens.css` (CSS custom props per design §3) + `src/styles/base.css`
  (shared element + component classes, including approval-page classes:
  `.ds-card`, `.ds-field`, `.ds-btn`, `.ds-btn--approve`, `.ds-btn--reject`,
  `.ds-pre`, `.ds-note`, `.ds-blocked`, `.ds-pill*`). Imported by `main.ts` so
  they land in the built CSS the approval pages also link.
- Honor `prefers-reduced-motion`.
- **Verify:** referenced by `main.ts`; build includes the CSS.

### P4 — Components (parallel fan-out once §3 + Appendix B fixed)
Author each component in Appendix B. Critical structural rules:
- `Timeline.svelte`/`Group.svelte`: render real `<details id="group-coordinator">`
  (open by default), `<details id="group-tools">`, `<details id="group-mcp">`,
  each with child `<div class="events" data-group="...">`. The child must be
  visible when the `<details>` is open (Playwright sets `.open=true`).
- `ChatForm.svelte`: `<form id="chat-form">` with `<input id="prompt-input"
  data-testid="chat-prompt" placeholder="Ask the coordinator…">`,
  `<select id="workload-select">` from `WORKLOADS`, `<button id="send-btn"
  data-testid="chat-submit">`. `.historical` class dims/disables in historical mode.
- `FinalResponse.svelte`: `<section id="final-response-card"
  data-testid="final-response">`, `.error` class on error, `hidden` until filled.
- `DecisionsRail.svelte`: `<aside id="decisions-rail" data-testid="past-decisions-pane">`
  → `<ul id="decisions-list">` → `<li class="decision-row"
  data-testid="past-decision-item">` each with `<button class="open-trace-btn"
  data-testid="open-trace-button">open trace →</button>` and the same-origin
  `Approve →` CTA (expired → strikethrough + `.expired-badge`).
- `HistoricalBanner.svelte`: `<div id="historical-badge"
  data-testid="historical-banner" data-active>` + `#historical-trace-id` +
  `#new-chat-btn` ("← new chat").
- `TraceBadge.svelte`: `#trace-badge` with trace pill (click-to-copy) +
  `#status-pill` (states pending/streaming/complete/stalled/error).
- `AuthPanel.svelte`: inline token entry (replaces `window.prompt()`); writes
  `sessionStorage['driftscribe_token']`; `TokenStatus.svelte` shows
  `#token-status` (ok/missing/invalid) + `#change-token-btn`.
- `App.svelte`: wires SSE submit (live) + poll fallback + historical replay +
  decisions load + auth flow; the **settle** animation (transition on event
  rows; flip on reflow) gated by reduced-motion.
- **Verify:** `npm run check` clean; `npm run build` succeeds.

### P5 — FastAPI shell + approval restyle (sequential, 1 agent)
- Rename current `agent/templates/transparency.html` →
  `transparency_legacy.html`; add route `GET /ui/transparency-legacy` serving it
  (same headers as today).
- New `agent/templates/transparency.html`: thin shell — `<div id="app"></div>`,
  manifest-resolved `<script type="module">` + `<link rel="stylesheet">`,
  `<title>DriftScribe — Reasoning Timeline</title>`, keeps `meta robots
  noindex`. Must contain `DriftScribe` and `reasoning timeline` text and `id="app"`.
- `agent/main.py`: add `_vite_asset(name)` manifest reader (cached; dev
  fallback to `/static/<name>`), mount `StaticFiles(directory=.../static,
  check_dir=False)` at `/static`, pass asset URLs into the shell context.
  Keep `Cache-Control: no-store`, unauthenticated GET.
- Restyle `approval.html` + `iac_approval.html`: replace inline `<style>` with a
  `<link rel="stylesheet" href="{{ ds_css }}">` (manifest-resolved) + new
  `ds-*` classes. **Keep every Jinja block, the `<form method="post">`, hidden
  `t`/`form_token` fields with their `data-testid`, and all conditionals
  unchanged.** Pass `ds_css` into their template context.
- **Verify:** `uv run pytest tests/integration/test_approvals.py
  tests/integration/test_iac_approval_get.py -q` green (form/testids intact);
  app imports cleanly.

### P6 — Test migration (sequential, 1 agent)
- Rewrite `tests/unit/test_transparency_template_testids.py`:
  `test_transparency_template_has_required_testids` greps
  `frontend/src/**/*.svelte` for each required testid; `test_data_group_unchanged`
  greps frontend src for `data-group="coordinator|tools|mcp"`;
  `test_sessionstorage_key_documented` greps frontend src for `driftscribe_token`;
  `test_approval_template_has_testids` UNCHANGED (approval.html still Jinja).
- Rewrite `tests/integration/test_ui_transparency.py`: keep the shell-route
  tests (200/html/no-store/no-token/"DriftScribe"); replace the IDs/JS-grep
  tests with shell assertions (`id="app"`, bundle/script reference). Add a
  comment pointing to the re-homed vitest guards for each removed assertion.
- Confirm `tests/unit/test_playwright_config.py` still passes unchanged.
- **Verify:** `uv run pytest tests/unit/test_transparency_template_testids.py
  tests/unit/test_playwright_config.py tests/integration/test_ui_transparency.py -q` green.

### P7 — Build / CI / Docker (sequential, 1 agent)
- `Dockerfile.agent`: prepend `FROM node:24-slim AS frontend` builder
  (`WORKDIR /build`, COPY `frontend/`, `npm ci`, `npm run build` →
  `/build/agent/static`); in the python stage `COPY --from=frontend
  /build/agent/static ./agent/static` AFTER `COPY agent/`. Keep everything else.
- `.dockerignore` + `.gitignore`: add `frontend/node_modules`, `frontend/dist`,
  `agent/static/`.
- `.github/workflows/ci.yml`: add `frontend` job (node 24 → npm ci → build →
  check → test:unit) and `ui-smoke` job (python+node → build → uvicorn →
  `npm run test:smoke`). Leave `lint-test` unchanged.
- `frontend/tests/smoke/transparency.smoke.ts` + a tiny runner that starts
  `uvicorn agent.main:app` and points Playwright at it with `page.route` mocks
  for `/chat` (canned SSE), `/decisions`, `/trace`. Assert the 7 testids, the
  three-group `.open` behavior, historical mode, auth panel.
- `Makefile`: `ui`, `ui-dev`, `ui-smoke` targets.
- **Verify:** `npm run build`; `docker build -f Dockerfile.agent -t ds-agent .`
  succeeds; `npm run test:smoke` green locally.

### P8 — Verification + PR (sequential, me)
- Full `uv run ruff check .` + `uv run pytest -q` green (≥ baseline 2016).
- `npm run check` + `npm run test:unit` + `npm run test:smoke` green.
- Local uvicorn smoke (curl `/ui/transparency`, `/static/*`, approval GETs).
- Commit; open PR (base `main`); body summarizes scope, verification evidence,
  what still needs human visual review, and that it is NOT deployed/merged.
- Codex `codex-reply` review of finished work vs. design/plan.

## Appendix A — Verified runtime contract (source of truth for lib/*)

See design §4. Endpoints + shapes (verified against `agent/main.py`,
`agent/adk_agent.py`):

- **POST `/chat`** (Accept: text/event-stream): body `{prompt: string,
  workload?: Workload, session_id?: string}`; header `X-DriftScribe-Token` or
  `Cf-Access-Jwt-Assertion`. Frames: `event: meta` `{trace_id}` →
  data-only event frames (`llm_thought`{thought_text} / `tool_call`{tool_name,
  tool_args} / `tool_result`{tool_name, result_preview, result_ok} /
  `llm_usage`{*_token_count} / `final_response`{response_preview, response_kind})
  → `event: done` `{reply, tool_calls[], session_id}` OR `event: error`
  `{detail, status_hint}`. `: keepalive` comments on idle. Response header
  `X-Trace-Id`. Non-SSE Accept → JSON `{reply, tool_calls, session_id}`.
- **GET `/trace/{trace_id}`** (32-hex): `{trace_id, events[], decision|null,
  complete: bool, fetched_from_cache: bool}`; events sorted by (timestamp,
  insert_id), same per-event shape as SSE frames + `timestamp`,`insert_id`.
- **GET `/decisions?limit=1..200`**: `{decisions: [{decision_id, event_key,
  trace_id, action, created_at, ...action fields, approval?}]}` newest first.
- **GET `/runs/{decision_id}`**: one decision object.
- **POST `/recheck?force=`**: body `{workload?}`; → `{trigger, decision_id,
  proposal{...}}`. 503 for chat-only workloads (explore/provision).
- **Auth:** `X-DriftScribe-Token` (401 missing / 403 invalid / 503 unset) OR
  `Cf-Access-Jwt-Assertion` (verified first when CF configured).
- **Approvals (Jinja, form POST — DO NOT convert to fetch):**
  GET `/approvals/{id}?t=` + POST `/approvals/{id}` (hidden `t`, `decision`);
  GET `/iac-approvals/{pr}` + POST `/iac-approvals/{pr}` (hidden `form_token`,
  `decision`; requires Cf-Access JWT; Origin/Sec-Fetch-Site check).

## Appendix B — Structure contract to preserve (Playwright + a11y)

**data-testids (exact):** `chat-prompt`, `chat-submit`, `final-response`,
`past-decisions-pane`, `past-decision-item`, `open-trace-button`,
`historical-banner`; (approval pages, unchanged) `approve-button`,
`reject-button`, `token-field`.

**Element IDs:** `app` (mount), `token-status`, `change-token-btn`,
`decisions-rail`, `decisions-list`, `chat-area`, `chat-form`, `prompt-input`,
`workload-select`, `send-btn`, `trace-badge`, `status-pill`,
`final-response-card`, `group-coordinator` (open by default), `group-tools`,
`group-mcp`, `historical-badge`, `historical-trace-id`, `new-chat-btn`.

**Attributes:** `data-group="coordinator|tools|mcp"` on `.events` divs;
`data-active` on `#historical-badge`; stable per-event key for open-state;
`aria-live="polite"` on `#final-response-card`/`#trace-badge`/`#historical-badge`;
`aria-label` on rail + chat area; `placeholder="Ask the coordinator…"`.

**Behavior:** three groups are real `<details>`; `#group-tools` must expand via
`.open=true` and reveal `[data-group="tools"]`. `sessionStorage['driftscribe_token']`
is read on load (no auto-prompt) and written by AuthPanel. SSE is the live
default; `/trace` poll (2s, stall detection) is the fallback + historical path.

**Workload `<select>` options (value→label):** drift→Cloud Run config,
upgrade→Dependencies, explore→Explore (read-only), provision→Provision (infra edits).

**Worker labels (exact):** Reader (drift), Developer Knowledge MCP, Upgrade
Reader, Upgrade Docs, Open infra PR (keyed on `open_infra_pr_tool`),
`upgrade_read_dependencies_tool`, `upgrade_propose_pr_tool`.

**Approval CTA security:** only render for `propose_rollback_tool` results with a
same-origin `/approvals/` URL via `safeApprovalHref` (URL-parse + origin
compare; reject off-origin/non-http).
