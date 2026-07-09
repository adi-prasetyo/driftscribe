// App-level wiring for multi-turn conversations (P2): resuming a thread from
// the rail, and a chat turn settling into the thread once the coordinator
// echoes a conversation_id. The SSE transport is covered by the smoke; here we
// drive the JSON fallback path, which runs the same settle logic.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';

function okJson(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

// A /chat Response the App will treat as an SSE stream: content-type
// text/event-stream + a ReadableStream body of `event:`/`data:` frames.
// Modeled on sseResponse in sse.test.ts:16-28, but sets the content-type
// header so App.svelte's stream-branch check (ctype.includes(...)) takes it.
function sseChatResponse(frames: string, headers: Record<string, string> = {}): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode(frames));
      c.close();
    },
  });
  return new Response(stream as unknown as BodyInit, {
    headers: { 'content-type': 'text/event-stream', ...headers },
  });
}

const GRAPH = {
  generated_at: null,
  project: 'demo-proj',
  caveat: '',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 0, drift: 1 },
  groups: [],
  edges: [],
};

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  // A token keeps `call` from opening the auth panel mid-test.
  window.sessionStorage.setItem('driftscribe_token', 'tok');
  // Mark the tour done so its banner doesn't intercept the view.
  window.localStorage.setItem('driftscribe_tour_done', '1');
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  // openTrace scrolls the window to top; jsdom doesn't implement scrollTo.
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  history.replaceState(null, '', '/');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — resume a conversation from the rail', () => {
  it('opens the thread, renders its turns, and snaps the composer to its crew', async () => {
    const list = {
      conversations: [
        {
          conversation_id: 'c1',
          workload: 'explore',
          title: 'prior chat about drift',
          updated_at: new Date().toISOString(),
          turn_count: 2,
        },
      ],
    };
    const detail = {
      conversation_id: 'c1',
      workload: 'explore',
      title: 'prior chat about drift',
      turns: [
        { seq: 0, role: 'user', text: 'what changed?', workload: 'explore' },
        { seq: 1, role: 'crew', text: 'the env var EXTRA drifted', workload: 'explore', trace_id: 't1' },
      ],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) return okJson(detail);
        if (url.includes('/conversations')) return okJson(list);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, container } = render(App);

    // The rail lists the prior conversation; open it.
    await fireEvent.click(await findByTestId('conversation-open'));

    // The thread rehydrates with both turns.
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the env var EXTRA drifted')).toBeTruthy());
    expect(getByText('what changed?')).toBeTruthy();

    // The composer snapped to the thread's locked crew (explore).
    await waitFor(() => {
      const checked = container.querySelector('input[type="radio"]:checked') as HTMLInputElement;
      expect(checked?.value).toBe('explore');
    });
  });

  it("auto-loads the latest crew turn's reasoning into the inline timeline (thread stays visible, NOT full replay)", async () => {
    const list = {
      conversations: [
        {
          conversation_id: 'c1',
          workload: 'drift',
          title: 'why did EXTRA drift?',
          updated_at: new Date().toISOString(),
          turn_count: 2,
          last_trace_id: 't1',
        },
      ],
    };
    const detail = {
      conversation_id: 'c1',
      workload: 'drift',
      title: 'why did EXTRA drift?',
      last_trace_id: 't1',
      turns: [
        { seq: 0, role: 'user', text: 'why did EXTRA drift?', workload: 'drift', trace_id: 't1' },
        { seq: 1, role: 'crew', text: 'someone set it in the console', workload: 'drift', trace_id: 't1' },
      ],
    };
    // The latest turn's trace carries real reasoning: a coordinator thought, a
    // paired tool call, and an MCP call — one displayable event per group.
    const trace = {
      trace_id: 't1',
      complete: true,
      events: [
        { event: 'llm_thought', trace_id: 't1', thought_text: 'weighing the region tradeoff', timestamp: '2026-07-02T00:00:00Z' },
        { event: 'tool_call', trace_id: 't1', tool_name: 'load_iac_plan_tool', tool_args: {}, timestamp: '2026-07-02T00:00:01Z' },
        { event: 'tool_result', trace_id: 't1', tool_name: 'load_iac_plan_tool', result_ok: true, result_preview: 'ok', timestamp: '2026-07-02T00:00:02Z' },
        { event: 'mcp_call', trace_id: 't1', mcp_tool: 'search_docs', mcp_server: 'ctx7', latency_ms: 12, timestamp: '2026-07-02T00:00:03Z' },
      ],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/')) return okJson(detail);
        if (url.includes('/conversations')) return okJson(list);
        if (url.includes('/trace/')) return okJson(trace);
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, queryByTestId } = render(App);

    await fireEvent.click(await findByTestId('conversation-open'));

    // The thread rehydrated AND the latest turn's reasoning shows inline: the
    // coordinator thought text only renders when the timeline is populated.
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('weighing the region tradeoff')).toBeTruthy());

    // We are in inline mode, NOT full-page historical replay: the thread and the
    // reasoning coexist, and the "viewing past reasoning" banner is absent.
    expect(queryByTestId('conversation-thread')).not.toBeNull();
    expect(queryByTestId('historical-banner')).toBeNull();
    // The crew reply bubble is still shown above the reasoning.
    expect(getByText('someone set it in the console')).toBeTruthy();
  });
});

describe('App — a chat turn settles into the thread', () => {
  it('appends the exchange and clears the standalone hero when conversation_id is echoed', async () => {
    let listCalls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          return okJson(
            { reply: 'here is the answer', tool_calls: [], conversation_id: 'new-conv' },
            { 'X-Trace-Id': 'trace-xyz' },
          );
        }
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'new-conv', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) {
          listCalls += 1;
          return okJson({ conversations: [] });
        }
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-xyz', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // The exchange folds into the thread (user prompt + crew reply bubbles).
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('here is the answer')).toBeTruthy());
    expect(getByText('why did it drift?')).toBeTruthy();

    // The standalone hero is cleared (the reply now lives in the thread).
    await waitFor(() => {
      const hero = queryByTestId('final-response');
      expect(hero?.hasAttribute('hidden')).toBe(true);
    });

    // The rail was refreshed after the turn (mount + post-settle).
    expect(listCalls).toBeGreaterThanOrEqual(2);
  });

  it('does NOT settle a paused refusal that echoes conversation_id (no turn persisted)', async () => {
    // The kill-switch reply carries conversation_id for crew-lock symmetry but
    // persists nothing; settling it would append a bubble that vanishes on
    // reload. The reply must stay in the standalone hero, with no thread.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') {
          return okJson({
            reply: 'DriftScribe is paused (operator kill switch active).',
            tool_calls: [],
            paused: true,
            conversation_id: 'echoed-but-not-persisted',
          });
        }
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: 't', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // The paused reply stays in the hero; no thread is created.
    await waitFor(() => {
      const hero = getByTestId('final-response');
      expect(hero.hasAttribute('hidden')).toBe(false);
      expect(hero.textContent).toContain('paused');
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
  });

  it('shows an optimistic thinking bubble while the reply is in flight, then settles it in place', async () => {
    // Hold /chat open so the in-flight state (prompt bubble + "thinking" crew
    // bubble, hero suppressed) is observable, then release the reply.
    let releaseChat!: (r: Response) => void;
    const chatPromise = new Promise<Response>((res) => {
      releaseChat = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return chatPromise;
        if (url.includes('/conversations/')) return okJson({ conversation_id: 'new-conv', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: 'trace-xyz', events: [], complete: true });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText, queryByTestId } = render(App);

    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // In flight: the exchange renders through the thread — the operator's prompt
    // bubble plus a live "thinking" crew bubble — and the standalone hero is
    // suppressed entirely (the reply will land in the bubble, not the hero).
    await findByTestId('conversation-thread');
    await findByTestId('thread-typing');
    expect(getByText('why did it drift?')).toBeTruthy();
    expect(queryByTestId('final-response')).toBeNull();

    // Release the reply → it fills that same bubble, the typing indicator goes
    // away, and the turn settles into the thread. The persisted crew bubble's
    // open-trace link only appears once the turn settles, so awaiting it pins
    // the post-settle state.
    releaseChat(
      okJson(
        { reply: 'because someone set it in the console', tool_calls: [], conversation_id: 'new-conv' },
        { 'X-Trace-Id': 'trace-xyz' },
      ),
    );
    await findByTestId('thread-open-trace');
    expect(getByText('because someone set it in the console')).toBeTruthy();
    expect(queryByTestId('thread-typing')).toBeNull();
    // The hero stayed out of the way throughout — present again post-settle but
    // hidden (its reply was cleared into the thread).
    const hero = queryByTestId('final-response');
    expect(hero === null || hero.hasAttribute('hidden')).toBe(true);
  });
});

// Fast-path composer release (docs/plans/2026-07-09-chat-composer-early-unblock.md):
// on a clean, persistable SSE `done` frame, the turn settles + composer
// re-enables + ?conversation is set IMMEDIATELY, with backfillTrace/loadDecisions
// backgrounded — instead of holding the composer disabled through those two
// post-answer round-trips.
describe('App — SSE chat turn releases the composer at the done frame', () => {
  it('fast path: settles the thread and re-enables the composer without waiting for /trace', async () => {
    const frames =
      'event: meta\ndata: {"trace_id":"trace-fast"}\n\n' +
      'event: done\ndata: {"reply":"the answer arrived fast","tool_calls":[],"conversation_id":"conv-fast"}\n\n';
    let releaseTrace!: (r: Response) => void;
    const tracePromise = new Promise<Response>((res) => {
      releaseTrace = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/conversations/'))
          return okJson({ conversation_id: 'conv-fast', workload: 'drift', title: 'x', turns: [] });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        // /trace never resolves during this test — proves settle doesn't wait on it.
        if (url.includes('/trace/')) return tracePromise;
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByText } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'why did it drift?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    // Settled into the thread, composer re-enabled, and ?conversation set —
    // all while /trace is still pending.
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the answer arrived fast')).toBeTruthy());
    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('conv-fast'),
    );

    releaseTrace(okJson({ trace_id: 'trace-fast', events: [], complete: true }));
  });

  it('slow path: a done-less stream still fires the recovery guard', async () => {
    const frames = 'event: meta\ndata: {"trace_id":"trace-nodone"}\n\n';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/trace/'))
          return okJson({ trace_id: 'trace-nodone', events: [], complete: true });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => {
      const hero = getByTestId('final-response');
      expect(hero.hasAttribute('hidden')).toBe(false);
      expect(hero.textContent).toContain('The reasoning stream ended before a final reply arrived.');
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
  });

  it('paused refusal over SSE stays in the hero (no fast-path settle)', async () => {
    const frames =
      'event: meta\ndata: {"trace_id":"trace-paused"}\n\n' +
      'event: done\ndata: {"reply":"DriftScribe is paused (operator kill switch active).","tool_calls":[],"paused":true,"conversation_id":"echoed"}\n\n';
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/chat') && init?.method === 'POST') return sseChatResponse(frames);
        if (url.includes('/trace/'))
          return okJson({ trace_id: 'trace-paused', events: [], complete: true });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );

    const { findByTestId, getByTestId, queryByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'anything' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => {
      const hero = getByTestId('final-response');
      expect(hero.hasAttribute('hidden')).toBe(false);
      expect(hero.textContent).toContain('paused');
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('conversation')).toBeNull();
  });
});

// EXACT DTO shape from InfraDiagram.test.ts::adoptGraph() (the source of
// truth): groups carry asset_type/adoptable/count/managed/drift/sensitive;
// nodes carry id/label/asset_type/managed/location. Do NOT invent fields —
// lib/infra_graph.resourceCards() reads these exact names, and a wrong shape
// silently renders no card-adopt-btn.
const ADOPT_GRAPH = {
  generated_at: null,
  project: 'demo-proj',
  caveat: 'test caveat',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 0, drift: 1 },
  groups: [
    {
      asset_type: 'storage.googleapis.com/Bucket',
      label: 'Storage bucket',
      adoptable: true,
      count: 1,
      managed: 0,
      drift: 1,
      sensitive: false,
      nodes: [
        {
          id: 'g0n0',
          label: 'my-old-uploads',
          asset_type: 'storage.googleapis.com/Bucket',
          managed: false,
          location: 'asia-northeast1',
        },
      ],
    },
  ],
  edges: [],
};

function resumeFixtures() {
  const list = {
    conversations: [
      {
        conversation_id: 'c1',
        workload: 'explore',
        title: 'prior chat about drift',
        updated_at: new Date().toISOString(),
        turn_count: 2,
      },
    ],
  };
  const detail = {
    conversation_id: 'c1',
    workload: 'explore',
    title: 'prior chat about drift',
    turns: [
      { seq: 0, role: 'user', text: 'what changed?', workload: 'explore' },
      { seq: 1, role: 'crew', text: 'the env var EXTRA drifted', workload: 'explore', trace_id: 't1' },
    ],
  };
  return { list, detail };
}

function stubResumeFetch(graph: unknown = GRAPH) {
  const { list, detail } = resumeFixtures();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/conversations/')) return okJson(detail);
      if (url.includes('/conversations')) return okJson(list);
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(graph);
      return okJson({});
    }),
  );
}

// A DriftScribe trace id is 32 lowercase hex chars (see lib/deeplink.ts).
const HEX32 = 'eba334f9211d46cabc79e50ed200a5a1';

// Same as stubResumeFetch, plus a generic /trace/ response — needed for tests
// that open a reasoning replay (?reasoning=<id> boot, or "view reasoning" on a
// thread turn) alongside the resumed thread.
function stubResumeFetchWithTrace(graph: unknown = GRAPH) {
  const { list, detail } = resumeFixtures();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
      if (url.includes('/conversations/')) return okJson(detail);
      if (url.includes('/conversations')) return okJson(list);
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(graph);
      return okJson({});
    }),
  );
}

describe('App — composer New chat + crew lock', () => {
  it('hides the composer New chat button on a fresh boot', async () => {
    stubResumeFetch();
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('chat-prompt');
    expect(queryByTestId('composer-new-chat')).toBeNull();
  });

  it('resuming a thread shows New chat and soft-locks the other crews', async () => {
    stubResumeFetch();
    const { findByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await findByTestId('composer-new-chat');
    await waitFor(() => {
      // Thread crew is explore → the other three lock.
      const drift = container.querySelector('[data-testid="crew-card-drift"] input')!;
      expect(drift.getAttribute('aria-disabled')).toBe('true');
      const explore = container.querySelector('[data-testid="crew-card-explore"] input')!;
      expect(explore.getAttribute('aria-disabled')).toBeNull();
    });
  });

  it('New chat drops the thread, unlocks the crews, and hides itself', async () => {
    stubResumeFetch();
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('composer-new-chat'));
    await waitFor(() => {
      expect(queryByTestId('conversation-thread')).toBeNull();
      expect(queryByTestId('composer-new-chat')).toBeNull();
      expect(container.querySelector('input[aria-disabled="true"]')).toBeNull();
    });
  });

  it('an Adopt click on an open thread starts a clean slate before prefilling', async () => {
    stubResumeFetch(ADOPT_GRAPH);
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('card-adopt-btn'));
    await waitFor(() => {
      // Thread dropped (clean slate), composer prefilled on Provision, unlocked.
      expect(queryByTestId('conversation-thread')).toBeNull();
      const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
      expect(input.value).toContain('my-old-uploads');
      const checked = container.querySelector('input[type="radio"]:checked') as HTMLInputElement;
      expect(checked.value).toBe('provision');
      expect(container.querySelector('input[aria-disabled="true"]')).toBeNull();
    });
  });
});

// ?conversation=<id> deep-link (docs/plans/2026-07-09-conversation-url-deeplink.md):
// bookmarkable/shareable open thread, mirroring the shipped ?reasoning=<id>
// replay param. setConversationId() is the sole writer of conversationId, so
// these wiring tests assert the URL invariant holds at every transition.
describe('App — ?conversation boot deep-link', () => {
  it('rehydrates the thread from ?conversation=<id> on boot', async () => {
    stubResumeFetch();
    history.replaceState(null, '', '/?conversation=c1');
    const { findByTestId, getByText } = render(App);
    await findByTestId('conversation-thread');
    await waitFor(() => expect(getByText('the env var EXTRA drifted')).toBeTruthy());
  });

  it('sets ?conversation when a thread opens from the rail', async () => {
    stubResumeFetch();
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('conversation')).toBe('c1'),
    );
  });

  it('clears both ?conversation and ?reasoning on New chat, preserving unrelated params + hash', async () => {
    stubResumeFetchWithTrace();
    history.replaceState(null, '', '/?unrelated=1#frag');
    const { findByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    // Open a turn's reasoning too, so both params are live before New chat —
    // this is the historical-replay New chat exit (the banner's "← new chat",
    // NOT composer-new-chat, which hides itself in historical mode).
    await fireEvent.click(await findByTestId('thread-open-trace'));
    await findByTestId('historical-banner');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe('t1');
    });

    const newChatBtn = container.querySelector('#new-chat-btn') as HTMLButtonElement;
    await fireEvent.click(newChatBtn);

    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBeNull();
      expect(p.get('reasoning')).toBeNull();
      expect(p.get('unrelated')).toBe('1');
    });
    expect(window.location.hash).toBe('#frag');
  });

  it('restores both the thread and a reasoning replay from ?conversation&reasoning, keeping both params', async () => {
    stubResumeFetchWithTrace();
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId } = render(App);
    await findByTestId('conversation-thread');
    await findByTestId('historical-banner');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe(HEX32);
    });
  });

  it('clears ?conversation but still opens the ?reasoning replay when the boot conversation 404s', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/bad')) return new Response('not found', { status: 404 });
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    history.replaceState(null, '', `/?conversation=bad&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('historical-banner');
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBeNull();
      expect(p.get('reasoning')).toBe(HEX32);
    });
    expect(queryByTestId('conversation-thread')).toBeNull();
  });

  it('does not open the boot reasoning replay on top if New chat interrupts the boot conversation fetch (the Codex race)', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations')) return okJson({ conversations: [] });
        if (url.includes('/trace/')) return okJson({ trace_id: HEX32, complete: true, events: [] });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    history.replaceState(null, '', `/?conversation=c1&reasoning=${HEX32}`);
    const { findByTestId, queryByTestId } = render(App);

    // The boot conversation fetch is in flight — conversationId was already
    // set synchronously (before the awaited fetch), so New chat is showing.
    await fireEvent.click(await findByTestId('composer-new-chat'));

    // Release the stalled boot fetch AFTER the interruption; let openConversation's
    // own runSeq guard (drops the stale detail) and the boot continuation's guard
    // (skips the queued openTrace) both run.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));

    expect(queryByTestId('historical-banner')).toBeNull();
    expect(queryByTestId('conversation-thread')).toBeNull();
    expect(new URLSearchParams(window.location.search).get('reasoning')).toBeNull();
  });

  it('openTrace on a thread turn keeps ?conversation alongside the new ?reasoning', async () => {
    stubResumeFetchWithTrace();
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    await fireEvent.click(await findByTestId('thread-open-trace'));
    await waitFor(() => {
      const p = new URLSearchParams(window.location.search);
      expect(p.get('conversation')).toBe('c1');
      expect(p.get('reasoning')).toBe('t1');
    });
  });

  // Codex review 019f46e8 must-fix: conversationId is set (synchronously) before
  // GET /conversations/{id} resolves, so there's a window where the thread is
  // "open" but conversationWorkload (and therefore the crew lock) is still null.
  // Without disabling Send there, a submit during that window rides the
  // half-open thread's id with whatever crew happens to be picked — the
  // crew-switch-reset guard in submitChat can't see the mismatch because it
  // requires conversationWorkload !== null. The boot deep-link makes this easier
  // to hit than the rail-click path (a cold fetch is slower than a warm one).
  it('disables Send while a resumed thread is still rehydrating (no crew-switch race)', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
  });

  // Codex review 019f46e8 (round 2): the first fix left resumingConversation
  // stuck true forever whenever something OTHER than openConversation itself
  // supersedes a pending resume — newChat/openTrace bump runSeq, so the stale
  // openConversation's own `if (myRun === runSeq)` guard correctly refuses to
  // clear a flag it no longer owns, but nothing else cleared it either. Both
  // superseding entry points now reset it themselves.
  it('New chat during a pending resume re-enables Send instead of leaving it stuck disabled', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/decisions')) return okJson({ decisions: [] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    // Interrupt the pending resume with New chat — Send must re-enable now,
    // not stay disabled waiting for a resume that no longer matters.
    await fireEvent.click(await findByTestId('composer-new-chat'));
    await waitFor(() => expect(sendBtn.disabled).toBe(false));

    // The stale detail landing afterwards (openConversation's own runSeq guard
    // drops it) must not re-disable Send either.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));
    expect(sendBtn.disabled).toBe(false);
  });

  it('opening a reasoning replay during a pending resume does not leave Send stuck disabled after returning to chat', async () => {
    let releaseDetail!: (r: Response) => void;
    const detailPromise = new Promise<Response>((res) => {
      releaseDetail = res;
    });
    const iac = {
      decision_id: 'd1',
      trace_id: 'tid-iac-1',
      action: 'iac_apply',
      pr_number: 47,
      apply_status: 'applied',
      approver: 'op@example.com',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/conversations/c1')) return detailPromise;
        if (url.includes('/conversations'))
          return okJson({
            conversations: [
              {
                conversation_id: 'c1',
                workload: 'explore',
                title: 'prior chat',
                updated_at: new Date().toISOString(),
                turn_count: 1,
              },
            ],
          });
        if (url.includes('/trace/')) return okJson({ trace_id: 'tid-iac-1', complete: true, events: [], decision: iac });
        if (url.includes('/decisions')) return okJson({ decisions: [iac] });
        if (url.includes('/infra/graph')) return okJson(GRAPH);
        return okJson({});
      }),
    );
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));

    const sendBtn = (await findByTestId('chat-submit')) as HTMLButtonElement;
    await waitFor(() => expect(sendBtn.disabled).toBe(true));

    // Interrupt the pending resume with a reasoning replay from the rail.
    await fireEvent.click(await findByTestId('open-trace-button'));
    await findByTestId('historical-banner');

    // The stale resume detail landing mid-replay must not corrupt anything.
    releaseDetail(okJson({ conversation_id: 'c1', workload: 'explore', title: 'x', turns: [] }));
    await new Promise((r) => setTimeout(r, 20));

    // Exit the replay — Send must not be stuck disabled by a leftover flag
    // openTrace never claimed.
    const exitBtn = document.querySelector('#new-chat-btn') as HTMLButtonElement;
    await fireEvent.click(exitBtn);
    await waitFor(() => expect(sendBtn.disabled).toBe(false));
  });
});
