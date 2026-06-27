// View-model grouping for the conversations history rail (P2). The backend
// returns conversations newest-updated first; this folds them into day buckets
// (Today / Yesterday / Older) so the rail reads as a timeline. Pure data →
// data; the component owns presentation. `now` is injected so the bucketing is
// deterministic under test (no hidden Date.now()).

import type { Conversation } from './types';

export type ConversationBucket = 'Today' | 'Yesterday' | 'Older';

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
 *   ≥ start-of-today      → Today
 *   ≥ start-of-yesterday  → Yesterday
 *   anything earlier      → Older
 * A missing / unparseable timestamp falls to `Older` (fail-safe: an undated
 * thread sorts to the bottom rather than masquerading as recent).
 */
export function bucketFor(updatedAt: string | undefined | null, now: Date): ConversationBucket {
  if (!updatedAt) return 'Older';
  const ts = Date.parse(updatedAt);
  if (Number.isNaN(ts)) return 'Older';
  const todayStart = startOfDay(now).getTime();
  const yesterdayStart = todayStart - 86_400_000; // 24h
  if (ts >= todayStart) return 'Today';
  if (ts >= yesterdayStart) return 'Yesterday';
  return 'Older';
}

/**
 * Fold a newest-first conversation list into day-bucket groups, preserving the
 * incoming order within each bucket. Only non-empty buckets are returned, in
 * the fixed order Today → Yesterday → Older. Tolerates a null/undefined list
 * and null entries (dropped).
 */
export function groupConversations(
  conversations: ReadonlyArray<Conversation | null | undefined> | null | undefined,
  now: Date,
): ConversationGroup[] {
  const order: ConversationBucket[] = ['Today', 'Yesterday', 'Older'];
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
