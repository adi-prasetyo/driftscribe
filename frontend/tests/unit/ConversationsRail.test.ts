import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import ConversationsRail from '../../src/components/ConversationsRail.svelte';
import type { Conversation } from '../../src/lib/types';

afterEach(cleanup);

const noop = () => {};

function conv(partial: Partial<Conversation> & { conversation_id: string }): Conversation {
  return { workload: 'drift', title: partial.conversation_id, ...partial } as Conversation;
}

describe('ConversationsRail', () => {
  it('shows the empty state when there are no conversations', () => {
    const { getByText, queryByTestId } = render(ConversationsRail, {
      props: { conversations: [], activeConversationId: null, onOpen: noop },
    });
    expect(getByText('No conversations yet.')).toBeTruthy();
    expect(queryByTestId('conversation-item')).toBeNull();
  });

  it('renders one card per conversation with its title and crew', () => {
    const conversations = [
      conv({ conversation_id: 'c1', title: 'Why did payment-demo drift?', workload: 'drift', updated_at: new Date().toISOString() }),
      conv({ conversation_id: 'c2', title: 'Adopt the assets bucket', workload: 'provision', updated_at: new Date().toISOString() }),
    ];
    const { getAllByTestId, getByText } = render(ConversationsRail, {
      props: { conversations, activeConversationId: null, onOpen: noop },
    });
    expect(getAllByTestId('conversation-item')).toHaveLength(2);
    expect(getByText('Why did payment-demo drift?')).toBeTruthy();
    expect(getByText('Adopt the assets bucket')).toBeTruthy();
  });

  it('fires onOpen with the conversation id when a card is clicked', async () => {
    const onOpen = vi.fn();
    const conversations = [conv({ conversation_id: 'c-42', title: 't', updated_at: new Date().toISOString() })];
    const { getByTestId } = render(ConversationsRail, {
      props: { conversations, activeConversationId: null, onOpen },
    });
    await fireEvent.click(getByTestId('conversation-open'));
    expect(onOpen).toHaveBeenCalledWith('c-42');
  });

  it('marks the active conversation row', () => {
    const conversations = [
      conv({ conversation_id: 'c1', title: 'a', updated_at: new Date().toISOString() }),
      conv({ conversation_id: 'c2', title: 'b', updated_at: new Date().toISOString() }),
    ];
    const { getAllByTestId } = render(ConversationsRail, {
      props: { conversations, activeConversationId: 'c2', onOpen: noop },
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
      props: { conversations, activeConversationId: null, onOpen: noop },
    });
    const groups = getAllByTestId('conv-group');
    expect(groups).toHaveLength(2);
    expect(groups[0].textContent).toContain('Today');
    expect(groups[1].textContent).toContain('Older');
  });
});
