# Real-time Transparency Timeline Streaming — Design

**Date:** 2026-05-26
**Status:** Approved (brainstorm + Codex review complete)

## Problem

The transparency timeline (tool_call / tool_result / llm_thought / llm_usage)
currently reaches the UI only via Cloud Logging: the agent emits structured
logs during the `/chat` request, and the UI polls `GET /trace/{trace_id}`
every 2s. The latency floor is **Cloud Logging ingestion lag** (seconds), not
poll frequency — so faster polling can't fix it. We want the timeline to feel
near-real-time.

Scope (confirmed with operator):
- **In scope:** live tool_call / tool_result / llm_thought / llm_usage / status.
- **Out of scope:** token-streaming the final reply (it may land all-at-once at
  the end of the turn).
- **mcp_call:** backfilled via one `/trace` fetch *after* the stream ends — not
  streamed live (side-channel log, only used by upgrade/explore workloads).

## Key facts (verified in code)

- `/chat` is a FastAPI `async def` (`agent/main.py:1896`) that runs the ADK
  agent loop **inline** via `run_chat` (`agent/adk_agent.py:711`). All timeline
  events are produced by the agent process inside that loop
  (`_emit_event_logs` / `_emit_llm_usage`, `adk_agent.py:434-549`). Workers do
  not emit timeline events. So the agent already owns the full live stream
  in-process.
- Today those events go **only** to Cloud Logging; `/trace`
  (`agent/trace_fetcher.py`) reads them back.
- `mcp_call` is rendered in the timeline (`transparency.html:1280-1287`) but
  logged from a side-channel (`agent/mcp/developer_knowledge.py:406`), not as an
  ADK event.
- Deployment: Cloud Run `--concurrency=1 --max-instances=1 --min-instances=0`,
  fronted by Cloudflare Access (`infra/cloudbuild.yaml`).
- The trace-id middleware resets the `trace_id` ContextVar in a `finally` right
  after `call_next` returns (`driftscribe_lib/logging.py:285`); `/chat` resets
  `workload` in its own `finally` (`main.py:1997`).

## Chosen approach

**Approach A — stream events directly off the inline `/chat` request via SSE,
additive to (never replacing) Cloud Logging emission.** The same redacted event
dict is both logged and yielded, so the live view is never less-redacted than
the durable log.

### Transport

- Content negotiation on `/chat`: `Accept: text/event-stream` → `StreamingResponse`
  (`text/event-stream`); otherwise the existing JSON dict (tests, `/recheck`,
  API callers untouched).
- Client uses `fetch()` + `ReadableStream` (POST + body + `X-DriftScribe-Token`
  header — which `EventSource` cannot do).

### Frames

- First: `event: meta` → `{trace_id}` (so the client can fall back to polling).
- Timeline: default `data: <redacted event-dict + seq/insert_id/timestamp>` —
  same shape `/trace` returns, so `renderTimeline` is reused.
- Terminal: `event: done` → `{reply, tool_calls, session_id}` **after** the loop
  and the existing empty-reply check — or `event: error` → `{detail, status_hint}`.
- Heartbeat: `: keepalive` comment every ~15s.

### Four Codex-driven corrections

1. **ContextVar re-binding inside the generator.** `/chat` captures
   `trace_id = get_trace_id()` and `workload` *before* returning the
   `StreamingResponse`; the stream generator re-binds both (`set_trace_id` /
   `set_workload`) on entry and resets in `finally`. Without this the body
   iterator runs *after* the middleware/handler `finally` blocks have already
   reset the ContextVars — corrupting both the stream and the durable logs with
   a fresh, uncorrelated trace_id. Disconnect surfaces as
   `asyncio.CancelledError` (covered by the same `finally`).
2. **Producer/consumer queue for heartbeats.** A generator parked in
   `async for event in runner.run_async(...)` cannot emit keepalives during a
   long tool await. A producer task drains the core `run_chat_stream` generator
   into an `asyncio.Queue`; the SSE consumer does
   `await asyncio.wait_for(queue.get(), 15)` and emits a heartbeat on timeout.
   On disconnect it cancels the producer and resets ContextVars. The JSON path
   drains the core generator directly (no queue).
3. **Exact ordering preserved.** Per-event yield order = current log order:
   timeline events → `final_response` (inside the final-event branch) →
   `llm_usage`. `done` is emitted after the loop, after the empty-reply
   `RuntimeError` check. The multi-final-response edge case is preserved.
4. **Synthetic `seq` + `insert_id` + `timestamp`** on streamed events, since
   `renderTimeline` keys expansion state on `insert_id` and shows timestamps
   (Cloud Logging supplies these for `/trace`; SSE must synthesize them).

### Error handling

Pre-flight checks stay before streaming begins → real HTTP status codes for
both paths. In-loop failures (status already 200) become an `event: error`
frame carrying a `status_hint`. The existing exception→code mapping
(`WorkerClientError`→502, `MissingDeveloperKnowledgeApiKeyError`→503,
`RuntimeError`→502) is factored into a shared helper used for both the
pre-flight `HTTPException` and the in-stream `status_hint`.

### Infra

- `--timeout=300` (Cloud Run total-request cap still applies; heartbeats only
  save Cloudflare's ~120s read-idle timeout, not the total).
- `--concurrency=2` so a fallback `/trace`, the post-`done` `reloadDecisions()`
  `/decisions` GET, or a second tab does not deadlock behind the live stream.

### UI

- On submit with streaming: parse SSE frames, push events into the same array
  `renderTimeline` consumes (using server `seq`/`insert_id`), re-render
  incrementally. No `/trace` polling while the stream is live.
- On `done`: `showFinalResponse`, `reloadDecisions()`, one backfill `/trace`
  fetch (reconciles + pulls `mcp_call`).
- On stream error/disconnect: fall back to `pollTrace(trace_id)`.

## Risks accepted

- Cloud Run total request timeout still caps very long runs.
- `concurrency=2` is the floor that avoids self-deadlock.
- `mcp_call` is end-of-run latency by operator choice.
