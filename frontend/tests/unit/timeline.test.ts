import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import {
  groupOf,
  subKey,
  groupEvents,
  pairToolEvents,
  toolCallCount,
  eventKey,
  reconcileBackfill,
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

describe('toolCallCount — logical tool invocations, not raw events', () => {
  it('counts a call+result pair as ONE call (not two raw events)', () => {
    const call = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c1' });
    const result = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r1' });
    // The bug: the tools header counted raw events (2) while the expanded
    // sub-group counted pairs (1 call). The header must agree with the inside.
    expect(toolCallCount([call, result])).toBe(1);
  });

  it('sums pairs across different tool_names (matches sum of sub-group pills)', () => {
    const events = [
      ev({ event: 'tool_call', tool_name: 'a', insert_id: 'ca' }),
      ev({ event: 'tool_result', tool_name: 'a', insert_id: 'ra' }),
      ev({ event: 'tool_call', tool_name: 'b', insert_id: 'cb' }),
      ev({ event: 'tool_result', tool_name: 'b', insert_id: 'rb' }),
    ];
    expect(toolCallCount(events)).toBe(2);
  });

  it('counts an in-flight call (no result yet) as one call', () => {
    const call = ev({ event: 'tool_call', tool_name: 't', insert_id: 'c1' });
    expect(toolCallCount([call])).toBe(1);
  });

  it('counts an orphan result (call out of window) as one call', () => {
    const result = ev({ event: 'tool_result', tool_name: 't', insert_id: 'r1' });
    expect(toolCallCount([result])).toBe(1);
  });

  it('returns 0 for an empty list', () => {
    expect(toolCallCount([])).toBe(0);
  });
});

describe('Timeline — tools header pill counts calls, not raw events', () => {
  afterEach(cleanup);

  it('shows "1" on the tools group header for a single completed call (was "2")', () => {
    const events = [
      ev({ event: 'tool_call', tool_name: 'read_drift', insert_id: 'c1' }),
      ev({ event: 'tool_result', tool_name: 'read_drift', insert_id: 'r1' }),
    ];
    const { container } = render(Timeline, {
      props: { events, status: 'historical' },
    });
    const pill = container.querySelector('#group-tools .group__count');
    expect(pill?.textContent?.trim()).toBe('1');
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

  it('historical + no events, NOT directly-recorded: says the trace could not be loaded, not that no reasoning ran', () => {
    // The default (a chat turn / reasoning decision). Copy must NOT claim the
    // turn was "recorded directly" — the reasoning happened, it just could not
    // be fetched (e.g. beyond the log lookback window).
    const { getByTestId } = render(Timeline, {
      props: { events: [], status: 'historical' },
    });
    const note = getByTestId('timeline-empty');
    expect(note.textContent).toContain("couldn't be loaded");
    expect(note.textContent).not.toContain('recorded directly');
  });

  it('historical + no events, directlyRecorded: keeps the accurate "recorded directly" copy', () => {
    // The iac_apply case: legitimately no coordinator reasoning run.
    const { getByTestId } = render(Timeline, {
      props: { events: [], status: 'historical', directlyRecorded: true },
    });
    expect(getByTestId('timeline-empty').textContent).toContain('recorded directly');
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

  it('streaming + no events: keeps the groups (live-chat waiting path regression guard)', () => {
    // Suppression must stay gated on 'historical' only — the live chat column
    // streams into empty groups while events arrive.
    const { queryByTestId, container } = render(Timeline, {
      props: { events: [], status: 'streaming' },
    });
    expect(container.querySelector('#group-coordinator')).not.toBeNull();
    expect(queryByTestId('timeline-empty')).toBeNull();
  });
});

// --- reconcileBackfill — merge /trace into the live timeline, never overwrite //
//
// Regression cover for the "new chat shows no coordinator reasoning / tools /
// mcp" bug. The live SSE stream carries every timeline kind EXCEPT the
// trace-only `mcp_call` side-channel, and it renders those events as they
// arrive. The post-turn GET /trace backfill used to REPLACE the live event set
// (`events = t.events`). Cloud Logging ingestion lags the stream by seconds, so
// that /trace snapshot is frequently incomplete — at the extreme it holds only
// non-timeline log lines (event=None) that still pass a `length > 0` guard — so
// the replace wiped the reasoning the user just watched stream in. Reopening
// later worked because /trace had fully ingested by then. reconcileBackfill
// keeps the live timeline and only ADDS the trace-only mcp_call events.

// A Cloud Logging "log line": no `event` field, so groupOf() bins it to null.
// These ingest BEFORE the timeline events, so a too-early /trace holds only
// these (n > 0, but zero displayable timeline events).
function logLine(insertId: string): TraceEvent {
  return {
    trace_id: 'a'.repeat(32),
    insert_id: insertId,
    level: 'info',
    logger: 'agent',
    msg: 'coordinator turn',
  } as unknown as TraceEvent;
}

describe('reconcileBackfill — merge /trace into live timeline (never overwrite)', () => {
  it('keeps the live reasoning when /trace holds only ingestion-lagged log lines', () => {
    // THE BUG: live streamed a thought + a resolved tool call; the immediate
    // /trace has only log lines (timeline events not yet ingested).
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
      ev({ event: 'tool_call', insert_id: 'stream-1', tool_name: 'read_live_env' }),
      ev({ event: 'tool_result', insert_id: 'stream-2', tool_name: 'read_live_env' }),
    ];
    const fetched: TraceEvent[] = [logLine('log-0'), logLine('log-1'), logLine('log-2')];

    const out = reconcileBackfill(live, fetched);

    // The live timeline survives intact — nothing dropped, nothing from the
    // stale snapshot swapped in.
    expect(out).toEqual(live);
    expect(out.filter((e) => groupOf(e) === 'coordinator')).toHaveLength(1);
    expect(out.filter((e) => groupOf(e) === 'tools')).toHaveLength(2);
  });

  it('adds the trace-only mcp_call events the stream never carried', () => {
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
      ev({ event: 'tool_call', insert_id: 'stream-1', tool_name: 'read_live_env' }),
      ev({ event: 'tool_result', insert_id: 'stream-2', tool_name: 'read_live_env' }),
    ];
    // /trace mirrors the stream (with REAL Cloud Logging insert_ids) plus the
    // mcp_call side-channel the SSE stream does not emit.
    const fetched: TraceEvent[] = [
      logLine('log-0'),
      ev({ event: 'llm_thought', insert_id: 'ci-0', thought_text: 'assessing drift' }),
      ev({ event: 'tool_call', insert_id: 'ci-1', tool_name: 'read_live_env' }),
      ev({ event: 'tool_result', insert_id: 'ci-2', tool_name: 'read_live_env' }),
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs', mcp_server: 'ctx7' }),
    ];

    const out = reconcileBackfill(live, fetched);

    // Every live event is preserved, the mcp_call is appended, and the
    // reasoning/tools are NOT duplicated from the /trace copy (which carries
    // different insert_ids for the same logical events).
    expect(out.filter((e) => groupOf(e) === 'coordinator')).toHaveLength(1);
    expect(out.filter((e) => groupOf(e) === 'tools')).toHaveLength(2);
    const mcp = out.filter((e) => groupOf(e) === 'mcp');
    expect(mcp).toHaveLength(1);
    expect(mcp[0].mcp_tool).toBe('search_docs');
  });

  it('falls back to the fetched trace when the live stream produced no timeline events (recovery)', () => {
    // Transport error / non-SSE fallback: the stream carried nothing
    // displayable, so there is nothing to protect — trust /trace wholesale.
    const live: TraceEvent[] = [logLine('log-0')]; // e.g. only a stray log line
    const fetched: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'ci-0', thought_text: 'recovered' }),
      ev({ event: 'tool_call', insert_id: 'ci-1', tool_name: 'read_live_env' }),
      ev({ event: 'tool_result', insert_id: 'ci-2', tool_name: 'read_live_env' }),
    ];

    const out = reconcileBackfill(live, fetched);
    expect(out).toEqual(fetched);
  });

  it('returns the live events unchanged when /trace is empty', () => {
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
    ];
    expect(reconcileBackfill(live, [])).toEqual(live);
  });

  it('does not re-add an mcp_call already present in the live events (dedup by key)', () => {
    // Defensive: if an mcp event ever reaches the live set, the same event in
    // /trace (identical insert_id) must not be duplicated.
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs' }),
    ];
    const fetched: TraceEvent[] = [
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs' }),
    ];
    const out = reconcileBackfill(live, fetched);
    expect(out.filter((e) => groupOf(e) === 'mcp')).toHaveLength(1);
    expect(out).toEqual(live);
  });

  it('collapses duplicate mcp_call rows WITHIN the fetched trace (no dup keys)', () => {
    // A repeated insert_id inside one /trace snapshot must not append twice —
    // that would produce duplicate keys in the keyed Svelte timeline loop.
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
    ];
    const fetched: TraceEvent[] = [
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs' }),
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs' }),
      ev({ event: 'mcp_call', insert_id: 'ci-4', mcp_tool: 'read_doc' }),
    ];
    const out = reconcileBackfill(live, fetched);
    const mcp = out.filter((e) => groupOf(e) === 'mcp');
    expect(mcp).toHaveLength(2); // ci-3 once, ci-4 once
    const keys = mcp.map(eventKey);
    expect(new Set(keys).size).toBe(keys.length); // no duplicate keys
  });

  it('does NOT recover the non-mcp tail from /trace after a partial live stream (documented limitation)', () => {
    // If the stream broke after emitting >=1 displayable event, liveHasTimeline
    // is already true, so we only import the trace-only mcp_call side-channel —
    // NOT the missing thought/tool tail (which would need unreliable
    // cross-source de-dup against an itself-incomplete /trace). Contract: never
    // WIPE the live timeline. Recovering the tail is a separate /trace-poll job.
    const live: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'stream-0', thought_text: 'assessing drift' }),
    ];
    const fetched: TraceEvent[] = [
      ev({ event: 'llm_thought', insert_id: 'ci-0', thought_text: 'assessing drift' }),
      ev({ event: 'tool_call', insert_id: 'ci-1', tool_name: 'read_live_env' }),
      ev({ event: 'tool_result', insert_id: 'ci-2', tool_name: 'read_live_env' }),
      ev({ event: 'mcp_call', insert_id: 'ci-3', mcp_tool: 'search_docs' }),
    ];
    const out = reconcileBackfill(live, fetched);
    // The one live thought is kept; the mcp_call is added; the tool tail is not.
    expect(out.filter((e) => groupOf(e) === 'coordinator')).toHaveLength(1);
    expect(out.filter((e) => groupOf(e) === 'tools')).toHaveLength(0);
    expect(out.filter((e) => groupOf(e) === 'mcp')).toHaveLength(1);
  });
});
