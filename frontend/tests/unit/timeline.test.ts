import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import {
  groupOf,
  eventKey,
  reconcileBackfill,
  omittedThoughtTokens,
  deriveThoughtSubtitle,
  interleaveTimeline,
  type GroupKey,
  type TraceEvent,
} from '../../src/lib/timeline';

// Re-homes the event-classification + sub-grouping + tool-pairing logic that
// lived inline in agent/templates/transparency.html (~1282-1400 + the
// _pairToolEvents helper at ~944). The binning is authoritative per plan §3:
//   llm_thought / llm_usage  -> coordinator
//   tool_call  / tool_result -> tools   (sub-grouped by tool_name)
//   mcp_call                 -> mcp     (sub-grouped by mcp_tool || mcp_server)
//   final_response           -> null (skipped; the reply renders as the turn's
//                               own crew bubble, never as a timeline row)
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

describe('omittedThoughtTokens — "reasoned but no summaries" detection', () => {
  // Vertex sheds Gemini's reasoning-summary layer under load: the turn
  // completes, usage bills thoughts_token_count > 0, but ZERO llm_thought
  // events arrive — and the coordinator group reads as broken. The helper
  // returns the thinking-token count to cite in the note, or 0 = don't show.

  it('returns the thinking-token count when usage proves thinking but no thought arrived', () => {
    const events = [
      ev({ event: 'llm_usage', thoughts_token_count: 714, total_token_count: 804 }),
    ];
    expect(omittedThoughtTokens(events)).toBe(714);
  });

  it('sums thinking tokens across multiple usage events (multi-step runs)', () => {
    const events = [
      ev({ event: 'llm_usage', thoughts_token_count: 300 }),
      ev({ event: 'tool_call', tool_name: 'read_live_env' }),
      ev({ event: 'llm_usage', thoughts_token_count: 400 }),
    ];
    expect(omittedThoughtTokens(events)).toBe(700);
  });

  it('returns 0 the moment ANY llm_thought is present (summaries arrived)', () => {
    const events = [
      ev({ event: 'llm_thought', thought_text: 'assessing drift' }),
      ev({ event: 'llm_usage', thoughts_token_count: 714 }),
    ];
    expect(omittedThoughtTokens(events)).toBe(0);
  });

  it('returns 0 with no usage event yet (mid-stream; summaries may still arrive)', () => {
    expect(omittedThoughtTokens([ev({ event: 'tool_call', tool_name: 't' })])).toBe(0);
    expect(omittedThoughtTokens([])).toBe(0);
  });

  it('returns 0 when usage carries no thinking (null / 0 / absent thoughts_token_count)', () => {
    expect(
      omittedThoughtTokens([ev({ event: 'llm_usage', thoughts_token_count: null })]),
    ).toBe(0);
    expect(
      omittedThoughtTokens([ev({ event: 'llm_usage', thoughts_token_count: 0 })]),
    ).toBe(0);
    expect(omittedThoughtTokens([ev({ event: 'llm_usage' })])).toBe(0);
  });
});


// ---- deriveThoughtSubtitle (ds-jns Task 1.3) ----
// The collapsed reasoning line's text. First-non-empty-line + clamp is the
// PRIMARY rule; stripping a leading bold heading is an enhancement layered on
// top, because thought_text arrives verbatim from the model and the heading
// shape is a habit of Gemini's summaries, not a contract.
describe('deriveThoughtSubtitle', () => {
  it('strips a leading bold markdown heading', () => {
    expect(deriveThoughtSubtitle('**Assessing the drift**')).toBe('Assessing the drift');
  });

  it('strips a bold heading with a trailing colon', () => {
    expect(deriveThoughtSubtitle('**Assessing the drift**:')).toBe('Assessing the drift');
  });

  it('strips a bold heading with a trailing fullwidth colon (JA is the default locale)', () => {
    expect(deriveThoughtSubtitle('**ドリフトの評価**：')).toBe('ドリフトの評価');
  });

  it('keeps a plain sentence as-is when it fits', () => {
    expect(deriveThoughtSubtitle('Checking the live env against the contract.')).toBe(
      'Checking the live env against the contract.',
    );
  });

  it('clamps a long line to 60 chars including the ellipsis', () => {
    const long = 'x'.repeat(120);
    const out = deriveThoughtSubtitle(long)!;
    expect(out).toHaveLength(60);
    expect(out.endsWith('…')).toBe(true);
    expect(out.slice(0, 59)).toBe('x'.repeat(59));
  });

  it('does not clamp a line of exactly 60 chars', () => {
    const exact = 'y'.repeat(60);
    expect(deriveThoughtSubtitle(exact)).toBe(exact);
  });

  it('uses the FIRST non-empty line of a multi-line chunk', () => {
    expect(deriveThoughtSubtitle('\n\n  **Reading the service**  \nthen the diff\n')).toBe(
      'Reading the service',
    );
  });

  it('returns null for empty, whitespace-only, null and undefined', () => {
    expect(deriveThoughtSubtitle('')).toBeNull();
    expect(deriveThoughtSubtitle('   \n\t\n  ')).toBeNull();
    expect(deriveThoughtSubtitle(null)).toBeNull();
    expect(deriveThoughtSubtitle(undefined)).toBeNull();
  });

  it('returns null for an empty bold heading rather than an empty subtitle', () => {
    // `**  **` matches the heading shape but carries no text; the caller must
    // fall back to the static label, not render a blank line.
    expect(deriveThoughtSubtitle('**  **')).toBeNull();
  });

  it('leaves inline bold alone (only a WHOLE-line heading is a heading)', () => {
    expect(deriveThoughtSubtitle('the **drift** is in PORT')).toBe('the **drift** is in PORT');
  });
});

// ---- interleaveTimeline (ds-jns Task 1.4) ----
// The disclosure/record row model. Timeline.svelte partitions events into three
// sibling panels; a message-attached disclosure needs ONE chronological
// sequence instead. Pure function, so the ordering rules are pinned here rather
// than through a component.
describe('interleaveTimeline', () => {
  const tid = 'a'.repeat(32);
  const e = (over: Partial<TraceEvent>): TraceEvent =>
    ({ event: 'llm_thought', trace_id: tid, ...over }) as TraceEvent;

  it('preserves input order for thought → tool → thought', () => {
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 't1' }),
      e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env' }),
      e({ event: 'llm_thought', insert_id: 't2' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought', 'tool', 'thought']);
  });

  it('anchors a tool pair at its CALL position even when the result lands later', () => {
    const call = e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env' });
    const result = e({ event: 'tool_result', insert_id: 'r1', tool_name: 'read_live_env' });
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 't1' }),
      call,
      e({ event: 'llm_thought', insert_id: 't2' }),
      result,
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought', 'tool', 'thought']);
    const tool = rows[1];
    expect(tool.kind).toBe('tool');
    if (tool.kind === 'tool') {
      expect(tool.call).toBe(call);
      expect(tool.result).toBe(result);
    }
  });

  it('pairs FIFO per tool_name, like pairToolEvents', () => {
    const c1 = e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env' });
    const c2 = e({ event: 'tool_call', insert_id: 'c2', tool_name: 'read_live_env' });
    const r1 = e({ event: 'tool_result', insert_id: 'r1', tool_name: 'read_live_env' });
    const r2 = e({ event: 'tool_result', insert_id: 'r2', tool_name: 'read_live_env' });
    const rows = interleaveTimeline([c1, c2, r1, r2]);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ kind: 'tool', call: c1, result: r1 });
    expect(rows[1]).toMatchObject({ kind: 'tool', call: c2, result: r2 });
  });

  it('keeps an orphan result at its own position (nothing vanishes)', () => {
    const orphan = e({ event: 'tool_result', insert_id: 'r1', tool_name: 'read_live_env' });
    const rows = interleaveTimeline([e({ event: 'llm_thought', insert_id: 't1' }), orphan]);
    expect(rows).toHaveLength(2);
    expect(rows[1]).toMatchObject({ kind: 'tool', call: undefined, result: orphan });
  });

  it('keeps an unmatched in-flight call as a row with no result', () => {
    const call = e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env' });
    const rows = interleaveTimeline([call]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: 'tool', call, result: undefined });
  });

  it('moves an mcp row whose timestamp sorts earlier ahead of the later-stamped rows', () => {
    // reconcileBackfill APPENDS the trace-only mcp side-channel, so a call the
    // coordinator actually made first arrives last in the array.
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 't1', timestamp: '2026-01-01T00:00:03Z' }),
      e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env', timestamp: '2026-01-01T00:00:04Z' }),
      e({ event: 'mcp_call', insert_id: 'm1', mcp_server: 'dev-knowledge', mcp_tool: 'search', timestamp: '2026-01-01T00:00:01Z' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['mcp', 'thought', 'tool']);
  });

  it('inserts an mcp row before the FIRST later-stamped row, not at the very top', () => {
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 't1', timestamp: '2026-01-01T00:00:01Z' }),
      e({ event: 'tool_call', insert_id: 'c1', tool_name: 'read_live_env', timestamp: '2026-01-01T00:00:05Z' }),
      e({ event: 'mcp_call', insert_id: 'm1', mcp_server: 'dev-knowledge', timestamp: '2026-01-01T00:00:03Z' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought', 'mcp', 'tool']);
  });

  it('leaves an mcp row in input order when nothing is stamped later (the live case)', () => {
    // Streamed events carry no timestamp at all — Cloud Logging stamps them on
    // the /trace path. So on a live+backfill entry there IS no later-stamped
    // row and the appended mcp stays where reconcileBackfill put it. This is
    // the documented best-effort limit, pinned so it can't regress silently.
    const rows = interleaveTimeline([
      e({ event: 'llm_thought' }),
      e({ event: 'tool_call', tool_name: 'read_live_env' }),
      e({ event: 'mcp_call', insert_id: 'm1', mcp_server: 'dev-knowledge', timestamp: '2026-01-01T00:00:01Z' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought', 'tool', 'mcp']);
  });

  it('leaves an mcp row without a timestamp in input order', () => {
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 't1', timestamp: '2026-01-01T00:00:09Z' }),
      e({ event: 'mcp_call', insert_id: 'm1', mcp_server: 'dev-knowledge' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought', 'mcp']);
  });

  it('excludes llm_usage, final_response and unknown kinds from the rows', () => {
    const rows = interleaveTimeline([
      e({ event: 'llm_usage', insert_id: 'u1', thoughts_token_count: 500 }),
      e({ event: 'final_response', insert_id: 'f1' }),
      e({ event: 'something_new', insert_id: 'x1' }),
      e({ event: 'llm_thought', insert_id: 't1' }),
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['thought']);
  });

  it('gives duplicate insert_ids distinct keys', () => {
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', insert_id: 'dupe' }),
      e({ event: 'llm_thought', insert_id: 'dupe' }),
      e({ event: 'llm_thought', insert_id: 'dupe' }),
    ]);
    expect(new Set(rows.map((r) => r.key)).size).toBe(3);
    expect(rows[0].key).toBe(eventKey(e({ event: 'llm_thought', insert_id: 'dupe' })));
    expect(rows[1].key).toContain('#2');
    expect(rows[2].key).toContain('#3');
  });

  it('gives every streamed thought a distinct key (they share one synthetic key)', () => {
    // Live events carry NO insert_id, so eventKey() falls back to a synthetic
    // built from (event, trace_id, timestamp, …) — identical for every thought
    // chunk in one stream. Without de-duplication Svelte throws
    // each_key_duplicate on the very first multi-thought turn.
    const rows = interleaveTimeline([
      e({ event: 'llm_thought', thought_text: 'first' }),
      e({ event: 'llm_thought', thought_text: 'second' }),
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].key).not.toBe(rows[1].key);
  });

  it('returns an empty list for no events', () => {
    expect(interleaveTimeline([])).toEqual([]);
  });
});
