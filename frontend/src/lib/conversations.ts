// View-model grouping for the conversations history rail (P2). The backend
// returns conversations newest-updated first; this folds them into day buckets
// (today / yesterday / older) so the rail reads as a timeline. Pure data →
// data; the component owns presentation. `now` is injected so the bucketing is
// deterministic under test (no hidden Date.now()).
//
// i18n: buckets are returned as SEMANTIC IDS, not rendered labels — this module
// stays locale-free. ConversationsRail.svelte maps id → `$t('conversations.bucket.<id>')`.

import type { Conversation } from './types';
import { normalizeForSearch } from './format';
import { crewName } from './workloads';

export type ConversationBucket = 'today' | 'yesterday' | 'older';

/**
 * Does a conversation match a free-text query? Case- and separator-insensitive
 * (via `normalizeForSearch`) substring over the title, the raw workload value,
 * and the crew display name — so `anchor` finds a `drift`-crew chat and `drift`
 * finds it too. An empty / whitespace-only query matches everything (the modal
 * shows the full list until the operator types).
 */
export function matchesConversation(c: Conversation, query: string): boolean {
  const q = normalizeForSearch(query);
  if (!q) return true;
  const hay = normalizeForSearch([c.title, c.workload, crewName(c.workload)].join(' '));
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
