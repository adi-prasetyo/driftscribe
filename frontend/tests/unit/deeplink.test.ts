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
  // Flipped at Task 3.6 step 2 — a bare URL is now the approval desk.
  it('defaults to the desk (the redesigned front door)', () => {
    expect(viewFromSearch('')).toBe('desk');
    expect(DEFAULT_VIEW).toBe('desk');
  });

  it('accepts the allowlist', () => {
    expect(viewFromSearch('?view=desk')).toBe('desk');
    expect(viewFromSearch('?view=chat')).toBe('chat');
  });

  // The estate merged INTO the desk (2026-07-31 design doc), so its view id is
  // retired. Old shared links must still land on the merged page rather than
  // falling through to the default by accident — this is an explicit alias, and
  // it keeps working WITHOUT 'estate' in VIEWS because it matches the raw
  // string, not the allowlist. That is the whole point: the id is retired from
  // the type, so nothing but this line can produce the old view.
  it('treats ?view=estate as a legacy alias for the desk', () => {
    expect(viewFromSearch('?view=estate')).toBe('desk');
  });

  it('rejects unknown values → default', () => {
    expect(viewFromSearch('?view=admin')).toBe(DEFAULT_VIEW);
  });

  // A present-but-valueless param reads as '' (distinct from absent, which
  // reads as null) — neither is in the allowlist, so both fall to the default.
  it('treats an empty ?view= as absent', () => {
    expect(viewFromSearch('?view=')).toBe(DEFAULT_VIEW);
  });

  it('works with or without a leading "?"', () => {
    expect(viewFromSearch('view=desk')).toBe('desk');
    expect(viewFromSearch('?view=desk')).toBe('desk');
  });

  // ds-jns — the five-row destination matrix. The param no longer decides the
  // view on its own: `reasoning` means "expand this message" inside a
  // conversation and "open this record" without one, and the desk renders the
  // second. The old suite asserted all four params force chat; that was the
  // behaviour this design set out to change, so this is a rewrite, not an edit.
  it('a conversation goes to chat, beating an explicit view param', () => {
    expect(viewFromSearch('?view=desk&conversation=' + CONV)).toBe('chat');
  });

  it('a conversation + reasoning goes to chat — the reasoning frames the thread', () => {
    expect(viewFromSearch(`?view=desk&conversation=${CONV}&reasoning=${HEX32}`)).toBe('chat');
  });

  it('ask_pr goes to chat — it is a composer prefill', () => {
    expect(viewFromSearch('?view=desk&ask_pr=42')).toBe('chat');
  });

  it('a BARE reasoning goes to the desk — that is where a decision record opens', () => {
    expect(viewFromSearch('?reasoning=' + HEX32)).toBe('desk');
    // ...and an explicit ?view=chat still wins, so the replay links this app
    // wrote before the fork keep resolving to the replay.
    expect(viewFromSearch('?view=chat&reasoning=' + HEX32)).toBe('chat');
  });

  it('preview_pr goes to the desk — the estate lives there', () => {
    expect(viewFromSearch('?preview_pr=42')).toBe('desk');
    expect(viewFromSearch('?view=desk&preview_pr=0')).toBe('desk');
  });

  it('preview_pr beats an explicit ?view=chat, because chat cannot render it', () => {
    // The one param allowed to outrank a stated view. Since the estate preview
    // moved to the desk there is NO chat rendering for preview_pr, so honoring
    // view=chat shows a page with no trace of why the visitor followed the link
    // and leaves the param riding along inertly until the next navigate().
    expect(viewFromSearch('?view=chat&preview_pr=168')).toBe('desk');
    // Contrast with the reasoning case directly above, which is NOT symmetric:
    // chat still renders that one as the page-level replay, so the stated view
    // and the param agree and the legacy link is honored. The rule is "a param
    // with no rendering on the stated view wins", not "desk params always win".
    expect(viewFromSearch('?view=chat&reasoning=' + HEX32)).toBe('chat');
    // A genuine chat errand still outranks it — hasChatIntent runs first.
    expect(viewFromSearch(`?conversation=${CONV}&preview_pr=168`)).toBe('chat');
    // Absent/empty is not "names something": no hijack of a plain chat link.
    expect(viewFromSearch('?view=chat&preview_pr=')).toBe('chat');
  });

  // A malformed conversation value is NOT a chat intent: the param is validated
  // by conversationIdFromSearch, and junk it rejects (e.g. path traversal)
  // carries no real "resume this" signal.
  it('a malformed conversation value does not force chat', () => {
    expect(viewFromSearch('?view=desk&conversation=../../etc/passwd')).toBe('desk');
    // And a malformed conversation cannot promote a bare reasoning into one.
    expect(viewFromSearch(`?conversation=../x&reasoning=${HEX32}`)).toBe('desk');
  });

  // ask_pr is read as raw truthiness, unlike conversation. A malformed value
  // (e.g. "?ask_pr=abc") still means the visitor arrived from the approval page
  // on an errand — landing them on the desk would silently swallow that intent,
  // even though the real parser would reject the value.
  it('an invalid or zero ask_pr value still forces chat (any non-empty value counts)', () => {
    expect(viewFromSearch('?view=desk&ask_pr=abc')).toBe('chat');
    expect(viewFromSearch('?view=desk&ask_pr=0')).toBe('chat');
  });

  // An empty-string value is falsy, so it does NOT count as intent — this mirrors
  // "the param is absent" rather than "the param names something".
  it('an empty ask_pr value does not force chat', () => {
    expect(viewFromSearch('?view=desk&ask_pr=')).toBe('desk');
  });
});
