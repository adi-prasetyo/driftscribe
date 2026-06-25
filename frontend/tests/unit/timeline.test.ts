import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import {
  groupOf,
  subKey,
  groupEvents,
  pairToolEvents,
  eventKey,
  type GroupKey,
  type TraceEvent,
} from '../../src/lib/timeline';
import Timeline from '../../src/components/Timeline.svelte';

// Re-homes the event-classification + sub-grouping + tool-pairing logic that
// lived inline in agent/templates/transparency.html (~1282-1400 + the
// _pairToolEvents helper at ~944). The binning is authoritative per plan §3:
//   llm_thought / llm_usage  -> coordinator
//   tool_call  / tool_result -> tools   (sub-grouped by tool_name)
//   mcp_call                 -> mcp     (sub-grouped by mcp_tool || mcp_server)
//   final_response           -> null (skipped; rendered in #final-response-card)
//   unknown                  -> null (dropped)
// CRITICAL: MCP routing is by event === 'mcp_call', NOT by any tool-name prefix.

// --- small fixtures -------------------------------------------------------- //
function ev(partial: Partial<TraceEvent> & { event: string }): TraceEvent {
  return { trace_id: 'a'.repeat(32), ...partial } as TraceEvent;
}

describe('groupOf — event kind -> group', () => {
  it('routes llm_thought to coordinator', () => {
    expect(groupOf(ev({ event: 'llm_thought' }))).toBe('coordinator');
  });

  it('routes llm_usage to coordinator', () => {
    expect(groupOf(ev({ event: 'llm_usage' }))).toBe('coordinator');
  });

  it('routes tool_call to tools', () => {
    expect(groupOf(ev({ event: 'tool_call', tool_name: 't' }))).toBe('tools');
  });

  it('routes tool_result to tools', () => {
    expect(groupOf(ev({ event: 'tool_result', tool_name: 't' }))).toBe('tools');
  });

  it('routes mcp_call to mcp (by event kind, NOT tool-name prefix)', () => {
    expect(groupOf(ev({ event: 'mcp_call', mcp_tool: 'search' }))).toBe('mcp');
  });

  it('does NOT route a tool_call to mcp even if its tool_name looks MCP-ish', () => {
    // The removed MCP_TOOL_PREFIXES routing must NOT resurface: a tool_call is
    // always 'tools', regardless of the tool_name string.
    expect(groupOf(ev({ event: 'tool_call', tool_name: 'mcp_developer_knowledge' }))).toBe(
      'tools',
    );
    expect(groupOf(ev({ event: 'tool_result', tool_name: 'developer_knowledge_search' }))).toBe(
      'tools',
    );
  });

  it('skips final_response (returns null — rendered elsewhere)', () => {
    expect(groupOf(ev({ event: 'final_response' }))).toBeNull();
  });

  it('drops unknown event kinds (returns null)', () => {
    expect(groupOf(ev({ event: 'some_future_kind' }))).toBeNull();
    expect(groupOf(ev({ event: '' }))).toBeNull();
  });
});

describe('subKey — sub-group key per event', () => {
  it('tools: uses tool_name', () => {
    expect(subKey(ev({ event: 'tool_call', tool_name: 'read_live_env_tool' }))).toBe(
      'read_live_env_tool',
    );
    expect(subKey(ev({ event: 'tool_result', tool_name: 'open_infra_pr_tool' }))).toBe(
      'open_infra_pr_tool',
    );
  });

  it("tools: falls back to '(unknown)' when tool_name is absent/empty", () => {
    expect(subKey(ev({ event: 'tool_call' }))).toBe('(unknown)');
    expect(subKey(ev({ event: 'tool_result', tool_name: '' }))).toBe('(unknown)');
  });

  it('mcp: uses mcp_tool when present', () => {
    expect(subKey(ev({ event: 'mcp_call', mcp_tool: 'search_docs', mcp_server: 'dev-kb' }))).toBe(
      'search_docs',
    );
  });

  it('mcp: falls back to mcp_server when mcp_tool absent (older event shape)', () => {
    expect(subKey(ev({ event: 'mcp_call', mcp_server: 'developer-knowledge' }))).toBe(
      'developer-knowledge',
    );
  });

  it("mcp: falls back to '(unknown)' when neither mcp_tool nor mcp_server present", () => {
    expect(subKey(ev({ event: 'mcp_call' }))).toBe('(unknown)');
  });
});

describe('groupEvents — bins a mixed list', () => {
  it('partitions every event into its group and drops final_response/unknown', () => {
    const events: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: '1' }),
      ev({ event: 'tool_call', tool_name: 'read_live_env_tool', insert_id: '2' }),
      ev({ event: 'mcp_call', mcp_tool: 'search', insert_id: '3' }),
      ev({ event: 'llm_usage', insert_id: '4' }),
      ev({ event: 'tool_result', tool_name: 'read_live_env_tool', insert_id: '5' }),
      ev({ event: 'final_response', insert_id: '6' }),
      ev({ event: 'mystery_kind', insert_id: '7' }),
    ];
    const g = groupEvents(events);

    expect(g.coordinator.map((e) => e.insert_id)).toEqual(['1', '4']);
    expect(g.tools.map((e) => e.insert_id)).toEqual(['2', '5']);
    expect(g.mcp.map((e) => e.insert_id)).toEqual(['3']);
  });

  it('always returns all three keys, even for an empty input', () => {
    const g = groupEvents([]);
    const keys: GroupKey[] = ['coordinator', 'tools', 'mcp'];
    for (const k of keys) {
      expect(Array.isArray(g[k])).toBe(true);
      expect(g[k]).toHaveLength(0);
    }
  });

  it('preserves chronological (first-seen) order within each group', () => {
    const events: TraceEvent[] = [
      ev({ event: 'tool_call', tool_name: 'b', insert_id: 'b1' }),
      ev({ event: 'tool_call', tool_name: 'a', insert_id: 'a1' }),
      ev({ event: 'tool_call', tool_name: 'b', insert_id: 'b2' }),
    ];
    expect(groupEvents(events).tools.map((e) => e.insert_id)).toEqual(['b1', 'a1', 'b2']);
  });
});

describe('pairToolEvents — pair call+result by order within a tool_name', () => {
  it('pairs a call with the next result of the same tool_name', () => {
    const call = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c1' });
    const result = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r1' });
    const pairs = pairToolEvents([call, result]);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].call).toBe(call);
    expect(pairs[0].result).toBe(result);
  });

  it('pairs FIFO within a tool_name across interleaved calls/results', () => {
    const c1 = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c1' });
    const c2 = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c2' });
    const r1 = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r1' });
    const r2 = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r2' });
    const pairs = pairToolEvents([c1, c2, r1, r2]);
    expect(pairs).toHaveLength(2);
    // r1 closes the oldest open call (c1); r2 closes c2.
    expect(pairs[0]).toEqual({ call: c1, result: r1 });
    expect(pairs[1]).toEqual({ call: c2, result: r2 });
  });

  it('does NOT cross-pair across different tool_names', () => {
    const ca = ev({ event: 'tool_call', tool_name: 'a', insert_id: 'ca' });
    const rb = ev({ event: 'tool_result', tool_name: 'b', insert_id: 'rb' });
    const ra = ev({ event: 'tool_result', tool_name: 'a', insert_id: 'ra' });
    const pairs = pairToolEvents([ca, rb, ra]);
    expect(pairs).toHaveLength(2);
    // 'a' pairs ca+ra; 'b' has only an orphan result rb (call absent).
    const aPair = pairs.find((p) => p.call === ca);
    expect(aPair).toBeDefined();
    expect(aPair?.result).toBe(ra);
    const orphan = pairs.find((p) => p.result === rb);
    expect(orphan).toBeDefined();
    expect(orphan?.call == null).toBe(true);
  });

  it('emits an unmatched (in-flight) call as a singleton (result undefined/null)', () => {
    const call = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c1' });
    const pairs = pairToolEvents([call]);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].call).toBe(call);
    expect(pairs[0].result == null).toBe(true);
  });

  it('emits a lone result as a singleton (call null) — orphan, out-of-window', () => {
    const result = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r1' });
    const pairs = pairToolEvents([result]);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].call == null).toBe(true); // absent (contract: optional)
    expect(pairs[0].result).toBe(result);
  });

  it('returns [] for an empty event list', () => {
    expect(pairToolEvents([])).toEqual([]);
  });
});

describe('eventKey — stable per-event DOM key', () => {
  it("uses 'evt:'+insert_id when insert_id is present", () => {
    expect(eventKey(ev({ event: 'llm_thought', insert_id: 'abc123' }))).toBe('evt:abc123');
  });

  it('is stable (idempotent) for the same insert_id across calls', () => {
    const a = ev({ event: 'tool_call', tool_name: 't', insert_id: 'XYZ' });
    expect(eventKey(a)).toBe(eventKey(a));
    // A different event object with the SAME insert_id maps to the SAME key,
    // so expand/collapse state survives a re-render/poll.
    const b = ev({ event: 'tool_result', tool_name: 't', insert_id: 'XYZ' });
    expect(eventKey(b)).toBe(eventKey(a));
  });

  it('produces a synthetic but stable key when insert_id is absent', () => {
    const e = ev({
      event: 'mcp_call',
      mcp_tool: 'search',
      timestamp: '2026-06-02T00:00:00Z',
    });
    const k1 = eventKey(e);
    const k2 = eventKey({ ...e });
    expect(typeof k1).toBe('string');
    expect(k1.length).toBeGreaterThan(0);
    expect(k1).toBe(k2); // deterministic for identical fields
  });

  it('synthetic keys differ for events that differ in their identifying fields', () => {
    const base = { event: 'mcp_call', timestamp: '2026-06-02T00:00:00Z' } as const;
    const k1 = eventKey(ev({ ...base, mcp_tool: 'search' }));
    const k2 = eventKey(ev({ ...base, mcp_tool: 'fetch' }));
    expect(k1).not.toBe(k2);
  });

  it('never produces the bare "evt:" empty namespace for an insert_id-less event', () => {
    // The legacy renderer emitted "" for missing insert_id; here the contract
    // requires a stable synthetic so open-state keys can never collapse to "".
    const k = eventKey(ev({ event: 'llm_thought' }));
    expect(k).not.toBe('evt:');
    expect(k).not.toBe('');
  });
});

describe('Timeline — historical-empty state', () => {
  afterEach(cleanup);

  it('historical + no events: shows only the empty note, suppresses the three group accordions', () => {
    const { getByTestId, queryByText, container } = render(Timeline, {
      props: { events: [], status: 'historical' },
    });
    // The one accurate explanatory note stays.
    expect(getByTestId('timeline-empty')).toBeTruthy();
    // The redundant empty group accordions are gone (no #group-* at all).
    expect(container.querySelector('#group-coordinator')).toBeNull();
    expect(container.querySelector('#group-tools')).toBeNull();
    expect(container.querySelector('#group-mcp')).toBeNull();
    // ...and so is the misleading "No coordinator reasoning yet." placeholder.
    expect(queryByText('No coordinator reasoning yet.')).toBeNull();
  });

  it('historical WITH events: still renders the grouped timeline, no empty note', () => {
    const { queryByTestId, container } = render(Timeline, {
      props: {
        events: [ev({ event: 'llm_thought', thought_text: 'considering drift' })],
        status: 'historical',
      },
    });
    expect(container.querySelector('#group-coordinator')).not.toBeNull();
    expect(queryByTestId('timeline-empty')).toBeNull();
  });

  it('live/pending + no events: keeps the groups + the "yet" placeholder (regression guard)', () => {
    const { getByText, queryByTestId, container } = render(Timeline, {
      props: { events: [], status: 'pending' },
    });
    expect(container.querySelector('#group-coordinator')).not.toBeNull();
    expect(getByText('No coordinator reasoning yet.')).toBeTruthy();
    expect(queryByTestId('timeline-empty')).toBeNull();
  });
});
