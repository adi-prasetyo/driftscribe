// Deep-link helpers for the shareable reasoning-timeline URL (?reasoning=<id>).
//
// The SPA shell is served at "/" ONLY (agent/main.py — no catch-all route), so a
// linkable pointer to one past reasoning timeline rides a query param on that
// same root. That is deliberate: it needs NO extra backend route and NO
// Cloudflare demo-allowlist entry — the "/" shell is un-gated and the GET
// /trace/{id} the record fetches is already on the demo allowlist. A pretty
// /reasoning/{id} PATH would have cost both (see the design discussion).

// A DriftScribe trace id is a Cloud-trace id: exactly 32 lowercase hex chars.
// This mirrors driftscribe_lib.logging._HEX32_RE (and agent/trace_fetcher.py),
// and the backend 400s GET /trace/{id} on anything else — so we only ever
// deep-link a well-formed id and never hand junk to openTrace().
const HEX32_RE = /^[0-9a-f]{32}$/;

/**
 * Whether `id` is a trace id this module would accept back out of a
 * `?reasoning=` param — i.e. whether a link built from it can round-trip.
 *
 * Exported so a component that OFFERS a reasoning link can gate on the same
 * rule the parser applies, rather than each side owning its own idea of a
 * well-formed id. Without it a caller can render a link that opens the record
 * once (openTrace takes any string) but silently fails to restore when the
 * resulting URL is shared or reloaded, because `reasoningTraceFromSearch`
 * rejects what `syncReasoningParam` just wrote. The desk's pending card
 * (ds-wd2.15) gates on this for exactly that reason.
 */
export function isReplayableTraceId(id: unknown): id is string {
  return typeof id === 'string' && HEX32_RE.test(id);
}

/**
 * The trace id to open as a record from a `?reasoning=<hex32>` query string, or null when
 * the param is absent or malformed. Pure — the caller decides what to do with it
 * (App.svelte calls openTrace on boot; syncReasoningParam writes it back).
 */
export function reasoningTraceFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get('reasoning');
  return raw !== null && HEX32_RE.test(raw) ? raw : null;
}

// A conversation id is a UUID4 minted at persist time (agent/main.py, str(uuid.uuid4())).
// We validate loosely — a safe id charset, not a strict UUID — because unlike a trace id
// the backend 404s (not 400s) an unknown conversation and openConversation() already
// fail-safes on a non-ok GET /conversations/{id}. The guard just keeps obvious junk
// (path traversal, empty, markup) from firing a doomed fetch. Fetch path is
// encodeURIComponent'd regardless (defense in depth).
const CONVERSATION_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

/**
 * The conversation id to resume from a `?conversation=<id>` query string, or null
 * when the param is absent or malformed. Pure — App.svelte calls openConversation
 * on boot and syncConversationParam writes it back. Mirrors reasoningTraceFromSearch.
 */
export function conversationIdFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get('conversation');
  return raw !== null && CONVERSATION_ID_RE.test(raw) ? raw : null;
}

// The SPA's two client-side views. No router library — this is the same
// pure-function-over-location.search pattern as the deep-link helpers above,
// just picking a view instead of a resource id.
// VIEWS is the single source of truth and AppView is derived from it, so the
// allowlist and the type can never drift apart (same idiom as AUTONOMY_MODES /
// AutonomyMode in lib/autonomy.ts).
//
// 'estate' left this list on 2026-07-31: the estate stopped being a view and
// became a SECTION of the desk. Dropping it from here is what makes the type
// enforce that — any `'estate'`-typed straggler is now a compile error rather
// than a live route to a page that no longer exists. Old links still work; see
// the alias in viewFromSearch below, which matches the raw string precisely so
// it does not depend on this list.
export const VIEWS = ['desk', 'chat'] as const;
export type AppView = (typeof VIEWS)[number];

// Flipped from 'chat' to 'desk' at Task 3.6 step 2, after the desk was
// visually verified in both locales across all three states
// (tests/visual/desk.visual.ts). This is the line that makes the redesigned
// "approval desk" the SPA's front door: a bare URL now lands on the desk, and
// the chat view is reached via the header nav or an explicit ?view=chat.
// Keep it a bare literal — no branching here — so reverting is one line.
//
// Nothing is stranded by this: hasChatIntent() below still forces 'chat' for
// every chat-intent param, so shared ?reasoning= / ?conversation= / ?ask_pr= /
// ?preview_pr= links keep working (pinned by tests in both deeplink.test.ts
// and App.test.ts).
export const DEFAULT_VIEW: AppView = 'desk';

// Every param that describes an errand somewhere OTHER than the desk's front
// door, named once. App.svelte's navigate() clears all of them when leaving
// chat, so a copied desk URL never carries a stale errand that would drag a
// later visitor back into chat on reload.
//
// Until ds-jns this list was ALSO the definition of "go to chat" — the two
// were the same set. They no longer are: `reasoning` and `preview_pr` now
// resolve to the DESK (see hasChatIntent below), so this list is the wider
// one, and the name is kept only because chat is still where every param on it
// was last handled. Do not re-derive one from the other.
export const CHAT_INTENT_PARAMS = [
  'reasoning',
  'conversation',
  'ask_pr',
  'preview_pr',
] as const;

/**
 * Whether this URL's purpose lives in the chat view, regardless of any
 * explicit `?view=` param.
 *
 * Two params carry that signal, and it is fewer than it used to be (ds-jns):
 *  - `?conversation=<id>` — a shared thread, validated by the helper above.
 *    A `?reasoning=` alongside it is part of that thread's errand (the message
 *    whose reasoning to expand) and rides along; it is not asked about here.
 *  - `?ask_pr=<n>` — the composer prefill (lib/workloads.ts).
 *
 * A BARE `?reasoning=<hex32>` and a `?preview_pr=<n>` deliberately do NOT.
 * Both now name something the desk renders — a decision record and an estate
 * preview — so sending them to chat would land the visitor on a page that has
 * nothing to do with the link they followed. This is the whole point of the
 * design's URL-context rule: the same param means "expand this message" inside
 * a conversation and "open this record" without one.
 *
 * ask_pr is read as raw truthiness rather than through a parser: even a
 * malformed value ("?ask_pr=abc") means the visitor arrived from the approval
 * page on an errand, and swallowing it would be worse than honoring it. An
 * empty value ("?ask_pr=") is treated as absent — "the param is absent" rather
 * than "the param names something".
 */
function hasChatIntent(search: string): boolean {
  const params = new URLSearchParams(search);
  return Boolean(conversationIdFromSearch(search) || params.get('ask_pr'));
}

/**
 * The view the SPA shell should render for a given `location.search`. Any
 * chat intent (see hasChatIntent) wins over an explicit `?view=`, because a
 * shared link's whole point is the thing it points at, not the default front
 * door. Pure — App.svelte (Task 2.2) owns wiring this into actual navigation.
 */
export function viewFromSearch(search: string): AppView {
  if (hasChatIntent(search)) return 'chat';
  // URLSearchParams tolerates a leading "?" itself, so — like the two helpers
  // above — the raw search string goes straight in.
  const params = new URLSearchParams(search);
  // `?preview_pr=` outranks an explicit `?view=chat`, which is the one place
  // this function lets a param beat a stated view.
  //
  // Chat has no rendering for it AT ALL since the estate preview moved to the
  // desk (Task 2.4), so honoring `view=chat` here shows a page with no trace of
  // why the visitor followed the link, and the param then rides along inertly
  // until the next navigate() drops it. Landing on the desk is the only reading
  // under which the link means anything.
  //
  // Deliberately NOT extended to `?view=chat&reasoning=`, and the reason
  // outlived its original one. It used to be that chat DID render that shape,
  // as a page-level replay, so the stated view and the param agreed. ds-jns
  // Task 3.3 deleted replay — but the explicit `view=chat` is still honoured
  // here, and App's boot continuation hands the trace on to the desk record
  // once chat has had its say. That ordering is deliberate: an explicit view
  // request is answered, then the app admits it has nothing to render.
  // The rule is "a param with no rendering on the stated view wins", not
  // "desk params always win".
  //
  // Bare `?preview_pr=` never needed this — DEFAULT_VIEW is already 'desk' —
  // and bare is the shape the IaC approval page actually emits, so this covers
  // a hand-written or hand-edited URL rather than a link the app produces.
  if (params.get('preview_pr')) return 'desk';
  const raw = params.get('view');
  // Legacy alias: the estate merged into the desk (2026-07-31 design doc). Old
  // ?view=estate links land on the merged page rather than 404-ing into a blank
  // main. Matched on the RAW string, deliberately not via VIEWS — the id is
  // retired from the allowlist and this line must keep working without it.
  if (raw === 'estate') return 'desk';
  return VIEWS.includes(raw as AppView) ? (raw as AppView) : DEFAULT_VIEW;
}
