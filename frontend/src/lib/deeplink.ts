// Deep-link helpers for the shareable reasoning-timeline URL (?reasoning=<id>).
//
// The SPA shell is served at "/" ONLY (agent/main.py — no catch-all route), so a
// linkable pointer to one past reasoning timeline rides a query param on that
// same root. That is deliberate: it needs NO extra backend route and NO
// Cloudflare demo-allowlist entry — the "/" shell is un-gated and the GET
// /trace/{id} the replay fetches is already on the demo allowlist. A pretty
// /reasoning/{id} PATH would have cost both (see the design discussion).

// A DriftScribe trace id is a Cloud-trace id: exactly 32 lowercase hex chars.
// This mirrors driftscribe_lib.logging._HEX32_RE (and agent/trace_fetcher.py),
// and the backend 400s GET /trace/{id} on anything else — so we only ever
// deep-link a well-formed id and never hand junk to openTrace().
const HEX32_RE = /^[0-9a-f]{32}$/;

/**
 * The trace id to replay from a `?reasoning=<hex32>` query string, or null when
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

// The SPA's three client-side views. No router library — this is the same
// pure-function-over-location.search pattern as the deep-link helpers above,
// just picking a view instead of a resource id.
// VIEWS is the single source of truth and AppView is derived from it, so the
// allowlist and the type can never drift apart (same idiom as AUTONOMY_MODES /
// AutonomyMode in lib/autonomy.ts).
export const VIEWS = ['desk', 'estate', 'chat'] as const;
export type AppView = (typeof VIEWS)[number];

// 'chat' until the redesigned "approval desk" front door has been visually
// verified (see the composite-redesign plan, Task 3.6), at which point this
// flips to 'desk' as a deliberate one-line change. Keep it a bare literal —
// no branching here — so that flip stays exactly one line.
export const DEFAULT_VIEW: AppView = 'chat';

// The chat-intent params, named once. Two consumers must agree on this list:
// hasChatIntent below (which of them mean "go to chat") and App.svelte's
// navigate() (which of them to clear when leaving chat). Getting only one side
// right when a fifth is added is a silent bug — either a param stops forcing
// chat, or a stale one survives into a copied desk URL and drags a later
// visitor back into chat on reload. Same drift risk VIEWS/AppView closes above.
export const CHAT_INTENT_PARAMS = [
  'reasoning',
  'conversation',
  'ask_pr',
  'preview_pr',
] as const;

/**
 * Whether this URL's purpose lives in the chat view, regardless of any
 * explicit `?view=` param. Four params carry that signal:
 *  - `?reasoning=<hex32>` / `?conversation=<id>` — a shared replay or thread,
 *    validated by the existing helpers above (reuse, don't re-validate).
 *  - `?ask_pr=<n>` / `?preview_pr=<n>` — composer-prefill and InfraDiagram
 *    ghost-overlay seeds (see lib/workloads.ts and lib/infra_graph.ts).
 * ask_pr/preview_pr are read as raw truthiness rather than through their own
 * parsers: even a malformed value (e.g. "?ask_pr=abc") means the visitor
 * arrived from the approval page on an errand, and landing them on the desk
 * would silently swallow that intent. An empty value ("?ask_pr=") is treated
 * as absent, matching "the param is absent" rather than "the param names something".
 */
function hasChatIntent(search: string): boolean {
  const params = new URLSearchParams(search);
  return Boolean(
    reasoningTraceFromSearch(search) ||
      conversationIdFromSearch(search) ||
      params.get('ask_pr') ||
      params.get('preview_pr'),
  );
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
  const raw = new URLSearchParams(search).get('view');
  return VIEWS.includes(raw as AppView) ? (raw as AppView) : DEFAULT_VIEW;
}
