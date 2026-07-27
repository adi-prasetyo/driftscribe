import { describe, it, expect } from 'vitest';
import {
  reasoningTraceFromSearch,
  conversationIdFromSearch,
  viewFromSearch,
  DEFAULT_VIEW,
} from '../../src/lib/deeplink';

const HEX32 = 'eba334f9211d46cabc79e50ed200a5a1'; // 32 lowercase hex
const CONV = '7f3b9c2a-1d4e-4a8b-9c0f-2e5a6b7c8d90'; // UUID4-shaped conversation id

describe('reasoningTraceFromSearch', () => {
  it('returns a well-formed 32-char lowercase-hex trace id', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32}`)).toBe(HEX32);
  });

  it('ignores other params and reads only reasoning', () => {
    expect(reasoningTraceFromSearch(`?preview_pr=12&reasoning=${HEX32}&x=1`)).toBe(HEX32);
  });

  it('is null when the param is absent', () => {
    expect(reasoningTraceFromSearch('?preview_pr=12')).toBeNull();
    expect(reasoningTraceFromSearch('')).toBeNull();
  });

  it('is null on the wrong length (backend 400s these)', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32.slice(0, 31)}`)).toBeNull();
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32}a`)).toBeNull();
  });

  it('is null on uppercase hex (canonical id is lowercase only)', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32.toUpperCase()}`)).toBeNull();
  });

  it('is null on non-hex / path-y / empty junk', () => {
    expect(reasoningTraceFromSearch('?reasoning=not-a-trace-id')).toBeNull();
    expect(reasoningTraceFromSearch('?reasoning=../../etc/passwd')).toBeNull();
    expect(reasoningTraceFromSearch('?reasoning=')).toBeNull();
  });
});

describe('conversationIdFromSearch', () => {
  it('returns a well-formed id', () => {
    expect(conversationIdFromSearch(`?conversation=${CONV}`)).toBe(CONV);
  });

  it('ignores other params and reads only conversation', () => {
    expect(conversationIdFromSearch(`?reasoning=${HEX32}&conversation=${CONV}`)).toBe(CONV);
  });

  it('mirrors: reasoningTraceFromSearch ignores conversation and reads only reasoning', () => {
    expect(reasoningTraceFromSearch(`?reasoning=${HEX32}&conversation=${CONV}`)).toBe(HEX32);
  });

  it('is null when the param is absent', () => {
    expect(conversationIdFromSearch('')).toBeNull();
    expect(conversationIdFromSearch('?preview_pr=12')).toBeNull();
  });

  it('is null on junk / path-y / empty / markup', () => {
    expect(conversationIdFromSearch('?conversation=')).toBeNull();
    expect(conversationIdFromSearch('?conversation=../../etc/passwd')).toBeNull();
    expect(conversationIdFromSearch('?conversation=<script>')).toBeNull();
  });

  it('accepts a UUID with hyphens', () => {
    expect(conversationIdFromSearch(`?conversation=${CONV}`)).toBe(CONV);
    expect(CONV).toContain('-');
  });
});

describe('viewFromSearch', () => {
  it('defaults to chat until the flip, then desk (see Task 3.6)', () => {
    expect(viewFromSearch('')).toBe('chat');
    expect(DEFAULT_VIEW).toBe('chat');
  });

  it('accepts the allowlist', () => {
    expect(viewFromSearch('?view=desk')).toBe('desk');
    expect(viewFromSearch('?view=estate')).toBe('estate');
    expect(viewFromSearch('?view=chat')).toBe('chat');
  });

  it('rejects unknown values → default', () => {
    expect(viewFromSearch('?view=admin')).toBe(DEFAULT_VIEW);
  });

  it('works with or without a leading "?"', () => {
    expect(viewFromSearch('view=desk')).toBe('desk');
    expect(viewFromSearch('?view=desk')).toBe('desk');
  });

  it('every chat intent forces chat, beating an explicit view param', () => {
    for (const q of ['?reasoning=' + HEX32, '?conversation=' + CONV, '?ask_pr=42', '?preview_pr=42'])
      expect(viewFromSearch('?view=desk&' + q.slice(1))).toBe('chat');
  });

  // A malformed reasoning/conversation value is NOT a chat intent: those params
  // are validated by reasoningTraceFromSearch/conversationIdFromSearch, and junk
  // that those reject (e.g. path traversal) carries no real "resume this" signal.
  it('a malformed reasoning/conversation value does not force chat', () => {
    expect(viewFromSearch('?view=desk&reasoning=not-a-trace-id')).toBe('desk');
    expect(viewFromSearch('?view=desk&conversation=../../etc/passwd')).toBe('desk');
  });

  // ask_pr / preview_pr are read as raw truthiness, unlike reasoning/conversation.
  // A malformed value (e.g. "?ask_pr=abc") still means the visitor arrived from
  // the approval page on an errand — landing them on the desk would silently
  // swallow that intent, even though the real parser would reject the value.
  it('an invalid or zero ask_pr/preview_pr value still forces chat (any non-empty value counts)', () => {
    expect(viewFromSearch('?view=desk&ask_pr=abc')).toBe('chat');
    expect(viewFromSearch('?view=desk&ask_pr=0')).toBe('chat');
    expect(viewFromSearch('?view=desk&preview_pr=0')).toBe('chat');
  });

  // An empty-string value is falsy, so it does NOT count as intent — this mirrors
  // "the param is absent" rather than "the param names something".
  it('an empty ask_pr/preview_pr value does not force chat', () => {
    expect(viewFromSearch('?view=desk&ask_pr=')).toBe('desk');
    expect(viewFromSearch('?view=desk&preview_pr=')).toBe('desk');
  });
});
