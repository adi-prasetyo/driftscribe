import { describe, it, expect } from 'vitest';
import {
  bucketFor,
  groupConversations,
  matchesConversation,
  capConversations,
  type ConversationGroup,
} from '../../src/lib/conversations';
import type { Conversation } from '../../src/lib/types';

function fullConv(over: Partial<Conversation>): Conversation {
  return { conversation_id: 'c', workload: 'drift', title: 'untitled', ...over };
}

describe('matchesConversation', () => {
  it('matches on the title (case-insensitive)', () => {
    const c = fullConv({ title: 'Investigate the Storefront bucket' });
    expect(matchesConversation(c, 'storefront')).toBe(true);
    expect(matchesConversation(c, 'STOREFRONT')).toBe(true);
    expect(matchesConversation(c, 'database')).toBe(false);
  });

  it('matches on the raw workload value AND the crew display name', () => {
    const c = fullConv({ workload: 'drift', title: 'x' });
    expect(matchesConversation(c, 'drift')).toBe(true);
    expect(matchesConversation(c, 'anchor')).toBe(true); // drift → Anchor
  });

  it('is separator-insensitive', () => {
    const c = fullConv({ title: 'check adopt_probe_topic' });
    expect(matchesConversation(c, 'adopt probe')).toBe(true);
  });

  it('an empty / whitespace query matches everything', () => {
    const c = fullConv({ title: 'anything' });
    expect(matchesConversation(c, '')).toBe(true);
    expect(matchesConversation(c, '   ')).toBe(true);
  });
});

describe('capConversations', () => {
  const list: Conversation[] = Array.from({ length: 8 }, (_, i) =>
    fullConv({ conversation_id: `c${i}`, title: `t${i}` }),
  ); // newest-first c0..c7

  it('returns the list unchanged when it fits within max', () => {
    expect(capConversations(list.slice(0, 3), 5, null)).toHaveLength(3);
  });

  it('keeps only the newest max when nothing active is hidden', () => {
    const out = capConversations(list, 5, null);
    expect(out.map((c) => c.conversation_id)).toEqual(['c0', 'c1', 'c2', 'c3', 'c4']);
  });

  it('appends the active conversation when it falls outside the cap', () => {
    const out = capConversations(list, 5, 'c7');
    expect(out.map((c) => c.conversation_id)).toEqual(['c0', 'c1', 'c2', 'c3', 'c4', 'c7']);
  });

  it('does not duplicate the active conversation when it is already visible', () => {
    const out = capConversations(list, 5, 'c2');
    expect(out.map((c) => c.conversation_id)).toEqual(['c0', 'c1', 'c2', 'c3', 'c4']);
  });

  it('drops null entries and tolerates null/undefined input', () => {
    expect(capConversations([null, undefined], 5, null)).toEqual([]);
    expect(capConversations(null, 5, null)).toEqual([]);
  });
});

// A fixed "now": 2026-06-27 15:00 local. All cases are expressed relative to it.
const NOW = new Date(2026, 5, 27, 15, 0, 0);

function conv(id: string, updated_at: string | undefined): Conversation {
  return { conversation_id: id, workload: 'drift', title: id, updated_at };
}

describe('bucketFor', () => {
  it('classifies a timestamp earlier today as Today', () => {
    expect(bucketFor(new Date(2026, 5, 27, 9, 0, 0).toISOString(), NOW)).toBe('Today');
  });

  it('classifies just-after-midnight today as Today (boundary)', () => {
    expect(bucketFor(new Date(2026, 5, 27, 0, 0, 0).toISOString(), NOW)).toBe('Today');
  });

  it('classifies yesterday as Yesterday', () => {
    expect(bucketFor(new Date(2026, 5, 26, 23, 30, 0).toISOString(), NOW)).toBe('Yesterday');
  });

  it('classifies the start of yesterday as Yesterday (boundary)', () => {
    expect(bucketFor(new Date(2026, 5, 26, 0, 0, 0).toISOString(), NOW)).toBe('Yesterday');
  });

  it('classifies two days ago as Older', () => {
    expect(bucketFor(new Date(2026, 5, 25, 12, 0, 0).toISOString(), NOW)).toBe('Older');
  });

  it('treats a missing timestamp as Older (fail-safe)', () => {
    expect(bucketFor(undefined, NOW)).toBe('Older');
    expect(bucketFor(null, NOW)).toBe('Older');
  });

  it('treats an unparseable timestamp as Older', () => {
    expect(bucketFor('not-a-date', NOW)).toBe('Older');
  });
});

describe('groupConversations', () => {
  it('buckets into Today/Yesterday/Older and preserves input order within a bucket', () => {
    const list = [
      conv('t1', new Date(2026, 5, 27, 14, 0, 0).toISOString()),
      conv('t2', new Date(2026, 5, 27, 8, 0, 0).toISOString()),
      conv('y1', new Date(2026, 5, 26, 10, 0, 0).toISOString()),
      conv('o1', new Date(2026, 5, 20, 10, 0, 0).toISOString()),
    ];
    const groups = groupConversations(list, NOW);
    expect(groups.map((g: ConversationGroup) => g.label)).toEqual([
      'Today',
      'Yesterday',
      'Older',
    ]);
    expect(groups[0].items.map((c) => c.conversation_id)).toEqual(['t1', 't2']);
    expect(groups[1].items.map((c) => c.conversation_id)).toEqual(['y1']);
    expect(groups[2].items.map((c) => c.conversation_id)).toEqual(['o1']);
  });

  it('omits empty buckets and keeps the fixed Today→Yesterday→Older order', () => {
    const list = [
      conv('o1', new Date(2026, 5, 1, 10, 0, 0).toISOString()),
      conv('t1', new Date(2026, 5, 27, 9, 0, 0).toISOString()),
    ];
    const groups = groupConversations(list, NOW);
    // Only Today + Older have members; no Yesterday group is emitted.
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Older']);
  });

  it('tolerates a null/undefined list and null entries', () => {
    expect(groupConversations(null, NOW)).toEqual([]);
    expect(groupConversations(undefined, NOW)).toEqual([]);
    const groups = groupConversations(
      [null, conv('t1', new Date(2026, 5, 27, 9, 0, 0).toISOString()), undefined],
      NOW,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].items.map((c) => c.conversation_id)).toEqual(['t1']);
  });

  it('sinks undated conversations into Older', () => {
    const groups = groupConversations([conv('u1', undefined)], NOW);
    expect(groups.map((g) => g.label)).toEqual(['Older']);
    expect(groups[0].items[0].conversation_id).toBe('u1');
  });
});
