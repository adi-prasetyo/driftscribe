// View-model grouping for the conversations history rail (P2). The backend
// returns conversations newest-updated first; this folds them into day buckets
// (today / yesterday / older) so the rail reads as a timeline. Pure data →
// data; the component owns presentation. `now` is injected so the bucketing is
// deterministic under test (no hidden Date.now()).
//
// i18n: buckets are returned as SEMANTIC IDS, not rendered labels — this module
// stays locale-free. ConversationsRail.svelte maps id → `$t('conversations.bucket.<id>')`.

import type { Conversation, ConversationTurn } from './types';
import { normalizeForSearch } from './format';
import { crewName } from './workloads';

export type ConversationBucket = 'today' | 'yesterday' | 'older';

/**
 * Does a conversation match a free-text query? Case- and separator-insensitive
 * (via `normalizeForSearch`) substring over the title plus EVERY crew that has
 * taken part — raw workload value and display name for each — so `anchor` finds
 * a `drift`-crew chat and `drift` finds it too.
 *
 * Searching the participant list rather than the bound `workload` is what makes
 * a handed-over conversation findable under the crew that started it: after a
 * handoff `workload` is the crew that JOINED, so searching it alone would hide
 * the thread from anyone looking for where they left it. `crews` is absent on
 * pre-handoff rows, where the single bound workload IS the whole history.
 *
 * An empty / whitespace-only query matches everything (the modal shows the full
 * list until the operator types).
 */
export function matchesConversation(c: Conversation, query: string): boolean {
  const q = normalizeForSearch(query);
  if (!q) return true;
  const crews = c.crews?.length ? c.crews : [c.workload];
  const hay = normalizeForSearch(
    [c.title, ...crews.flatMap((w) => [w, crewName(w)])].join(' '),
  );
  return hay.includes(q);
}

/**
 * Cap the rail to the newest `max` conversations, but never hide the one the
 * operator currently has open: if `activeId` falls outside the newest `max`, it
 * is appended so the active-row affordance survives (e.g. after resuming an
 * older chat from the search modal). The input is already newest-first
 * (backend contract); null/undefined entries are dropped. Returned in
 * newest-first order so the caller can bucket it unchanged.
 */
export function capConversations(
  conversations: ReadonlyArray<Conversation | null | undefined> | null | undefined,
  max: number,
  activeId: string | null,
): Conversation[] {
  const list = (conversations ?? []).filter((c): c is Conversation => c != null);
  if (list.length <= max) return list;
  const top = list.slice(0, max);
  if (activeId && !top.some((c) => c.conversation_id === activeId)) {
    const active = list.find((c) => c.conversation_id === activeId);
    if (active) return [...top, active];
  }
  return top;
}

export interface ConversationGroup {
  label: ConversationBucket;
  items: Conversation[];
}

/** Local midnight (00:00) of the day containing `d`. */
function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * Classify one conversation's `updated_at` relative to `now`:
 *   ≥ start-of-today      → today
 *   ≥ start-of-yesterday  → yesterday
 *   anything earlier      → older
 * A missing / unparseable timestamp falls to `older` (fail-safe: an undated
 * thread sorts to the bottom rather than masquerading as recent).
 */
export function bucketFor(updatedAt: string | undefined | null, now: Date): ConversationBucket {
  if (!updatedAt) return 'older';
  const ts = Date.parse(updatedAt);
  if (Number.isNaN(ts)) return 'older';
  const todayStart = startOfDay(now).getTime();
  const yesterdayStart = todayStart - 86_400_000; // 24h
  if (ts >= todayStart) return 'today';
  if (ts >= yesterdayStart) return 'yesterday';
  return 'older';
}

/**
 * Fold a newest-first conversation list into day-bucket groups, preserving the
 * incoming order within each bucket. Only non-empty buckets are returned, in
 * the fixed order today → yesterday → older. Tolerates a null/undefined list
 * and null entries (dropped).
 */
export function groupConversations(
  conversations: ReadonlyArray<Conversation | null | undefined> | null | undefined,
  now: Date,
): ConversationGroup[] {
  const order: ConversationBucket[] = ['today', 'yesterday', 'older'];
  const buckets = new Map<ConversationBucket, Conversation[]>();
  for (const c of conversations ?? []) {
    if (c == null) continue;
    const b = bucketFor(c.updated_at, now);
    const list = buckets.get(b);
    if (list) list.push(c);
    else buckets.set(b, [c]);
  }
  return order
    .filter((label) => (buckets.get(label)?.length ?? 0) > 0)
    .map((label) => ({ label, items: buckets.get(label)! }));
}

/** Roles the thread renders as a centered TRANSITION rule rather than as
 *  anybody's message. Server-authored: nobody said them. */
const TRANSITION_ROLES = ['crew_change', 'handoff_declined'];

/**
 * Does this turn own a reasoning disclosure — i.e. is it the kind of row
 * ConversationThread renders as a CREW bubble, the only branch that mounts
 * one?
 *
 * Extracted (ds-jns) because two callers must agree and had drifted: the
 * thread renders the disclosure, and App decides whether a
 * `?conversation=&reasoning=` deep link names a message this thread actually
 * shows — dropping the param when it does not. A `trace_id` is not sufficient
 * on its own: a USER turn carries the trace of the run it started, and a
 * declined handoff records a transition trace with no crew response after it.
 * Matching on the trace alone left the URL claiming a message that renders no
 * disclosure to open.
 */
export function turnOwnsReasoning(
  turn: ConversationTurn,
): turn is ConversationTurn & { trace_id: string } {
  return (
    typeof turn.trace_id === 'string' &&
    turn.trace_id !== '' &&
    turn.role !== 'user' &&
    !TRANSITION_ROLES.includes(turn.role)
  );
}
