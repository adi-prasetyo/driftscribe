import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import ReasoningDisclosure from '../../src/components/ReasoningDisclosure.svelte';
import { createTraceCache } from '../../src/lib/traceCache';
import type { TraceEvent } from '../../src/lib/timeline';
import type { TraceResponse } from '../../src/lib/types';

// The collapsed reasoning line under a crew reply. It owns no trace state —
// everything comes from the per-trace cache — so these tests drive a REAL
// cache with a mocked `call`, which is also what pins the "expanding while the
// stream runs must not fetch /trace" contract end to end.

afterEach(cleanup);

const TID = 'a'.repeat(32);

function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const ev = (over: Partial<TraceEvent>): TraceEvent =>
  ({ event: 'llm_thought', trace_id: TID, ...over }) as TraceEvent;

function traceResponse(over: Partial<TraceResponse> = {}): TraceResponse {
  return { trace_id: TID, events: [], decision: null, complete: true, ...over };
}

function mount(
  handler: (path: string) => Response | Promise<Response> = () => res(traceResponse()),
  props: Record<string, unknown> = {},
) {
  const paths: string[] = [];
  const call = vi.fn(async (path: string) => {
    paths.push(path);
    return handler(path);
  });
  const cache = createTraceCache(call);
  const view = render(ReasoningDisclosure, {
    props: { traceId: TID, cache, ...props },
  });
  return { ...view, cache, paths, call };
}

describe('ReasoningDisclosure — collapsed line', () => {
  it('is collapsed by default and shows the static label', () => {
    const { getByTestId } = mount();
    const line = getByTestId('reasoning-disclosure');
    expect(line.getAttribute('aria-expanded')).toBe('false');
    expect(getByTestId('reasoning-subtitle').textContent).toBe('View reasoning');
  });

  it('renders no trace detail while collapsed', () => {
    const { queryByTestId } = mount();
    expect(queryByTestId('trace-detail')).toBeNull();
  });

  it('takes its subtitle from the latest thought chunk', async () => {
    const { getByTestId, cache } = mount();
    cache.beginLive(TID);
    cache.appendLive(TID, ev({ thought_text: '**Assessing the drift**' }));
    await waitFor(() =>
      expect(getByTestId('reasoning-subtitle').textContent).toBe('Assessing the drift'),
    );
  });

  it('updates the subtitle as later thoughts stream in', async () => {
    const { getByTestId, cache } = mount();
    cache.beginLive(TID);
    cache.appendLive(TID, ev({ thought_text: 'first' }));
    await waitFor(() => expect(getByTestId('reasoning-subtitle').textContent).toBe('first'));
    cache.appendLive(TID, ev({ thought_text: 'second' }));
    await waitFor(() => expect(getByTestId('reasoning-subtitle').textContent).toBe('second'));
  });

  it('says "Thinking…" while streaming before any summary arrives', async () => {
    // Vertex can omit summaries for a whole run, so this is not necessarily a
    // transient state — offering to "view reasoning" that may never exist
    // would be a promise the run can't keep.
    const { getByTestId, cache } = mount();
    cache.beginLive(TID);
    await waitFor(() => expect(getByTestId('reasoning-subtitle').textContent).toBe('Thinking…'));
  });

  it('marks the line when the stream errored, keeping the partial subtitle', async () => {
    const { getByTestId, cache } = mount();
    cache.beginLive(TID);
    cache.appendLive(TID, ev({ thought_text: 'got this far' }));
    cache.endLive(TID, 'error');
    await waitFor(() => expect(getByTestId('reasoning-stream-error')).toBeTruthy());
    expect(getByTestId('reasoning-subtitle').textContent).toBe('got this far');
  });

  it('shows no stream-error marker on a clean run', async () => {
    const { queryByTestId, cache } = mount();
    cache.beginLive(TID);
    cache.endLive(TID, 'complete');
    await waitFor(() => expect(queryByTestId('reasoning-stream-error')).toBeNull());
  });
});

describe('ReasoningDisclosure — expanding', () => {
  it('fetches the trace exactly once on first expand', async () => {
    const { getByTestId, paths } = mount();
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(paths).toEqual([`/trace/${TID}`]));
    await fireEvent.click(getByTestId('reasoning-disclosure')); // collapse
    await fireEvent.click(getByTestId('reasoning-disclosure')); // re-expand
    expect(paths).toEqual([`/trace/${TID}`]);
  });

  it('does NOT fetch /trace when expanded during a live stream', async () => {
    // The `meta` frame creates the entry before any timeline event, so an
    // early expand finds a streaming entry and asks for nothing — the trace is
    // not in Cloud Logging yet and the stream is authoritative for it.
    const { getByTestId, cache, paths } = mount();
    cache.beginLive(TID);
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(getByTestId('trace-detail')).toBeTruthy());
    expect(paths).toEqual([]);
  });

  it('renders the streamed rows live while expanded', async () => {
    const { getByTestId, getAllByTestId, cache } = mount();
    cache.beginLive(TID);
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    cache.appendLive(TID, ev({ thought_text: 'thinking about ports' }));
    cache.appendLive(TID, ev({ event: 'tool_call', tool_name: 'read_live_env_tool' }));
    await waitFor(() => expect(getAllByTestId('trace-row-thought')).toHaveLength(1));
    expect(getAllByTestId('trace-row-tool')).toHaveLength(1);
  });

  it('renders the loaded rows in interleaved order after expanding a history turn', async () => {
    const { getByTestId, container } = mount(() =>
      res(
        traceResponse({
          events: [
            ev({ insert_id: 't1', thought_text: 'first' }),
            ev({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env_tool' }),
            ev({ event: 'tool_result', insert_id: 'r1', tool_name: 'read_live_env_tool', result_ok: true }),
            ev({ insert_id: 't2', thought_text: 'second' }),
          ],
        }),
      ),
    );
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() =>
      expect(
        [...container.querySelectorAll('[data-testid^="trace-row-"]')].map((n) =>
          n.getAttribute('data-testid'),
        ),
      ).toEqual(['trace-row-thought', 'trace-row-tool', 'trace-row-thought']),
    );
  });

  it('surfaces a failed fetch with a retry that re-asks', async () => {
    let fail = true;
    const { getByTestId, paths } = mount(() =>
      fail ? res({}, 500) : res(traceResponse({ events: [ev({ insert_id: 't1', thought_text: 'ok' })] })),
    );
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(getByTestId('trace-detail-error')).toBeTruthy());
    fail = false;
    await fireEvent.click(getByTestId('trace-detail-retry'));
    await waitFor(() => expect(getByTestId('trace-row-thought')).toBeTruthy());
    expect(paths).toHaveLength(2);
  });

  it('passes the conversation id down so the copied link carries thread context', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { getByTestId } = mount(undefined, { conversationId: 'conv-9' });
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(getByTestId('trace-copy-link')).toBeTruthy());
    await fireEvent.click(getByTestId('trace-copy-link'));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(String(writeText.mock.calls[0][0])).toContain('conversation=conv-9');
  });

  it('reports a fail-soft missing PR body without an error state', async () => {
    const { getByTestId, queryByTestId } = mount((path) =>
      path.endsWith('/pr-body')
        ? res({}, 502)
        : res(traceResponse({ decision: { decision_id: 'd', action: 'iac_apply' } })),
    );
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(getByTestId('trace-detail-prbody-missing')).toBeTruthy());
    expect(queryByTestId('trace-detail-error')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// ds-jns PR 2 — `autoExpand`: how a `?conversation=&reasoning=` deep link lands
// on the MESSAGE it names, instead of opening a page-level replay over the
// thread that message belongs to.
// ---------------------------------------------------------------------------
describe('ReasoningDisclosure — autoExpand', () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  it('opens on mount, loads the trace, and brings itself into view', async () => {
    const { getByTestId, paths } = mount(undefined, { autoExpand: true });
    expect(getByTestId('reasoning-disclosure').getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(paths).toContain(`/trace/${TID}`));
    await waitFor(() => expect(getByTestId('trace-detail')).toBeTruthy());
    expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it('stays collapsed and fetches nothing when it is not the named message', () => {
    const { getByTestId, queryByTestId, paths } = mount(undefined, { autoExpand: false });
    expect(getByTestId('reasoning-disclosure').getAttribute('aria-expanded')).toBe('false');
    expect(queryByTestId('trace-detail')).toBeNull();
    expect(paths).toEqual([]);
  });

  it('does not spring back open when the operator collapses it', async () => {
    // The effect writes `open` but never READS it, so nothing re-arms when the
    // operator closes an auto-expanded disclosure. A deep link is an opening
    // gesture, not a lock.
    const { getByTestId, queryByTestId } = mount(undefined, { autoExpand: true });
    await waitFor(() => expect(getByTestId('trace-detail')).toBeTruthy());
    await fireEvent.click(getByTestId('reasoning-disclosure'));
    await waitFor(() => expect(queryByTestId('trace-detail')).toBeNull());
    expect(getByTestId('reasoning-disclosure').getAttribute('aria-expanded')).toBe('false');
  });
});
