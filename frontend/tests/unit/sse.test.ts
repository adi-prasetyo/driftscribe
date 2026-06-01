import { describe, it, expect, vi } from 'vitest';
import {
  parseSseFrames,
  consumeSse,
  type ChatEvent,
  type ChatMeta,
  type ChatDone,
  type ChatError,
  type SseHandlers,
} from '../../src/lib/sse';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a Response whose body is a ReadableStream of the encoded SSE text,
 *  optionally split into multiple chunks to exercise cross-chunk buffering. */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  // Cast: in jsdom, Response accepts a ReadableStream body.
  return new Response(stream as unknown as BodyInit);
}

function recordingHandlers(): {
  h: SseHandlers;
  calls: Array<['meta', ChatMeta] | ['event', ChatEvent] | ['done', ChatDone] | ['error', ChatError]>;
} {
  const calls: Array<['meta', ChatMeta] | ['event', ChatEvent] | ['done', ChatDone] | ['error', ChatError]> = [];
  const h: SseHandlers = {
    onMeta: (m) => calls.push(['meta', m]),
    onEvent: (e) => calls.push(['event', e]),
    onDone: (d) => calls.push(['done', d]),
    onError: (e) => calls.push(['error', e]),
  };
  return { h, calls };
}

// ---------------------------------------------------------------------------
// parseSseFrames — pure parser
// ---------------------------------------------------------------------------

describe('parseSseFrames', () => {
  it('parses a single complete data-only frame', () => {
    const buf = 'data: {"event":"llm_thought"}\n\n';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([{ data: '{"event":"llm_thought"}' }]);
    expect(rest).toBe('');
  });

  it('parses a named event frame (event: + data:)', () => {
    const buf = 'event: meta\ndata: {"trace_id":"abc"}\n\n';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([{ event: 'meta', data: '{"trace_id":"abc"}' }]);
    expect(rest).toBe('');
  });

  it('parses multiple frames in one buffer in order', () => {
    const buf =
      'event: meta\ndata: {"trace_id":"t"}\n\n' +
      'data: {"event":"tool_call"}\n\n' +
      'event: done\ndata: {"reply":"hi"}\n\n';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([
      { event: 'meta', data: '{"trace_id":"t"}' },
      { data: '{"event":"tool_call"}' },
      { event: 'done', data: '{"reply":"hi"}' },
    ]);
    expect(rest).toBe('');
  });

  it('returns the trailing incomplete frame as rest (partial tail)', () => {
    const buf = 'data: {"event":"a"}\n\ndata: {"event":"b"';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([{ data: '{"event":"a"}' }]);
    expect(rest).toBe('data: {"event":"b"');
  });

  it('skips keepalive comment lines starting with ":"', () => {
    const buf = ': keepalive\n\ndata: {"event":"x"}\n\n';
    const { frames, rest } = parseSseFrames(buf);
    // The comment-only frame is dropped (no data line); only the real frame remains.
    expect(frames).toEqual([{ data: '{"event":"x"}' }]);
    expect(rest).toBe('');
  });

  it('drops a comment line mixed inside an otherwise-valid frame but keeps data', () => {
    const buf = ': hb\nevent: meta\ndata: {"trace_id":"z"}\n\n';
    const { frames } = parseSseFrames(buf);
    expect(frames).toEqual([{ event: 'meta', data: '{"trace_id":"z"}' }]);
  });

  it('concatenates multiple data: lines within one frame', () => {
    const buf = 'data: {"a":1,\ndata: "b":2}\n\n';
    const { frames } = parseSseFrames(buf);
    // Per SSE spec, multiple data lines are joined with newlines.
    expect(frames).toEqual([{ data: '{"a":1,\n"b":2}' }]);
  });

  it('emits a frame with no data line as undefined-data (caller skips it)', () => {
    // A frame consisting solely of a comment between blank lines yields no
    // data line; it should NOT appear as a parsed data frame.
    const buf = ':only-comment\n\n';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([]);
    expect(rest).toBe('');
  });

  it('handles an empty buffer', () => {
    expect(parseSseFrames('')).toEqual({ frames: [], rest: '' });
  });

  it('preserves a leading partial frame as rest when no boundary yet', () => {
    const buf = 'event: meta\ndata: {"trace_id":"partial"}';
    const { frames, rest } = parseSseFrames(buf);
    expect(frames).toEqual([]);
    expect(rest).toBe(buf);
  });
});

// ---------------------------------------------------------------------------
// consumeSse — stream consumption + dispatch
// ---------------------------------------------------------------------------

describe('consumeSse', () => {
  it('dispatches meta, several events, then done in order', async () => {
    const sse =
      'event: meta\ndata: {"trace_id":"trace-123"}\n\n' +
      'data: {"event":"llm_thought","trace_id":"trace-123","workload":"drift","thought_text":"thinking"}\n\n' +
      'data: {"event":"tool_call","trace_id":"trace-123","workload":"drift","tool_name":"read_live_env_tool","tool_args":{}}\n\n' +
      'data: {"event":"final_response","trace_id":"trace-123","workload":"drift","response_preview":"ok","response_kind":"text"}\n\n' +
      'event: done\ndata: {"reply":"all done","tool_calls":["read_live_env_tool"],"session_id":"s1"}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'event', 'event', 'event', 'done']);

    const meta = calls[0][1] as ChatMeta;
    expect(meta.trace_id).toBe('trace-123');

    const first = calls[1][1] as ChatEvent;
    expect(first.event).toBe('llm_thought');

    const done = calls[4][1] as ChatDone;
    expect(done).toEqual({ reply: 'all done', tool_calls: ['read_live_env_tool'], session_id: 's1' });
  });

  it('reassembles frames split across multiple stream chunks', async () => {
    const part1 = 'event: meta\ndata: {"trace_id":"t"}\n\nda';
    const part2 = 'ta: {"event":"tool_result","trace_id":"t","workload":"drift","tool_name":"x","result_preview":"p","result_ok":true}\n';
    const part3 = '\nevent: done\ndata: {"reply":"r","tool_calls":[],"session_id":"s"}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([part1, part2, part3]), h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'event', 'done']);
    const evt = calls[1][1] as ChatEvent;
    expect(evt.event).toBe('tool_result');
    if (evt.event === 'tool_result') {
      expect(evt.result_ok).toBe(true);
      expect(evt.tool_name).toBe('x');
    }
  });

  it('dispatches an error frame via onError', async () => {
    const sse =
      'event: meta\ndata: {"trace_id":"t"}\n\n' +
      'event: error\ndata: {"detail":"boom","status_hint":503}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'error']);
    const err = calls[1][1] as ChatError;
    expect(err).toEqual({ detail: 'boom', status_hint: 503 });
  });

  it('ignores keepalive comment frames while streaming', async () => {
    const sse =
      ': keepalive\n\n' +
      'event: meta\ndata: {"trace_id":"t"}\n\n' +
      ': keepalive\n\n' +
      'data: {"event":"llm_usage","trace_id":"t","workload":"drift","prompt_token_count":1,"candidates_token_count":2,"thoughts_token_count":0,"total_token_count":3}\n\n' +
      'event: done\ndata: {"reply":"r","tool_calls":[],"session_id":"s"}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'event', 'done']);
    const evt = calls[1][1] as ChatEvent;
    expect(evt.event).toBe('llm_usage');
  });

  it('skips frames whose JSON fails to parse (graceful tolerance)', async () => {
    const sse =
      'event: meta\ndata: {"trace_id":"t"}\n\n' +
      'data: {not valid json\n\n' +
      'data: {"event":"llm_thought","trace_id":"t","workload":"drift","thought_text":"ok"}\n\n' +
      'event: done\ndata: {"reply":"r","tool_calls":[],"session_id":"s"}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    // The malformed frame is silently dropped; the good ones still flow.
    expect(calls.map((c) => c[0])).toEqual(['meta', 'event', 'done']);
  });

  it('flushes a trailing frame that lacks the final blank-line delimiter', async () => {
    const sse =
      'event: meta\ndata: {"trace_id":"t"}\n\n' +
      'event: done\ndata: {"reply":"tail","tool_calls":[],"session_id":"s"}';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'done']);
    const done = calls[1][1] as ChatDone;
    expect(done.reply).toBe('tail');
  });

  it('skips a meta frame whose JSON is malformed without throwing', async () => {
    const sse =
      'event: meta\ndata: {bad\n\n' +
      'event: done\ndata: {"reply":"r","tool_calls":[],"session_id":"s"}\n\n';

    const { h, calls } = recordingHandlers();
    await consumeSse(sseResponse([sse]), h);

    expect(calls.map((c) => c[0])).toEqual(['done']);
  });

  it('does nothing (no throw) when the response has no body', async () => {
    const { h, calls } = recordingHandlers();
    // A 204-style response carries a null body.
    const resp = new Response(null, { status: 204 });
    await expect(consumeSse(resp, h)).resolves.toBeUndefined();
    expect(calls).toEqual([]);
  });

  it('uses a manually constructed multi-event stream and preserves event payloads', async () => {
    const reader = {
      _chunks: [
        'event: meta\ndata: {"trace_id":"m"}\n\n',
        'data: {"event":"tool_call","trace_id":"m","workload":"explore","tool_name":"developer_knowledge","tool_args":{"q":"x"}}\n\n',
        'event: done\ndata: {"reply":"done","tool_calls":["developer_knowledge"],"session_id":"sx"}\n\n',
      ],
      _i: 0,
      read() {
        const enc = new TextEncoder();
        if (this._i < this._chunks.length) {
          return Promise.resolve({ done: false, value: enc.encode(this._chunks[this._i++]) });
        }
        return Promise.resolve({ done: true, value: undefined });
      },
    };
    const fakeResp = {
      body: { getReader: () => reader },
    } as unknown as Response;

    const { h, calls } = recordingHandlers();
    await consumeSse(fakeResp, h);

    expect(calls.map((c) => c[0])).toEqual(['meta', 'event', 'done']);
    const evt = calls[1][1] as ChatEvent;
    if (evt.event === 'tool_call') {
      expect(evt.tool_args).toEqual({ q: 'x' });
    }
  });
});
