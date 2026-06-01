// Event classification + sub-grouping + tool-pairing for the reasoning
// timeline. Ported verbatim (behaviour-for-behaviour) from the legacy inline
// renderer in agent/templates/transparency.html (~1282-1400 + _pairToolEvents
// at ~944, _safeApprovalHref/DOM ids around ~857-1065).
//
// Authoritative binning (plan §3):
//   llm_thought / llm_usage  -> 'coordinator'
//   tool_call  / tool_result -> 'tools'   (sub-grouped by tool_name)
//   mcp_call                 -> 'mcp'     (sub-grouped by mcp_tool || mcp_server)
//   final_response           -> null (skipped; rendered in #final-response-card)
//   unknown                  -> null (dropped — the server is authoritative)
//
// CRITICAL: MCP routing is by `event === 'mcp_call'`, NOT by any tool-name
// prefix. The old MCP_TOOL_PREFIXES heuristic is intentionally removed.

export type GroupKey = 'coordinator' | 'tools' | 'mcp';

// TraceEvent is the superset of the SSE stream events plus the trace-only
// `mcp_call` side-channel kind (which appears only in GET /trace). Extra
// per-kind fields (tool_args, result_preview, latency_ms, doc_count, …) flow
// through via the Record<string, unknown> index signature.
export interface TraceEvent extends Record<string, unknown> {
  event: string;
  trace_id: string;
  workload?: string;
  insert_id?: string;
  timestamp?: string;
  tool_name?: string; // tool_call / tool_result
  mcp_tool?: string; // mcp_call
  mcp_server?: string; // mcp_call
}

// Lifecycle state of the timeline (consumed by TraceBadge / status pill).
export type TimelineStatus = 'pending' | 'streaming' | 'complete' | 'stalled' | 'error';

/**
 * Classify a single event into its display group.
 * Returns null for `final_response` (rendered separately) and for any unknown
 * event kind (silently dropped).
 */
export function groupOf(e: TraceEvent): GroupKey | null {
  switch (e.event) {
    case 'llm_thought':
    case 'llm_usage':
      return 'coordinator';
    case 'tool_call':
    case 'tool_result':
      return 'tools';
    case 'mcp_call':
      return 'mcp';
    default:
      // final_response (sentinel) and unknown kinds both fall through to null.
      return null;
  }
}

/**
 * Sub-group key for an event within its group:
 *   tools -> tool_name || '(unknown)'
 *   mcp   -> mcp_tool || mcp_server || '(unknown)'
 * For coordinator events (no sub-grouping) we still return a stable string so
 * the function is total; callers only use it for tools/mcp.
 */
export function subKey(e: TraceEvent): string {
  if (e.event === 'mcp_call') {
    return e.mcp_tool || e.mcp_server || '(unknown)';
  }
  // tool_call / tool_result (and any other caller use) key by tool_name.
  return e.tool_name || '(unknown)';
}

/**
 * Partition a mixed event list into the three display groups, preserving the
 * incoming (chronological) order within each group. final_response and unknown
 * kinds are dropped. Always returns all three keys (possibly empty arrays).
 */
export function groupEvents(events: TraceEvent[]): Record<GroupKey, TraceEvent[]> {
  const out: Record<GroupKey, TraceEvent[]> = {
    coordinator: [],
    tools: [],
    mcp: [],
  };
  for (const e of events) {
    const g = groupOf(e);
    if (g !== null) {
      out[g].push(e);
    }
  }
  return out;
}

/**
 * Pair tool_call -> next tool_result with the SAME tool_name (FIFO per tool),
 * a single forward pass with a per-tool "open call" queue. Unmatched calls
 * (in-flight) and orphan results (call out of window) are still emitted as
 * singletons so nothing vanishes from the UI.
 *
 * Faithful port of legacy `_pairToolEvents` (transparency.html ~944). A call
 * has `result` undefined; an orphan result has `call` null.
 */
export function pairToolEvents(
  events: TraceEvent[],
): Array<{ call?: TraceEvent; result?: TraceEvent }> {
  const pending = new Map<string, Array<{ call?: TraceEvent; result?: TraceEvent }>>();
  const pairs: Array<{ call?: TraceEvent; result?: TraceEvent }> = [];

  for (const e of events) {
    const tn = e.tool_name ?? '';
    if (e.event === 'tool_call') {
      const q = pending.get(tn) ?? [];
      q.push({ call: e, result: undefined });
      pending.set(tn, q);
    } else if (e.event === 'tool_result') {
      const q = pending.get(tn) ?? [];
      const head = q.shift();
      if (head) {
        head.result = e;
        pairs.push(head);
        pending.set(tn, q);
      } else {
        // Orphan result (the matching call is out of the polling window).
        pairs.push({ call: undefined, result: e });
      }
    }
    // Non tool_call/tool_result events are ignored here; callers feed this
    // only the tools-group events for a single sub-group.
  }

  // Flush remaining un-resulted (in-flight) calls.
  for (const q of pending.values()) {
    for (const p of q) pairs.push(p);
  }
  return pairs;
}

/**
 * Stable per-event DOM/open-state key.
 *   - "evt:" + insert_id when insert_id is present (legacy namespace, so it
 *     can never collide with sub-group "sub:" ids — transparency.html ~865).
 *   - a deterministic synthetic key derived from the event's identifying
 *     fields when insert_id is absent, so expand/collapse state survives a
 *     re-render even for events the server didn't stamp with an insert_id.
 *     (The legacy renderer used "" here, which the §3 contract upgrades to a
 *     stable synthetic.)
 */
export function eventKey(e: TraceEvent): string {
  if (e.insert_id) {
    return 'evt:' + e.insert_id;
  }
  // Synthetic, stable for identical identifying fields. Join with a delimiter
  // unlikely to appear in the values to avoid accidental collisions.
  const parts = [
    e.event ?? '',
    e.trace_id ?? '',
    e.timestamp ?? '',
    e.tool_name ?? '',
    e.mcp_tool ?? '',
    e.mcp_server ?? '',
  ];
  return 'evt:syn:' + parts.join('␟'); // U+241F SYMBOL FOR UNIT SEPARATOR
}
