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
// 'historical' is the label for a past decision opened from the rail — it is a
// snapshot replay, NOT a live stream, so it must never derive from /trace's
// `complete` flag (which is false for any trace without a stable final_response,
// e.g. an iac_apply, or any trace on a cold observation cache after a restart).
export type TimelineStatus =
  | 'pending'
  | 'streaming'
  | 'complete'
  | 'stalled'
  | 'error'
  | 'historical';

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
 * Number of logical tool invocations in a tools-group event list.
 *
 * A single tool run emits TWO trace events (a `tool_call` and its
 * `tool_result`), so counting raw events double-counts every resolved call.
 * This collapses each call+result into one via `pairToolEvents`, so the tools
 * group header agrees with the "N call(s)" shown inside each sub-group. Pairing
 * is per-`tool_name`, so running over the whole tools list equals the sum of
 * the per-sub-group pair counts. In-flight calls and orphan results each count
 * as one (they render as singletons and must not vanish from the tally).
 */
export function toolCallCount(events: TraceEvent[]): number {
  return pairToolEvents(events).length;
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

/**
 * Detect the "coordinator reasoned but Vertex omitted the summaries" state.
 *
 * Gemini's reasoning summaries are a best-effort layer: under load, Vertex
 * completes the turn (and bills the thinking) while returning ZERO summary
 * parts, so the coordinator group renders empty and reads as broken. The
 * proof that thinking happened anyway is in the usage events: their
 * `thoughts_token_count` is reported by the serving stack regardless.
 *
 * Returns the summed thinking-token count to cite in the UI note, or 0 when
 * the note must not show: any llm_thought present (summaries arrived), no
 * usage event yet (mid-stream; usage is emitted at the end of the run, so
 * this can never fire while summaries may still arrive), or no thinking
 * spent (thoughts_token_count absent/zero, e.g. a directly recorded trace).
 */
export function omittedThoughtTokens(events: TraceEvent[]): number {
  let tokens = 0;
  for (const e of events) {
    if (e.event === 'llm_thought') return 0;
    if (e.event === 'llm_usage') {
      const v = e.thoughts_token_count;
      if (typeof v === 'number' && v > 0) tokens += v;
    }
  }
  return tokens;
}

/**
 * Reconcile the live-streamed timeline with a post-turn GET /trace snapshot.
 *
 * The live SSE /chat stream already carries every timeline kind the coordinator
 * emits AND renders them as they arrive — EXCEPT `mcp_call`, which is a
 * trace-only side-channel (see TraceEvent). Cloud Logging ingestion lags the
 * stream by seconds, so a /trace fetched immediately after the turn is
 * frequently INCOMPLETE: it may hold a subset of the reasoning, or — at the
 * extreme — only non-timeline log lines (event=None) that ingest first. A
 * naive `events = t.events` therefore REPLACED the complete live timeline with
 * a stale snapshot and wiped the reasoning/tools/mcp the user just watched
 * stream in (the "new chat shows no coordinator reasoning" bug); reopening the
 * conversation later worked only because /trace had fully ingested by then.
 *
 * So we MERGE, never overwrite:
 *   - If the live set has NO displayable timeline event (transport error, or a
 *     non-SSE JSON fallback), there is nothing to protect — trust /trace
 *     wholesale (the recovery path).
 *   - Otherwise KEEP every live event and ADD only the `mcp_call` events the
 *     stream never sent, de-duplicated by eventKey against any mcp already
 *     present AND against earlier mcp in the same fetched snapshot. We
 *     deliberately do NOT merge the other kinds from /trace: the stream is
 *     authoritative for them, and the same logical event is stamped with a
 *     DIFFERENT insert_id on each source (stream-N vs a real Cloud Logging id),
 *     so cross-source de-dup by key is unreliable — re-adding them would
 *     double-count.
 *
 * KNOWN LIMITATION: if the live stream is interrupted AFTER emitting at least
 * one displayable event, the missing non-mcp tail is NOT recovered from /trace
 * (liveHasTimeline is already true). Backfilling it would need the same
 * unreliable cross-source de-dup, and a too-early /trace is itself incomplete;
 * the honest recovery is a brief /trace poll, deferred as a separate change.
 * This function's contract is narrow: never WIPE the live timeline.
 */
export function reconcileBackfill(
  live: TraceEvent[],
  fetched: TraceEvent[],
): TraceEvent[] {
  if (!Array.isArray(fetched) || fetched.length === 0) return live;
  const liveHasTimeline = live.some((e) => groupOf(e) !== null);
  if (!liveHasTimeline) return fetched; // recovery path — nothing to protect
  // Seed with the live mcp keys, then grow the set as we accept each fetched
  // mcp — so duplicate mcp rows WITHIN the /trace snapshot are also collapsed
  // (a repeated insert_id would otherwise yield duplicate Svelte keys).
  const seenMcp = new Set(
    live.filter((e) => groupOf(e) === 'mcp').map(eventKey),
  );
  const additions: TraceEvent[] = [];
  for (const e of fetched) {
    if (groupOf(e) !== 'mcp') continue;
    const k = eventKey(e);
    if (seenMcp.has(k)) continue;
    seenMcp.add(k);
    additions.push(e);
  }
  return additions.length > 0 ? [...live, ...additions] : live;
}
