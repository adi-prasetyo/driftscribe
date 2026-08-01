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

describe('TraceDetail — the caller can name the decision (ds-jns)', () => {
  // A desk ledger row holds its own copy of the decision, from GET /decisions.
  // Without a way to hand it over, one card could name the action in its header
  // and, in the panel directly below, say the trace could not be loaded — two
  // statements about one decision, one of them wrong.
  const IAC: Decision = { decision_id: 'd', action: 'iac_apply' };

  it('uses the caller’s decision when the fetched trace carries none', () => {
    const { getByTestId } = mount(entry({ enrich: 'loaded' }), { decision: IAC });
    expect(getByTestId('trace-detail-empty').textContent).toContain('recorded directly');
    expect(getByTestId('decision-summary')).toBeTruthy();
  });

  it('an OMITTED prop still means "use the entry’s" — not "there is none"', () => {
    const { getByTestId } = mount(entry({ enrich: 'loaded', decision: IAC }));
    expect(getByTestId('trace-detail-empty').textContent).toContain('recorded directly');
  });

  it('an explicit null overrides the entry’s decision', () => {
    // The distinction the `undefined` default exists for: a caller that knows
    // there is no decision can say so, and is not confused with one that has
    // not been asked.
    const { getByTestId } = mount(entry({ enrich: 'loaded', decision: IAC }), { decision: null });
    expect(getByTestId('trace-detail-empty').textContent).toContain("couldn't be loaded");
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

  it('copies a bare-reasoning URL when there is no conversation to frame it', async () => {
    // ds-jns gave `?reasoning=` a destination of its own (a desk decision
    // record), so the conversation-less shape is a URL too. It used to copy the
    // raw trace id — which the footer already displays two elements away, and
    // which nobody can paste anywhere useful.
    const writeText = stubClipboard();
    const { getByTestId } = mount(entry({ enrich: 'loaded' }));
    await fireEvent.click(getByTestId('trace-copy-link'));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    const url = new URL(writeText.mock.calls[0][0]);
    expect(url.searchParams.get('reasoning')).toBe(TID);
    expect(url.searchParams.get('conversation')).toBeNull();
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

// The inline HITL gate. Carried over from the deleted page-level Timeline,
// which was its only renderer — so between that deletion and this, an operator
// who asked a crew to roll something back got the approval URL as a substring
// of a JSON preview and nothing to click.
//
// The security guard itself lives in ApprovalCta (and is pinned in its own
// suite against every hostile URL shape). What is pinned HERE is that this
// component ASKS: on the right tool, and not on any other.
describe('TraceDetail — a rollback proposal offers its approval inline', () => {
  const APPROVAL = 'http://localhost:3000/approvals/ap-7?t=tok';

  function rollbackPair(toolName: string, preview: unknown) {
    return [
      ev({ event: 'tool_call', tool_name: toolName, insert_id: 'c1' }),
      ev({
        event: 'tool_result',
        tool_name: toolName,
        result_ok: true,
        result_preview: JSON.stringify(preview),
        insert_id: 'r1',
      }),
    ];
  }

  it('turns the proposal payload into a same-origin Approve link', () => {
    const { getByRole } = mount(
      entry({ events: rollbackPair('propose_rollback_tool', { approval_url: APPROVAL }) }),
    );
    const link = getByRole('link', { name: /approve/i }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toContain('/approvals/ap-7');
  });

  it('offers nothing on a different tool carrying the same payload', () => {
    // The gate is the TOOL, not the shape of the payload: any tool could
    // return a field called approval_url, and only the rollback proposal means
    // "an operator must now approve this".
    const { queryByRole } = mount(
      entry({ events: rollbackPair('drift_read_live_env', { approval_url: APPROVAL }) }),
    );
    expect(queryByRole('link', { name: /approve/i })).toBeNull();
  });

  it('offers nothing when the proposal carried no approval url', () => {
    const { queryByRole } = mount(
      entry({ events: rollbackPair('propose_rollback_tool', { ok: true }) }),
    );
    expect(queryByRole('link', { name: /approve/i })).toBeNull();
  });
});

// Run accounting: what the run SPENT and what its grounding actually returned.
// Both rendered only in the deleted page-level Timeline. `interleaveTimeline`
// drops llm_usage from the row list on purpose (accounting is not a step in the
// story), which is exactly why the total has to be rendered somewhere else —
// and between deleting Timeline and this, it was rendered nowhere.
describe('TraceDetail — what the run spent and what it consulted', () => {
  it('sums llm_usage across steps into one footer total', () => {
    // Per-STEP events, not one per run: a multi-step run emits several, and
    // showing the last one would understate the cost of every run that took
    // more than one turn through the model.
    const { getByTestId } = mount(
      entry({
        events: [
          ev({ event: 'llm_usage', total_token_count: 300, insert_id: 'u1' }),
          ev({ event: 'llm_thought', thought_text: 'weighing it', insert_id: 't1' }),
          ev({ event: 'llm_usage', total_token_count: 504, insert_id: 'u2' }),
        ],
      }),
    );
    expect(getByTestId('trace-tokens').textContent).toContain('804');
  });

  it('says nothing rather than "0" when the run spent nothing measurable', () => {
    // A directly-recorded iac_apply has no reasoning run behind it at all.
    // "0 tokens spent" would read as a measurement; there was no measurement.
    const a = mount(entry({ events: [ev({ event: 'llm_thought', thought_text: 'x' })] }));
    expect(a.queryByTestId('trace-tokens')).toBeNull();
    cleanup();
    const b = mount(entry({ events: [ev({ event: 'llm_usage', total_token_count: 0 })] }));
    expect(b.queryByTestId('trace-tokens')).toBeNull();
  });

  it('says how many documents an MCP call came back with, and omits an empty one', () => {
    const { getAllByTestId, getByTestId } = mount(
      entry({
        events: [
          ev({ event: 'mcp_call', mcp_server: 'developer_knowledge', doc_count: 7, latency_ms: 120, insert_id: 'm1' }),
          ev({ event: 'mcp_call', mcp_server: 'developer_knowledge', doc_count: 0, latency_ms: 90, insert_id: 'm2' }),
        ],
      }),
    );
    expect(getAllByTestId('trace-row-mcp')).toHaveLength(2);
    // Only the call that actually returned something claims to have.
    const docs = getAllByTestId('trace-row-mcp-docs');
    expect(docs).toHaveLength(1);
    expect(docs[0].textContent).toContain('7');
    // …and the latency of the empty one still renders, so the row is not blank.
    expect(getByTestId('trace-detail').textContent).toContain('90');
  });
});
