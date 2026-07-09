# Trace event-kind filter + iac_apply "view details" label

One PR, two complementary fixes for the "view reasoning shows three empty cards" bug
and the over-promising label on directly-recorded decisions.

## Problem

1. **`/trace` returns non-event log lines.** `CloudLoggingFetcher._query`
   (agent/trace_fetcher.py:131-135) filters on `jsonPayload.trace_id` only. Every log
   line emitted inside the request context inherits the trace id via the
   `driftscribe_lib.logging` ContextVar (main.py:247), so plumbing logs (httpx CF-Access
   JWKS fetch, PyGithub retry/comment warnings) ride along. The repo's own docs
   (docs/runbooks/deploy.md:113, docs/architecture/multi-agent-design.md:353) document
   an `jsonPayload.event=(...)` clause that the implementation dropped.
   - Live evidence: traces `2738727f…` (PR #216) and `eba334f9…` (PR #221) return 3
     kind-less log lines each; PR #32's trace returns 0.
   - Frontend consequence: `Timeline.svelte:43` gates the empty-state note on raw
     `events.length === 0`, so 3 junk lines suppress the honest "recorded directly"
     note and render three empty group accordions instead (`groupOf` drops all junk
     to null).
   - Backend consequence: junk lines perturb the event-count stability watch in
     `_observe_and_check_stability`.

2. **"view reasoning →" over-promises on `iac_apply` rows.** Those decisions are
   recorded directly by the approval handler — there is never a reasoning run. The
   click delivers decision fields, env-diff, PR body, apply status. Label should say
   so. Owner ruling: **"view details →"** for `iac_apply`, everything else keeps
   "view reasoning →".

## Changes

### Backend — agent/trace_fetcher.py

- Add to the filter in `_query`:
  `AND jsonPayload.event=("llm_thought" OR "tool_call" OR "tool_result" OR "llm_usage" OR "mcp_call" OR "final_response")`
- The allowlist is **all six** kinds the pipeline emits (verified by grep over
  agent/ + driftscribe_lib/). NOT the four in the docs (missing `tool_result` →
  result_preview pairing breaks; missing `final_response` →
  `_observe_and_check_stability` can never see completion). NOT a bare existence
  operator (`:*`) — an explicit allowlist keeps future stray event-bearing logs out.
- Keep the kinds in a module-level tuple (e.g. `_EVENT_KINDS`) with a comment stating
  why all six are load-bearing; build the clause from it.
- Mirror the same kind filter in `StubTraceFetcher.fetch` (agent/trace_fetcher.py:205)
  so dev/dry-run parity holds; update the kind-less `test_stub_respects_limit` fixture
  (tests/unit/test_trace_fetcher.py:51) so it still exercises the limit after filtering.

### Frontend — Timeline.svelte

- `historicalEmpty` gates on zero *displayable* events, not raw count:
  `status === 'historical' && groups.coordinator.length === 0 && groups.tools.length === 0 && groups.mcp.length === 0`
  (reuses the already-derived `groups`; semantics match the App.svelte:438 autoload
  gate). Update the adjacent comment; the note/suppression still share one condition.

### Frontend — DecisionsRail.svelte (+ lib/rail.ts)

- New pure helper in `lib/rail.ts`:
  `traceButtonLabel(action) => action === 'iac_apply' ? 'view details →' : 'view reasoning →'`
- Use it at both button sites: decision rows (:241) and lifecycle steps (:300).
  Lifecycle steps are always `iac_apply` (rail.ts `groupablePr`), so they all read
  "view details →"; the shared helper keeps the predicate identical to the
  empty-copy gate (`App.svelte:903`).
- `ConversationThread.svelte` unchanged (chat turns are always reasoning-backed).
- Keep `data-testid`s, CSS class names (`open-trace-btn`), `/trace` endpoint,
  internals unchanged.

### Docs

- Align docs/runbooks/deploy.md:113 and docs/architecture/multi-agent-design.md:353
  with the six-kind clause so the docs and implementation agree again.

## Tests

- **Backend** (tests/unit/test_trace_fetcher.py): update the filter-string snapshot;
  add an assertion that the event clause lists exactly the six kinds; add a
  StubFetcher case where a kind-less entry with a matching trace_id is excluded.
- **Frontend** (vitest):
  - Timeline: historical status + only unknown-kind events → empty-state note
    renders (both `directlyRecorded` copy variants), group accordions suppressed;
    historical + a real displayable event → groups render as today.
  - rail.ts: `traceButtonLabel` unit cases (iac_apply / rollback / recheck / null).
  - DecisionsRail: iac_apply row + lifecycle steps show "view details →"; a
    rollback single row shows "view reasoning →". Fix any test asserting the old
    verbatim label.

## Out of scope (separate follow-up)

- PAT 403 on PR comments (github.py:1134, also :777 and :491 sites) — post-merge PR
  commenting is silently dead in prod; needs a token-scope fix or removal, not part
  of this PR.

## Verification & rollout

- pytest (backend), vitest + build (frontend).
- One PR → CI green → Codex review → merge → coordinator-only deploy (trace_fetcher
  lives in agent/, SPA is served by the same Cloud Run service; driftscribe_lib
  untouched → no worker redeploy).
- Live-verify on run.app: `/trace/2738727f…` returns 0 events and the SPA shows the
  "recorded directly" note + "view details →" for PR #216/#221; a fresh ephemeral
  explore turn still yields a full timeline (thoughts, tool call+result pairs, MCP
  rows) and `complete` eventually flips true (proves `final_response` survived the
  filter).
