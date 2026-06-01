<script lang="ts">
  import { onMount } from 'svelte';
  import {
    apiFetch,
    getStoredToken,
    setToken,
    clearToken,
    type TokenState,
  } from './lib/api';
  import { consumeSse } from './lib/sse';
  import type { TraceEvent, TimelineStatus } from './lib/timeline';
  import type { Decision, TraceResponse } from './lib/types';
  import type { Workload } from './lib/workloads';

  import TokenStatus from './components/TokenStatus.svelte';
  import AuthPanel from './components/AuthPanel.svelte';
  import ChatForm from './components/ChatForm.svelte';
  import TraceBadge from './components/TraceBadge.svelte';
  import FinalResponse from './components/FinalResponse.svelte';
  import HistoricalBanner from './components/HistoricalBanner.svelte';
  import DecisionsRail from './components/DecisionsRail.svelte';
  import Timeline from './components/Timeline.svelte';

  // ---- state ----
  let tokenState = $state<TokenState>(getStoredToken() ? 'ok' : 'missing');
  let events = $state<TraceEvent[]>([]);
  let traceId = $state<string | null>(null);
  let status = $state<TimelineStatus>('pending');
  let finalReply = $state<string | null>(null);
  let finalIsError = $state(false);

  let decisions = $state<Decision[]>([]);

  let historicalActive = $state(false);
  let historicalTraceId = $state<string | null>(null);
  let activeTraceId = $state<string | null>(null);

  let authPanelOpen = $state(false);
  let authResolver: ((t: string | null) => void) | null = null;

  // ---- auth plumbing (replaces window.prompt) ----
  function requestToken(): Promise<string | null> {
    authPanelOpen = true;
    return new Promise((resolve) => {
      authResolver = resolve;
    });
  }
  function onAuthSubmit(token: string) {
    authPanelOpen = false;
    setToken(token);
    tokenState = 'ok';
    const r = authResolver;
    authResolver = null;
    r?.(token);
  }
  function onAuthCancel() {
    authPanelOpen = false;
    tokenState = getStoredToken() ? 'ok' : 'missing';
    const r = authResolver;
    authResolver = null;
    r?.(null);
  }
  function onChangeToken() {
    clearToken();
    tokenState = 'missing';
    void requestToken();
  }

  // ---- request wrapper that keeps the token pill honest ----
  async function call(path: string, init?: RequestInit): Promise<Response> {
    const resp = await apiFetch(path, init, requestToken);
    if (resp.ok) {
      if (getStoredToken()) tokenState = 'ok';
    } else if (resp.status === 401 || resp.status === 403) {
      tokenState = getStoredToken() ? 'invalid' : 'missing';
    }
    return resp;
  }

  // ---- decisions rail ----
  async function loadDecisions() {
    try {
      const resp = await call('/decisions?limit=50');
      if (!resp.ok) return;
      const body = await resp.json();
      if (Array.isArray(body?.decisions)) decisions = body.decisions as Decision[];
    } catch {
      /* best-effort */
    }
  }

  // ---- live chat (SSE) ----
  async function submitChat(prompt: string, workload: Workload) {
    if (historicalActive) return;
    events = [];
    traceId = null;
    finalReply = null;
    finalIsError = false;
    status = 'pending';

    let resp: Response;
    try {
      resp = await call('/chat', {
        method: 'POST',
        headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, workload }),
      });
    } catch {
      status = 'error';
      finalReply = 'Network error contacting the coordinator.';
      finalIsError = true;
      return;
    }

    if (!resp.ok) {
      status = 'error';
      finalReply = `Request failed (${resp.status}).`;
      finalIsError = true;
      return;
    }

    const ctype = resp.headers.get('content-type') ?? '';
    if (!ctype.includes('text/event-stream')) {
      // Fallback: non-streaming JSON {reply, tool_calls}. The backfill below
      // still pulls the full timeline (incl. mcp_call) from /trace.
      try {
        const body = await resp.json();
        traceId = resp.headers.get('X-Trace-Id');
        finalReply = typeof body?.reply === 'string' ? body.reply : JSON.stringify(body);
        status = 'complete';
      } catch {
        status = 'error';
        finalReply = 'Malformed response.';
        finalIsError = true;
      }
      await backfillTrace();
      await loadDecisions();
      return;
    }

    await consumeSse(resp, {
      onMeta: (m) => {
        traceId = m.trace_id;
        status = 'streaming';
      },
      onEvent: (e) => {
        events = [...events, e as unknown as TraceEvent];
      },
      onDone: (d) => {
        finalReply = d.reply;
        finalIsError = false;
        status = 'complete';
      },
      onError: (er) => {
        finalReply = er.detail || 'The coordinator returned an error.';
        finalIsError = true;
        status = 'error';
      },
    });

    // One post-`done` backfill: pulls side-channel mcp_call events not carried
    // on the stream + reconciles ordering (mirrors the legacy UI).
    await backfillTrace();
    await loadDecisions();
  }

  async function backfillTrace() {
    if (!traceId) return;
    try {
      const resp = await call('/trace/' + encodeURIComponent(traceId));
      if (!resp.ok) return;
      const t = (await resp.json()) as TraceResponse;
      if (Array.isArray(t.events) && t.events.length > 0) {
        events = t.events;
      }
    } catch {
      /* backfill is best-effort — the live stream already populated the timeline */
    }
  }

  // ---- historical replay ----
  async function openTrace(tid: string) {
    historicalActive = true;
    historicalTraceId = tid;
    activeTraceId = tid;
    traceId = tid;
    events = [];
    finalReply = null;
    finalIsError = false;
    status = 'pending';
    try {
      const resp = await call('/trace/' + encodeURIComponent(tid));
      if (resp.ok) {
        const t = (await resp.json()) as TraceResponse;
        events = Array.isArray(t.events) ? t.events : [];
        status = t.complete ? 'complete' : 'streaming';
      } else {
        status = 'error';
      }
    } catch {
      status = 'error';
    }
  }

  function newChat() {
    historicalActive = false;
    historicalTraceId = null;
    activeTraceId = null;
    traceId = null;
    events = [];
    finalReply = null;
    finalIsError = false;
    status = 'pending';
  }

  onMount(() => {
    void loadDecisions();
  });
</script>

<header class="app-header">
  <h1 class="app-title">DriftScribe <span class="app-title__sub">— Reasoning Timeline</span></h1>
  <TokenStatus state={tokenState} onChange={onChangeToken} />
</header>

<main class="layout">
  <DecisionsRail {decisions} {activeTraceId} onOpenTrace={openTrace} />

  <section id="chat-area" class="chat-area" aria-label="Chat and reasoning timeline">
    <ChatForm disabled={historicalActive} onSubmit={submitChat} />
    <HistoricalBanner active={historicalActive} traceId={historicalTraceId} onNewChat={newChat} />
    <TraceBadge {traceId} {status} />
    <FinalResponse reply={finalReply} isError={finalIsError} />
    <Timeline {events} {status} />
  </section>
</main>

<AuthPanel open={authPanelOpen} onSubmit={onAuthSubmit} onCancel={onAuthCancel} />

<style>
  .app-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-4);
    padding: var(--ds-sp-3) var(--ds-sp-6);
    border-bottom: 1px solid var(--ds-border);
    background: var(--ds-surface);
  }
  .app-title {
    font-size: var(--ds-fs-3);
    font-weight: var(--ds-fw-bold);
    letter-spacing: -0.01em;
    margin: 0;
  }
  .app-title__sub {
    color: var(--ds-muted);
    font-weight: var(--ds-fw-normal);
  }
  .layout {
    display: grid;
    grid-template-columns: 280px minmax(0, 1fr);
    align-items: start;
    min-height: calc(100vh - 56px);
  }
  .chat-area {
    padding: var(--ds-sp-5) var(--ds-sp-6) var(--ds-sp-8);
    max-width: var(--ds-page-max);
  }
  .chat-area > :global(*) {
    margin-bottom: var(--ds-sp-4);
  }
  @media (max-width: 760px) {
    .layout {
      grid-template-columns: 1fr;
    }
  }
</style>
