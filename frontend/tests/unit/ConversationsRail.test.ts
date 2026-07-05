import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, within } from '@testing-library/svelte';
import ConversationsRail from '../../src/components/ConversationsRail.svelte';
import type { Conversation } from '../../src/lib/types';

afterEach(cleanup);

const noop = () => {};
// onNewChat is a required prop; tests that don't exercise it pass a no-op.

function conv(partial: Partial<Conversation> & { conversation_id: string }): Conversation {
  return { workload: 'drift', title: partial.conversation_id, ...partial } as Conversation;
}

describe('ConversationsRail', () => {
  it('shows the empty state when there are no conversations', () => {
    const { getByText, queryByTestId } = render(ConversationsRail, {
      props: { conversations: [], activeConversationId: null, onOpen: noop, onNewChat: noop },
    });
    // Substring match — the empty state now carries a fuller resume hint.
    expect(
      getByText((t) => t.startsWith('No conversations yet.')),
    ).toBeTruthy();
    expect(queryByTestId('conversation-item')).toBeNull();
  });

  it('explains the chat features through a header help hint (collapsed by default, trust boundary pinned)', async () => {
    const { getByTestId, queryByTestId } = render(ConversationsRail, {
      // Present even with zero conversations — it explains what the rail is for.
      props: { conversations: [], activeConversationId: null, onOpen: noop, onNewChat: noop },
    });
    const btn = getByTestId('conversations-help');
    // Non-status accessible name (this hint is not an iac_apply-status hint).
    expect(btn.getAttribute('aria-label')).toBe('About conversations');
    // Collapsed by default — no panel until the operator opens it.
    expect(queryByTestId('conversations-help-panel')).toBeNull();
    await fireEvent.click(btn);
    const panel = getByTestId('conversations-help-panel');
    const text = panel.textContent ?? '';
    // The resume story AND the cross-crew trust boundary must both survive edits.
    expect(text).toContain('reopen');
    expect(text).toContain('team memory');
    expect(text).toContain('redacted');
  });

  it('renders one card per conversation with its title and crew', () => {
    const conversations = [
      conv({ conversation_id: 'c1', title: 'Why did payment-demo drift?', workload: 'drift', updated_at: new Date().toISOString() }),
      conv({ conversation_id: 'c2', title: 'Adopt the assets bucket', workload: 'provision', updated_at: new Date().toISOString() }),
    ];
    const { getAllByTestId, getByText } = render(ConversationsRail, {
      props: { conversations, activeConversationId: null, onOpen: noop, onNewChat: noop },
    });
    expect(getAllByTestId('conversation-item')).toHaveLength(2);
    expect(getByText('Why did payment-demo drift?')).toBeTruthy();
    expect(getByText('Adopt the assets bucket')).toBeTruthy();
  });

  it('fires onOpen with the conversation id when a card is clicked', async () => {
    const onOpen = vi.fn();
    const conversations = [conv({ conversation_id: 'c-42', title: 't', updated_at: new Date().toISOString() })];
    const { getByTestId } = render(ConversationsRail, {
      props: { conversations, activeConversationId: null, onOpen, onNewChat: noop },
    });
    await fireEvent.click(getByTestId('conversation-open'));
    expect(onOpen).toHaveBeenCalledWith('c-42');
  });

  it('fires onNewChat when the header New chat button is clicked', async () => {
    const onNewChat = vi.fn();
    const { getByTestId } = render(ConversationsRail, {
      props: { conversations: [], activeConversationId: null, onOpen: noop, onNewChat },
    });
    await fireEvent.click(getByTestId('conversations-new-chat'));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it('marks the active conversation row', () => {
    const conversations = [
      conv({ conversation_id: 'c1', title: 'a', updated_at: new Date().toISOString() }),
      conv({ conversation_id: 'c2', title: 'b', updated_at: new Date().toISOString() }),
    ];
    const { getAllByTestId } = render(ConversationsRail, {
      props: { conversations, activeConversationId: 'c2', onOpen: noop, onNewChat: noop },
    });
    const rows = getAllByTestId('conversation-item');
    expect(rows[0].classList.contains('active')).toBe(false);
    expect(rows[1].classList.contains('active')).toBe(true);
  });

  it('groups conversations into day buckets (Today / Older)', () => {
    const conversations = [
      conv({ conversation_id: 'today', title: 'recent', updated_at: new Date().toISOString() }),
      conv({ conversation_id: 'old', title: 'ancient', updated_at: '2020-01-01T00:00:00Z' }),
    ];
    const { getAllByTestId } = render(ConversationsRail, {
      props: { conversations, activeConversationId: null, onOpen: noop, onNewChat: noop },
    });
    const groups = getAllByTestId('conv-group');
    expect(groups).toHaveLength(2);
    expect(groups[0].textContent).toContain('Today');
    expect(groups[1].textContent).toContain('Older');
  });
});

// Eight "Today" conversations (newest-first c0..c7) for the cap/search tests.
function manyConvs(n = 8): Conversation[] {
  const ts = new Date().toISOString();
  return Array.from({ length: n }, (_, i) =>
    conv({ conversation_id: `c${i}`, title: `chat ${i}`, updated_at: ts }),
  );
}

describe('ConversationsRail — cap + search', () => {
  it('caps the rail to `max` and shows the search affordance with the TOTAL count', () => {
    const { getAllByTestId, getByTestId } = render(ConversationsRail, {
      props: { conversations: manyConvs(8), activeConversationId: null, onOpen: noop, onNewChat: noop, max: 5 },
    });
    expect(getAllByTestId('conversation-item')).toHaveLength(5);
    expect(getByTestId('conversations-search-open').textContent).toContain('(8)');
  });

  it('hides the affordance when the list fits within `max`', () => {
    const { queryByTestId } = render(ConversationsRail, {
      props: { conversations: manyConvs(4), activeConversationId: null, onOpen: noop, onNewChat: noop, max: 5 },
    });
    expect(queryByTestId('conversations-search-open')).toBeNull();
  });

  it('hides the affordance when active-pinning means every row is already shown (total = max+1)', () => {
    const { getAllByTestId, queryByTestId } = render(ConversationsRail, {
      props: { conversations: manyConvs(6), activeConversationId: 'c5', onOpen: noop, onNewChat: noop, max: 5 },
    });
    expect(getAllByTestId('conversation-item')).toHaveLength(6); // 5 + pinned active = all
    expect(queryByTestId('conversations-search-open')).toBeNull(); // nothing hidden
  });

  it('pins the active conversation in the rail even when it falls outside the cap', () => {
    const { getAllByTestId } = render(ConversationsRail, {
      props: { conversations: manyConvs(8), activeConversationId: 'c7', onOpen: noop, onNewChat: noop, max: 5 },
    });
    const rows = getAllByTestId('conversation-item');
    expect(rows).toHaveLength(6); // 5 newest + the pinned active one
    expect(rows.some((r) => r.classList.contains('active'))).toBe(true);
  });

  it('opens the modal showing the full list, filters live, and resumes on click', async () => {
    const onOpen = vi.fn();
    const convs = manyConvs(7);
    convs[6] = conv({ conversation_id: 'c6', title: 'zztarget unique', updated_at: new Date().toISOString() });
    const { getByTestId, queryByTestId, container } = render(ConversationsRail, {
      props: { conversations: convs, activeConversationId: null, onOpen, onNewChat: noop, max: 5 },
    });
    // Modal closed initially.
    expect(queryByTestId('conversations-search-input')).toBeNull();
    await fireEvent.click(getByTestId('conversations-search-open'));
    // Full list visible + count.
    expect(getByTestId('conversations-search-count').textContent).toContain('7 of 7');
    // Filter to the one distinctive title.
    await fireEvent.input(getByTestId('conversations-search-input'), { target: { value: 'zztarget' } });
    expect(getByTestId('conversations-search-count').textContent).toContain('1 of 7');
    // Resume that result — scope to the modal (the rail still shows its 5).
    const dialog = container.querySelector('dialog')!;
    await fireEvent.click(within(dialog).getByTestId('conversation-open'));
    expect(onOpen).toHaveBeenCalledWith('c6');
  });

  it('shows a no-match note when the query matches nothing', async () => {
    const { getByTestId, getByText } = render(ConversationsRail, {
      props: { conversations: manyConvs(7), activeConversationId: null, onOpen: noop, onNewChat: noop, max: 5 },
    });
    await fireEvent.click(getByTestId('conversations-search-open'));
    await fireEvent.input(getByTestId('conversations-search-input'), { target: { value: 'qqqzzz-nomatch' } });
    expect(getByTestId('conversations-search-count').textContent).toContain('0 of 7');
    expect(getByText((t) => t.startsWith('No chats match'))).toBeTruthy();
  });
});
