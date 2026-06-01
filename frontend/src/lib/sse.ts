// SSE frame parsing + consumption for POST /chat.
//
// The /chat endpoint (Accept: text/event-stream) emits, in order:
//   event: meta   data: {trace_id}
//   (data-only)   data: {<ChatEvent>}        — one timeline event per frame
//   event: done   data: {reply, tool_calls[], session_id}   (terminal)
//   event: error  data: {detail, status_hint?}              (terminal alt)
// with `: keepalive` comment frames on idle. See the plan §3 lib/sse.ts +
// Appendix A. `mcp_call` is intentionally NOT in ChatEvent — it is a
// trace-only side-channel kind (see lib/timeline.ts TraceEvent).

export type ChatEvent =
  | {
      event: 'llm_thought';
      trace_id: string;
      workload: string;
      thought_text: string;
    }
  | {
      event: 'tool_call';
      trace_id: string;
      workload: string;
      tool_name: string;
      tool_args: Record<string, unknown>;
    }
  | {
      event: 'tool_result';
      trace_id: string;
      workload: string;
      tool_name: string;
      result_preview: string;
      result_ok: boolean;
      latency_ms?: number | null;
    }
  | {
      event: 'llm_usage';
      trace_id: string;
      workload: string;
      prompt_token_count: number | null;
      candidates_token_count: number | null;
      thoughts_token_count: number | null;
      total_token_count: number | null;
    }
  | {
      event: 'final_response';
      trace_id: string;
      workload: string;
      response_preview: string;
      response_kind: 'json' | 'text';
    };

export interface ChatMeta {
  trace_id: string;
}

export interface ChatDone {
  reply: string;
  tool_calls: string[];
  session_id: string;
}

export interface ChatError {
  detail: string;
  status_hint?: number;
}

export interface SseHandlers {
  onMeta(m: ChatMeta): void;
  onEvent(e: ChatEvent): void;
  onDone(d: ChatDone): void;
  onError(e: ChatError): void;
}

interface ParsedFrame {
  event?: string;
  data: string;
}

/**
 * Pure SSE frame parser.
 *
 * Splits `buffer` on blank-line frame boundaries (`\n\n`). Within each frame,
 * reads the optional `event:` line and concatenates any number of `data:`
 * lines (joined with `\n`, per the SSE spec). Lines starting with `:` are
 * comments (keepalives) and are ignored. Frames that carry no `data:` line at
 * all (e.g. a comment-only keepalive frame) are dropped. The trailing
 * incomplete chunk (after the last `\n\n`) is returned as `rest` for the
 * caller to prepend to the next read.
 */
export function parseSseFrames(buffer: string): {
  frames: ParsedFrame[];
  rest: string;
} {
  const frames: ParsedFrame[] = [];
  let rest = buffer;
  let idx: number;

  while ((idx = rest.indexOf('\n\n')) !== -1) {
    const block = rest.slice(0, idx);
    rest = rest.slice(idx + 2);
    const frame = parseBlock(block);
    if (frame) frames.push(frame);
  }

  return { frames, rest };
}

/** Parse one frame block (text between blank-line boundaries). Returns null
 *  if the block contains no `data:` line (e.g. a comment-only keepalive). */
function parseBlock(block: string): ParsedFrame | null {
  let event: string | undefined;
  const dataLines: string[] = [];

  for (const rawLine of block.split('\n')) {
    // Normalize a possible trailing CR (CRLF transports).
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith(':')) continue; // comment / keepalive — ignore
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      // Per spec: a single leading space after the colon is stripped.
      let value = line.slice('data:'.length);
      if (value.startsWith(' ')) value = value.slice(1);
      dataLines.push(value);
    }
  }

  if (dataLines.length === 0) return null;
  const data = dataLines.join('\n');
  return event !== undefined ? { event, data } : { data };
}

/** Safely JSON.parse; returns undefined on failure so callers can skip. */
function tryParse<T>(data: string): T | undefined {
  try {
    return JSON.parse(data) as T;
  } catch {
    return undefined;
  }
}

/** Dispatch one parsed frame to the appropriate handler, tolerating bad JSON. */
function dispatch(frame: ParsedFrame, h: SseHandlers): void {
  if (frame.event === 'meta') {
    const m = tryParse<ChatMeta>(frame.data);
    if (m !== undefined) h.onMeta(m);
  } else if (frame.event === 'done') {
    const d = tryParse<ChatDone>(frame.data);
    if (d !== undefined) h.onDone(d);
  } else if (frame.event === 'error') {
    const e = tryParse<ChatError>(frame.data);
    if (e !== undefined) h.onError(e);
  } else {
    // No event name (or an unknown one): treat as a data-only timeline event.
    const e = tryParse<ChatEvent>(frame.data);
    if (e !== undefined) h.onEvent(e);
  }
}

/**
 * Consume a fetch `Response` whose body is a `text/event-stream`, parsing
 * frames incrementally and dispatching each to `h`. Reads the body via a
 * `ReadableStream` reader + `TextDecoder`, buffering across chunk boundaries.
 * A trailing frame missing its final `\n\n` delimiter is flushed at EOF.
 * Returns once the stream is exhausted. Tolerates a null body (no-op).
 */
export async function consumeSse(resp: Response, h: SseHandlers): Promise<void> {
  const body = resp.body;
  if (!body) return;

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = '';

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseFrames(buf);
      buf = rest;
      for (const frame of frames) dispatch(frame, h);
    }

    // Flush any decoder tail + a trailing frame that lacked its delimiter.
    buf += decoder.decode();
    if (buf.trim() !== '') {
      const frame = parseBlock(buf);
      if (frame) dispatch(frame, h);
    }
  } finally {
    // Real DOM readers always expose releaseLock(); guard so a minimal
    // test/double reader (or a non-standard polyfill) doesn't crash teardown.
    reader.releaseLock?.();
  }
}
