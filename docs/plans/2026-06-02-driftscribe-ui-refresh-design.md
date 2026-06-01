# DriftScribe Operator UI Refresh — Design

**Date:** 2026-06-02
**Branch:** `feat/ui-refresh-svelte`
**Status:** Design — Codex review incorporated (thread 019e83fc; CSP/lockfile/
manifest/`mcp_call`/required-smoke fixes folded into the plan's §2a P0 patches).

> Deferred (Codex suggestion, low value / higher risk): server-side
> normalization of `approval.approval_url` in `GET /decisions`. The URL is
> server-minted from `COORDINATOR_URL` (not user input), and the client
> `safeApprovalHref` same-origin guard is sufficient defense-in-depth, so we do
> not modify the stable `/decisions` endpoint.

## 1. Context & goal

The operator UI today is a single 2181-line `agent/templates/transparency.html`
(inline CSS + ~1500 lines of vanilla JS) plus two server-rendered approval
pages (`approval.html`, `iac_approval.html`). It is functional, accessible, and
testable, but visually it reads like an internal admin tool and undersells the
product. For the hackathon (Google Cloud Japan / Findy, deadline 2026-07-10)
the UI has ~3 minutes to make the multi-agent architecture feel impressive to a
judge seeing it cold.

**Decisions locked in brainstorming (2026-06-02):**

| Axis | Decision |
| --- | --- |
| Audience | **Both, judges first** — demo-stunning but genuinely usable |
| Build model | **Light build step** (Svelte + Vite), not single-file, not full framework SPA platform |
| Headline moment | **"Watch the agent think, live"** + **"the full loop closes"** (prompt → live reasoning → decision → human gate → real infra change) |
| Aesthetic | **Editorial Clarity (light)** — generous whitespace, large type, calm narrative; the live stream animates in, then *settles* into a clean numbered story |
| Scope | **Timeline + approval pages** — approval gate stays on its own signed page (security flow untouched), restyled to match |
| Tooling | **Svelte + Vite**, compiled to a static bundle served by the existing FastAPI route |

**Non-goals (YAGNI):** no auth-system change, no new backend endpoints, no
inline-approve (rejected — would change the HMAC/single-use HITL flow), no
multi-cloud/topology dashboard, no dark mode, no i18n of the UI, no React/Svelte
SSR runtime in the Python container.

## 2. Architecture

**Svelte SPA compiled to a static bundle; FastAPI serves a thin shell. No
change to the deploy topology (still one Python container on Cloud Run).**

```
frontend/                      # NEW Vite + Svelte + TypeScript project
  src/
    lib/                       # pure TS (framework-free, unit-tested with vitest)
      api.ts                   #   token + CF-Access fetch wrapper (try-then-prompt)
      sse.ts                   #   POST /chat SSE frame parser
      timeline.ts              #   event → {coordinator,tools,mcp} group reducer
      labels.ts                #   worker/tool friendly-label map (_WORKER_LABELS)
      approval.ts              #   safeApprovalHref() same-origin guard
      workloads.ts             #   workload list (API contract: drift/upgrade/explore/provision)
      format.ts                #   time / token / preview formatting
    components/                # Svelte components
      App.svelte
      Header.svelte            #   title + TokenStatus + change-token
      TokenStatus.svelte
      DecisionsRail.svelte     #   #decisions-rail / past-decisions-pane
      ChatForm.svelte          #   #chat-form / prompt-input / workload-select / send-btn
      TraceBadge.svelte        #   #trace-badge (trace pill + status pill)
      FinalResponse.svelte     #   #final-response-card / final-response
      Timeline.svelte          #   the three <details> groups
      Group.svelte             #   <details id="group-*"> + <div data-group="*">
      EventRow.svelte
      ApprovalCta.svelte       #   inline HITL CTA (same-origin guarded)
      HistoricalBanner.svelte  #   #historical-badge / historical-banner
      AuthPanel.svelte         #   replaces window.prompt() for token entry
    styles/
      tokens.css               #   design system: color, type scale, spacing, motion
      base.css                 #   shared element/utility styles (also used by approval pages)
    main.ts                    # mounts <App> into #app
  index.html                   # Vite entry (dev only)
  vite.config.ts               # build → ../agent/static, manifest: true
  svelte.config.js
  tsconfig.json
  package.json                 # scripts: dev, build, check, test:unit, test:smoke
  vitest.config.ts
  tests/unit/                  # vitest for lib/*
  tests/smoke/                 # mock-Playwright DOM/flow smoke (local + CI)

agent/static/                  # BUILD OUTPUT (gitignored); created by `vite build`
  transparency-[hash].js
  driftscribe-[hash].css
  .vite/manifest.json
```

**Serving (FastAPI):**
- Mount `StaticFiles(directory="agent/static", check_dir=False)` at `/static`
  (`check_dir=False` so the pure-Python CI job — which never runs `vite build` —
  still imports the app cleanly).
- `GET /ui/transparency` renders a thin Jinja shell (`transparency.html`,
  rewritten) containing `<div id="app"></div>` + the hashed `<script
  type="module">` / `<link rel="stylesheet">` resolved from the Vite
  `manifest.json`. A small `_vite_asset(name)` helper reads the manifest once at
  import; if the manifest is absent (pure-Python CI / dev before build), it
  falls back to conventional `/static/transparency.js` + `/static/driftscribe.css`
  so the **shell HTML still returns 200** with `id="app"` and `DriftScribe`
  present. Keeps `Cache-Control: no-store`, no token required (unchanged).
- `GET /ui/transparency-legacy` serves the current `transparency.html`
  (renamed `transparency_legacy.html`) verbatim — a one-release safety net for
  the demo window.

**Approval pages stay Jinja, restyled.** `approval.html` and
`iac_approval.html` remain server-rendered real `<form method="post">` pages
with their injected `t` / `form_token` and the Origin/Sec-Fetch-Site check —
**zero change to the security flow**. They `<link>` the same built
`driftscribe-[hash].css` and adopt the new design-system class names, so the
loop is visually cohesive when the operator clicks Approve. (They get the same
manifest-resolved CSS href via the shared helper.)

## 3. Design system (Editorial Clarity)

- **Color:** warm-neutral light background (`#fcfcfb`), ink text (`#1a1a18`),
  muted (`#6b6b66`), hairline borders (`#e7e6e1`); semantic accent green
  (decision/ok), amber (gate/pending), red (error/danger), blue (streaming/MCP).
  Defined as CSS custom properties in `tokens.css`.
- **Type:** a real scale (e.g. 13/15/18/24/32px) with a humanist UI font stack;
  monospace (`ui-monospace`) reserved for trace IDs, tokens, and code/diff.
- **Spacing:** 4px base unit, generous vertical rhythm; max content width for
  the narrative column.
- **Motion:** Svelte `transition:`/`animate:flip` for the headline effect —
  reasoning events **fly/fade in** as they stream, then the list **reflows**
  (flip) into the settled, numbered narrative when the `done` frame lands.
  Respect `prefers-reduced-motion` (disable transitions).
- The same tokens/components style the approval pages for one coherent language.

## 4. Data flow

All shapes per the verified runtime contract (see
`2026-06-02-driftscribe-ui-refresh-plan.md` appendix).

1. **Live chat (SSE):** `POST /chat` with `Accept: text/event-stream`, body
   `{prompt, workload, session_id?}`. Parse frames: `meta` (trace_id) →
   `llm_thought`/`tool_call`/`tool_result`/`llm_usage`/`final_response` →
   `done` (reply, tool_calls, session_id) or `error` (detail, status_hint).
   Render the final reply from the `done` frame immediately; the timeline is
   already populated live. **MCP note:** `mcp_call` events are NOT carried on the
   stream — after `done`, the app does ONE `/trace/{id}` **backfill** to pull the
   side-channel `mcp_call` events (sub-grouped by `mcp_tool || mcp_server`) and
   reconcile ordering (mirrors the legacy UI). MCP routing is by `event ===
   'mcp_call'`, not by tool-name prefix.
2. **Historical replay:** opening a past decision fetches `GET /trace/{id}` and
   renders its `events[]` (sorted by timestamp, insert_id) read-only, with the
   historical banner shown and the chat form dimmed (`.historical`).
3. **Decisions rail:** `GET /decisions?limit=50` populates the left rail
   (newest first); each row with a `trace_id` gets an `open-trace-button`; each
   with an `approval.approval_url` gets a same-origin-guarded `Approve →` (with
   expired strikethrough + badge when `expires_at` is past).
4. **Auth:** try-then-prompt. Send the request with the `X-DriftScribe-Token`
   from `sessionStorage['driftscribe_token']` if present; on 401/403 clear it
   and show the **AuthPanel** (inline, replacing `window.prompt()`); CF-Access
   users (Cf-Access-Jwt-Assertion auto-injected) succeed with no token and never
   see the panel.
5. **State persistence:** per-event `<details>` open/closed state is preserved
   across re-renders via stable keys (the `insert_id`), matching today's
   `data-insert-id` behavior.

The historical/poll fallback (`/trace` on a 2s cadence with stall detection) is
retained for the JSON+poll path and for historical replay; SSE is the live
default.

## 5. Test strategy — preserve the contract, re-home the guards

The Playwright e2e suite (`tests/e2e/ui/transparency.spec.ts`) is the real UI
contract and is **transport-agnostic**; it depends only on:
- 7 data-testids: `chat-prompt`, `chat-submit`, `final-response`,
  `past-decisions-pane`, `past-decision-item`, `open-trace-button`,
  `historical-banner`;
- three real `<details id="group-coordinator|tools|mcp">` whose child
  `[data-group="..."]` becomes visible when the details is opened
  (Test 1 sets `.open = true` on `#group-tools`);
- `sessionStorage['driftscribe_token']`;
- workload `<select>` with values drift/upgrade/explore/provision.

**The Svelte components MUST preserve all of the above verbatim** (the three
groups stay real `<details>` elements). The spec and `playwright.config.ts` are
**not** changed → `test_playwright_config.py` stays green.

Several Python tests grep the single-file HTML and break under the SPA model.
They are migrated **intent-for-intent**, never silently weakened:

| Existing Python guard | New home |
| --- | --- |
| `test_ui_transparency.py` — shell route 200 / html / no-store / no token / "DriftScribe" | **Kept** (Python), updated to assert the shell + `id="app"` + bundle reference |
| `test_ui_transparency.py` — three-group renderer, worker labels, workload labels | **vitest** `labels.test.ts` + smoke (DOM has `group-*`, `data-group`, friendly labels) |
| `test_ui_transparency.py` — approval-cta same-origin guard (`_safeApprovalHref`, `new URL`, `window.location.origin`) | **vitest** `approval.test.ts` (security guard, asserted on the pure fn) |
| `test_ui_transparency.py` — workload option VALUES (API contract) | **vitest** `workloads.test.ts` |
| `test_ui_transparency.py` — token helpers, poll/SSE logic | **vitest** `api.test.ts` + `sse.test.ts` + smoke |
| `test_transparency_template_testids.py` — testids present | **rewritten** to grep `frontend/src/**` for each testid (source-level guard) |
| `test_transparency_template_testids.py` — `data-group` present | grep `frontend/src/**` |
| `test_transparency_template_testids.py` — sessionStorage key | grep `frontend/src/**` |
| `test_transparency_template_testids.py` — `approval.html` testids | **unchanged** (approval.html stays Jinja) |

New frontend tests (run in CI's new `frontend` job): **vitest unit** for every
`lib/*` module (api, sse, timeline reducer, labels, approval guard, workloads,
format) + a **mock-Playwright smoke** (`frontend/tests/smoke`) that boots the
real FastAPI app via uvicorn, mocks `/chat`(SSE)/`/decisions`/`/trace` with
`page.route`, and asserts all 7 testids, the three-group open behavior, the
historical-mode flow, and the auth-panel path — i.e. a local stand-in for the
cloud e2e, runnable without a deployed coordinator. I verify it green locally
before opening the PR.

## 6. Build & deploy integration

- **`frontend/vite.config.ts`:** `build.outDir = '../agent/static'`,
  `build.emptyOutDir = true`, `build.manifest = true`, a single entry
  (`src/main.ts`) producing `transparency-[hash].js` + `driftscribe-[hash].css`.
- **`Dockerfile.agent`:** add a `node:24-slim` builder stage that runs
  `npm ci && npm run build` in `frontend/` and emits `agent/static/`; the Python
  stage `COPY --from=builder /build/agent/static ./agent/static`. Order the
  COPYs so the build output is authoritative.
- **`.dockerignore` / `.gitignore`:** ignore `frontend/node_modules`,
  `frontend/dist`, and `agent/static/` (build output is never committed; built
  in Docker/CI and locally for the smoke).
- **`.github/workflows/ci.yml`:** add a `frontend` job (setup-node 24 →
  `npm ci` → `npm run build` → `npm run check` (svelte-check) → `npm run
  test:unit`) and a `ui-smoke` job (Python + Node → build → uvicorn → mock
  Playwright). The existing `lint-test` (ruff + pytest) job is unchanged and
  still passes because the shell route returns 200 without built assets.
- **Cloud Build:** `infra/cloudbuild.coordinator-update.yaml` already builds
  `Dockerfile.agent`; the new builder stage means a deploy now also builds the
  frontend — no config change needed beyond the Dockerfile.
- **`Makefile`:** `make ui` (build), `make ui-dev` (vite dev), `make ui-smoke`.

## 7. Rollout & safety

- All work on `feat/ui-refresh-svelte`; opened as a PR. **Not deployed, not
  merged** — left for operator visual review.
- Legacy UI preserved at `/ui/transparency-legacy` for one release.
- Reversible: the new UI is additive; reverting the shell route + Dockerfile
  stage restores the old single-file page.
- Security flows (token auth, CF-Access, approval CSRF/HMAC, same-origin CTA
  guard) are preserved and guarded by re-homed tests.

## 8. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Cloud Playwright e2e only runs on dispatch post-deploy; SPA bug slips through | Local mock-Playwright smoke replicates all 7 testids + 3 flows; run green before PR |
| `#group-tools` must respond to `.open=true` | Groups are real `<details>` elements (not custom components) |
| Hashed asset names vs. shell template | Vite manifest + `_vite_asset()` helper with dev fallback |
| Node build breaks Docker/CI | Multi-stage isolated; pure-Python `lint-test` job unaffected; build proven locally first |
| Re-homed security guards weaker than originals | Explicit old→new mapping table; each guard re-implemented on the pure fn and asserted in CI |
| Bundle bloat | Svelte compiles away; target < 60 KB gz; no heavy deps |

## 9. Acceptance criteria

1. `npm run build` produces `agent/static/` bundle; `npm run check` clean.
2. `npm run test:unit` (vitest) green; covers api/sse/timeline/labels/approval/workloads/format.
3. `npm run test:smoke` (mock Playwright) green locally: 7 testids, 3-group open, historical mode, auth panel.
4. `uv run pytest -q` green (≥ 2016 tests; migrated tests pass with preserved intent).
5. `uv run ruff check .` clean.
6. Local uvicorn smoke: `GET /ui/transparency` 200 with bundle; `/static/*` served; approval pages render restyled with form POST intact.
7. Docker image builds (frontend stage + python stage).
8. PR opened; Codex review of finished work vs. this design passes.
