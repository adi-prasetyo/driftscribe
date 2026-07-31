import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import TraceDetail from '../../src/components/TraceDetail.svelte';
import type { TraceCacheEntry } from '../../src/lib/traceCache';
import type { TraceEvent } from '../../src/lib/timeline';
import type { Decision, PrBody } from '../../src/lib/types';

// TraceDetail is the EXPANDED content of one trace — the interleaved
// thought/tool/MCP rows plus the decision + PR-body enrichment. It takes a
// cache ENTRY, not a store, because PR 2's desk decision record mounts the
// same component off the chat view.

afterEach(cleanup);

const TID = 'a'.repeat(32);

function entry(over: Partial<TraceCacheEntry> = {}): TraceCacheEntry {
  return {
    events: [],
    stream: 'idle',
    enrich: 'idle',
    decision: null,
    prBody: null,
    prBodyMissing: false,
    complete: true,
    ...over,
  };
}

const ev = (over: Partial<TraceEvent>): TraceEvent =>
  ({ event: 'llm_thought', trace_id: TID, ...over }) as TraceEvent;

function mount(e: TraceCacheEntry, props: Record<string, unknown> = {}) {
  const onRetry = vi.fn();
  const view = render(TraceDetail, {
    props: { traceId: TID, entry: e, onRetry, ...props },
  });
  return { ...view, onRetry };
}

describe('TraceDetail — rows', () => {
  it('renders the interleaved rows in order', () => {
    const { container } = mount(
      entry({
        enrich: 'loaded',
        events: [
          ev({ event: 'llm_thought', insert_id: 't1', thought_text: 'Reading the service' }),
          ev({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env_tool' }),
          ev({ event: 'tool_result', insert_id: 'r1', tool_name: 'read_live_env_tool', result_ok: true }),
          ev({ event: 'mcp_call', insert_id: 'm1', mcp_server: 'developer_knowledge', mcp_tool: 'search_documents' }),
        ],
      }),
    );
    const kinds = [...container.querySelectorAll('[data-testid^="trace-row-"]')].map((n) =>
      n.getAttribute('data-testid'),
    );
    expect(kinds).toEqual(['trace-row-thought', 'trace-row-tool', 'trace-row-mcp']);
  });

  it('renders thought text as escaped plain text (never markup)', () => {
    const { getByTestId } = mount(
      entry({
        enrich: 'loaded',
        events: [ev({ insert_id: 't1', thought_text: '<img src=x onerror=alert(1)>' })],
      }),
    );
    const row = getByTestId('trace-row-thought');
    expect(row.querySelector('img')).toBeNull();
    expect(row.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('names the tool on each tool row (an interleaved list has no subgroup header)', () => {
    const { getByTestId } = mount(
      entry({
        enrich: 'loaded',
        events: [ev({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env_tool' })],
      }),
    );
    // workerLabel resolves the ADK tool __name__ to its operator-facing label.
    expect(getByTestId('trace-row-tool').textContent).toContain('Reader');
  });

  it('marks an unresolved tool call pending, a failed one errored', () => {
    const { getAllByTestId } = mount(
      entry({
        enrich: 'loaded',
        events: [
          ev({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env_tool' }),
          ev({ event: 'tool_call', insert_id: 'c2', tool_name: 'notify_tool' }),
          ev({ event: 'tool_result', insert_id: 'r2', tool_name: 'notify_tool', result_ok: false }),
        ],
      }),
    );
    const rows = getAllByTestId('trace-row-tool');
    expect(rows[0].textContent).toContain('pending');
    expect(rows[1].textContent?.toLowerCase()).toContain('error');
  });
});

describe('TraceDetail — states', () => {
  it('shows an error line with a retry button, never a blank panel', async () => {
    const { getByTestId, onRetry } = mount(entry({ enrich: 'error' }));
    expect(getByTestId('trace-detail-error')).toBeTruthy();
    await fireEvent.click(getByTestId('trace-detail-retry'));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('keeps whatever streamed even when enrichment errored (independent axes)', () => {
    const { getByTestId, getAllByTestId } = mount(
      entry({
        stream: 'complete',
        enrich: 'error',
        events: [ev({ insert_id: 't1', thought_text: 'partial reasoning' })],
      }),
    );
    expect(getByTestId('trace-detail-error')).toBeTruthy();
    expect(getAllByTestId('trace-row-thought')).toHaveLength(1);
  });

  it('shows a loading line while the first fetch is in flight', () => {
    const { getByTestId } = mount(entry({ enrich: 'loading' }));
    expect(getByTestId('trace-detail-loading')).toBeTruthy();
  });

  it('does not show the loading line when live rows are already on screen', () => {
    const { queryByTestId } = mount(
      entry({ enrich: 'loading', stream: 'complete', events: [ev({ insert_id: 't1', thought_text: 'x' })] }),
    );
    expect(queryByTestId('trace-detail-loading')).toBeNull();
  });

  it('explains an empty loaded trace as "couldn\'t load" for a reasoning turn', () => {
    const { getByTestId } = mount(entry({ enrich: 'loaded' }));
    expect(getByTestId('trace-detail-empty').textContent).toContain("couldn't be loaded");
  });

  it('explains an empty iac_apply trace as directly recorded, not as a failure', () => {
    const decision: Decision = { decision_id: 'd', action: 'iac_apply' };
    const { getByTestId } = mount(entry({ enrich: 'loaded', decision }));
    expect(getByTestId('trace-detail-empty').textContent).toContain('recorded directly');
  });
});

describe('TraceDetail — omitted thought note', () => {
  it('renders the note on a settled trace that spent thinking tokens with no summaries', () => {
    const { getByTestId } = mount(
      entry({
        stream: 'complete',
        enrich: 'loaded',
        events: [ev({ event: 'llm_usage', insert_id: 'u1', thoughts_token_count: 714 })],
      }),
    );
    expect(getByTestId('thought-omitted-note').textContent).toContain('714');
  });

  it('does NOT render the note mid-stream (usage is per LLM step)', () => {
    const { queryByTestId } = mount(
      entry({
        stream: 'streaming',
        events: [ev({ event: 'llm_usage', insert_id: 'u1', thoughts_token_count: 500 })],
      }),
    );
    expect(queryByTestId('thought-omitted-note')).toBeNull();
  });

  it('does NOT render the note on an errored stream', () => {
    const { queryByTestId } = mount(
      entry({
        stream: 'error',
        enrich: 'loaded',
        events: [ev({ event: 'llm_usage', insert_id: 'u1', thoughts_token_count: 500 })],
      }),
    );
    expect(queryByTestId('thought-omitted-note')).toBeNull();
  });
});

describe('TraceDetail — enrichment cards', () => {
  it('renders the diff card for a decision carrying diffs', () => {
    const decision: Decision = {
      decision_id: 'd',
      action: 'drift_pr',
      diffs: [{ name: 'PORT', expected: '8080', live: '9090', contract_status: 'present_disallow_manual' }],
    };
    const { getByTestId } = mount(entry({ enrich: 'loaded', decision }));
    expect(getByTestId('drift-diff-card')).toBeTruthy();
  });

  it('passes the PR body FIELDS to PrBodyDisclosure, not the object', () => {
    const prBody: PrBody = {
      pr_number: 42,
      head_sha: 'abc',
      body: 'Adopts the bucket.',
      body_truncated: false,
      cached: false,
    };
    const { getByText } = mount(
      entry({ enrich: 'loaded', decision: { decision_id: 'd', action: 'iac_apply' }, prBody }),
    );
    expect(getByText('Adopts the bucket.')).toBeTruthy();
  });

  it('renders a quiet fail-soft line for a missing PR body, not an error state', () => {
    const { getByTestId, queryByTestId } = mount(
      entry({
        enrich: 'loaded',
        decision: { decision_id: 'd', action: 'iac_apply' },
        prBodyMissing: true,
      }),
    );
    expect(getByTestId('trace-detail-prbody-missing')).toBeTruthy();
    expect(queryByTestId('trace-detail-error')).toBeNull();
  });
});

describe('TraceDetail — copyable deep link', () => {
  function stubClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    return writeText;
  }

  it('copies a conversation+reasoning URL when the trace belongs to a thread', async () => {
    const writeText = stubClipboard();
    const { getByTestId } = mount(entry({ enrich: 'loaded' }), { conversationId: 'conv-1' });
    await fireEvent.click(getByTestId('trace-copy-link'));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    const url = new URL(writeText.mock.calls[0][0]);
    expect(url.searchParams.get('conversation')).toBe('conv-1');
    expect(url.searchParams.get('reasoning')).toBe(TID);
  });

  it('copies the bare trace id when there is no conversation to frame it', async () => {
    const writeText = stubClipboard();
    const { getByTestId } = mount(entry({ enrich: 'loaded' }));
    await fireEvent.click(getByTestId('trace-copy-link'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TID));
  });

  it('is a button, not a link (it copies, it does not navigate)', () => {
    stubClipboard();
    const { getByTestId } = mount(entry({ enrich: 'loaded' }), { conversationId: 'c' });
    const el = getByTestId('trace-copy-link');
    expect(el.tagName).toBe('BUTTON');
    expect(el.getAttribute('href')).toBeNull();
  });

  it('survives a rejected clipboard write without throwing', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { getByTestId } = mount(entry({ enrich: 'loaded' }));
    await fireEvent.click(getByTestId('trace-copy-link'));
    await waitFor(() => expect(writeText).toHaveBeenCalled());
    expect(getByTestId('trace-copy-link').textContent).toContain('Copy link');
  });
});
