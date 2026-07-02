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
    // reasoning coexist, and the "viewing historical trace" banner is absent.
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
