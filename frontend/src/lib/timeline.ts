// Event classification + sub-grouping + tool-pairing for the reasoning
// timeline. Ported verbatim (behaviour-for-behaviour) from the legacy inline
// renderer in agent/templates/transparency.html (~1282-1400 + _pairToolEvents
// at ~944, _safeApprovalHref/DOM ids around ~857-1065).
//
// Authoritative binning (plan §3):
//   llm_thought / llm_usage  -> 'coordinator'
//   tool_call  / tool_result -> 'tools'   (sub-grouped by tool_name)
//   mcp_call                 -> 'mcp'     (sub-grouped by mcp_tool || mcp_server)
//   final_response           -> null (skipped; the reply renders as the turn's
//                               own crew bubble, never as a timeline row)
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
 * usage event, or no thinking spent (thoughts_token_count absent/zero, e.g.
 * a directly recorded trace). NOTE: usage events are emitted per LLM step,
 * not once per run — on a multi-step run this can be > 0 between steps while
 * a later step's summaries are still in flight, so the CALLER must also gate
 * on a terminal timeline status before showing the note (Timeline.svelte
 * gates on 'complete' | 'historical').
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

/** One row of the interleaved disclosure/record timeline. */
export type TimelineRow =
  | { kind: 'thought'; key: string; event: TraceEvent }
  | { kind: 'tool'; key: string; call?: TraceEvent; result?: TraceEvent }
  | { kind: 'mcp'; key: string; event: TraceEvent };

/** The timestamp a row sorts by, or '' when the source never stamped one. */
function rowStamp(row: TimelineRow): string {
  if (row.kind === 'tool') return row.call?.timestamp ?? row.result?.timestamp ?? '';
  return row.event.timestamp ?? '';
}

/**
 * Flatten a trace's events into ONE chronological row list for the inline
 * reasoning disclosure and the desk decision record (ds-jns, design §2).
 *
 * `groupEvents` + `pairToolEvents` partition into three sibling panels, which
 * is the page-level Timeline's shape; a disclosure attached to a single message
 * needs the thought → tool_call → tool_result → mcp_call sequence instead.
 *
 * Ordering rules:
 *  - INPUT ORDER is authoritative for same-source events. The SSE stream is
 *    ordered by construction, and GET /trace is sorted server-side by
 *    (timestamp, insert_id) — so re-sorting here would only add ways to be
 *    wrong.
 *  - Each tool pair is anchored at its CALL's position, so a long-running tool
 *    doesn't jump down the list when its result lands. Pairing is FIFO per
 *    `tool_name`, the same rule as `pairToolEvents`. Orphan results stay at
 *    their own position and in-flight calls keep a result-less row: nothing
 *    ever vanishes from the UI.
 *  - `mcp_call` is the one CROSS-SOURCE kind (reconcileBackfill APPENDS it from
 *    /trace), so it gets one best-effort correction: if it carries a timestamp
 *    and some earlier row is stamped LATER, it moves ahead of that row.
 *    Streamed events carry no timestamp at all, so on a live+backfill entry
 *    nothing is comparable and the appended order stands — the same
 *    cross-source caveat reconcileBackfill documents.
 *  - `llm_usage`, `final_response` and unknown kinds emit NO row. llm_usage
 *    still matters (omittedThoughtTokens reads it) — it is filtered here, not
 *    upstream, so the cache keeps the full event list.
 *  - Keys come from `eventKey()`, de-duplicated with `#2`/`#3`… suffixes.
 *    Load-bearing for live streams: streamed events have no `insert_id`, so
 *    every thought chunk in one turn shares ONE synthetic key and Svelte would
 *    throw each_key_duplicate without this.
 */
export function interleaveTimeline(events: TraceEvent[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  // Per-tool_name FIFO of rows still awaiting a result (mirrors pairToolEvents).
  const openCalls = new Map<string, Array<{ kind: 'tool'; key: string; call?: TraceEvent; result?: TraceEvent }>>();
  const seenKeys = new Map<string, number>();

  const uniqueKey = (e: TraceEvent): string => {
    const base = eventKey(e);
    const n = (seenKeys.get(base) ?? 0) + 1;
    seenKeys.set(base, n);
    return n === 1 ? base : `${base}#${n}`;
  };

  for (const e of events) {
    switch (groupOf(e)) {
      case 'coordinator': {
        // llm_usage lives in this group but renders no row.
        if (e.event !== 'llm_thought') break;
        rows.push({ kind: 'thought', key: uniqueKey(e), event: e });
        break;
      }
      case 'tools': {
        const tn = e.tool_name ?? '';
        if (e.event === 'tool_call') {
          const row = { kind: 'tool' as const, key: uniqueKey(e), call: e, result: undefined };
          rows.push(row);
          const q = openCalls.get(tn) ?? [];
          q.push(row);
          openCalls.set(tn, q);
        } else {
          const q = openCalls.get(tn) ?? [];
          const head = q.shift();
          if (head) {
            // The pair stays where the CALL was — do not append a second row.
            head.result = e;
            openCalls.set(tn, q);
          } else {
            rows.push({ kind: 'tool', key: uniqueKey(e), call: undefined, result: e });
          }
        }
        break;
      }
      case 'mcp': {
        const row: TimelineRow = { kind: 'mcp', key: uniqueKey(e), event: e };
        const ts = e.timestamp ?? '';
        // Best-effort cross-source correction — see the doc above.
        const at = ts ? rows.findIndex((r) => rowStamp(r) > ts) : -1;
        if (at === -1) rows.push(row);
        else rows.splice(at, 0, row);
        break;
      }
      default:
        break; // final_response + unknown kinds render nothing
    }
  }
  return rows;
}

/** Longest collapsed-subtitle text before the ellipsis clamp bites. */
const SUBTITLE_MAX = 60;

/**
 * The collapsed reasoning line's subtitle, derived from the LATEST thought
 * chunk (ds-jns, design §2).
 *
 * PRIMARY RULE: the first non-empty line, clamped. The bold-heading strip is
 * an ENHANCEMENT, not a data contract — real Gemini thought summaries often
 * lead with `**Assessing the drift**`, but `thought_text` arrives verbatim
 * from the model and nothing guarantees that shape. When the heading pattern
 * doesn't match, the plain first line is used unchanged.
 *
 * Returns `null` (not a default string) for empty/whitespace input so the
 * COMPONENT picks the static i18n label — locale strings stay out of lib/.
 */
export function deriveThoughtSubtitle(
  thoughtText: string | null | undefined,
): string | null {
  if (!thoughtText) return null;
  const line = thoughtText
    .split('\n')
    .map((l) => l.trim())
    .find((l) => l.length > 0);
  if (!line) return null;
  // Both colon forms: the app's default locale is JA and a summary written in
  // Japanese ends its heading with U+FF1A, not U+003A.
  const m = /^\*\*(.+?)\*\*[:：]?\s*$/.exec(line);
  const text = m ? m[1].trim() : line;
  if (!text) return null;
  return text.length > SUBTITLE_MAX ? `${text.slice(0, SUBTITLE_MAX - 1)}…` : text;
}
