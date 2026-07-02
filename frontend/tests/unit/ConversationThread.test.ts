import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import ConversationThread from '../../src/components/ConversationThread.svelte';
import type { ConversationTurn } from '../../src/lib/types';

afterEach(cleanup);

const noop = () => {};

function turn(partial: Partial<ConversationTurn> & { seq: number; role: string }): ConversationTurn {
  return { text: '', workload: 'drift', ...partial } as ConversationTurn;
}

describe('ConversationThread', () => {
  it('renders user and crew bubbles in order with the crew display name', () => {
    const turns = [
      turn({ seq: 0, role: 'user', text: 'hello there' }),
      turn({ seq: 1, role: 'crew', text: 'hi, I am Anchor', workload: 'drift' }),
    ];
    const { getAllByTestId, getByText } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    expect(getAllByTestId('thread-turn-user')).toHaveLength(1);
    expect(getAllByTestId('thread-turn-crew')).toHaveLength(1);
    expect(getByText('hello there')).toBeTruthy();
    // "drift" maps to the crew display name "Anchor".
    expect(getByText('Anchor')).toBeTruthy();
  });

  it('renders turn text as escaped plain text (no HTML injection)', () => {
    const turns = [
      turn({ seq: 0, role: 'crew', text: '<img src=x onerror=alert(1)>', workload: 'drift' }),
    ];
    const { container } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    // The malicious markup must appear as literal text, never as a real element.
    expect(container.querySelector('img')).toBeNull();
    const body = container.querySelector('.turn__text') as HTMLElement;
    expect(body.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('links a crew turn to its trace and fires onOpenTrace with the trace id', async () => {
    const onOpenTrace = vi.fn();
    const turns = [turn({ seq: 1, role: 'crew', text: 'done', trace_id: 'tid-9' })];
    const { getByTestId } = render(ConversationThread, {
      props: { turns, onOpenTrace },
    });
    await fireEvent.click(getByTestId('thread-open-trace'));
    expect(onOpenTrace).toHaveBeenCalledWith('tid-9');
  });

  it('omits the trace link when a crew turn has no trace id', () => {
    const turns = [turn({ seq: 1, role: 'crew', text: 'no trace', trace_id: null })];
    const { queryByTestId } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    expect(queryByTestId('thread-open-trace')).toBeNull();
  });

  it('surfaces a PR CTA on a crew turn that opened an infra PR', () => {
    const turns = [
      turn({
        seq: 1,
        role: 'crew',
        text: 'opened a PR',
        iac_pr: { pr_number: 42, pr_url: 'https://github.com/o/r/pull/42' },
      }),
    ];
    const { getByTestId } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    const link = getByTestId('thread-pr-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/iac-approvals/42');
    expect(link.textContent).toContain('42');
  });

  it('renders a pending crew turn as a typing indicator with no reply text or actions', () => {
    // A live "thinking" bubble: pending + optimistic, and even though a trace id
    // and PR are present, the action links stay suppressed until it persists.
    const turns = [
      turn({
        seq: 1,
        role: 'crew',
        text: '',
        trace_id: 'tid-live',
        iac_pr: { pr_number: 7, pr_url: 'https://github.com/o/r/pull/7' },
        optimistic: true,
        pending: true,
      }),
    ];
    const { getByTestId, queryByTestId, getByText, container } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    // The typing indicator stands in for the reply body…
    expect(getByTestId('thread-typing')).toBeTruthy();
    expect(container.querySelector('.turn__text')).toBeNull();
    // …and the assistive-tech line announces the in-progress state.
    expect(getByText('Generating reply…')).toBeTruthy();
    // No action links on an optimistic turn, even with a trace id + PR present.
    expect(queryByTestId('thread-open-trace')).toBeNull();
    expect(queryByTestId('thread-pr-link')).toBeNull();
  });

  it('shows the reply text but no actions on an optimistic (not-yet-persisted) crew turn', () => {
    // The reply has landed in the bubble (pending=false) but the turn has not
    // settled into the thread yet, so its links are still withheld.
    const turns = [
      turn({
        seq: 1,
        role: 'crew',
        text: 'here is the streamed reply',
        trace_id: 'tid-live',
        iac_pr: { pr_number: 7, pr_url: 'https://github.com/o/r/pull/7' },
        optimistic: true,
        pending: false,
      }),
    ];
    const { getByText, queryByTestId } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    expect(getByText('here is the streamed reply')).toBeTruthy();
    expect(queryByTestId('thread-typing')).toBeNull();
    expect(queryByTestId('thread-open-trace')).toBeNull();
    expect(queryByTestId('thread-pr-link')).toBeNull();
  });

  it('regression: a persisted crew turn renders text plus the open-trace and PR links', () => {
    const turns = [
      turn({
        seq: 1,
        role: 'crew',
        text: 'the settled reply',
        trace_id: 'tid-done',
        iac_pr: { pr_number: 9, pr_url: 'https://github.com/o/r/pull/9' },
      }),
    ];
    const { getByText, getByTestId } = render(ConversationThread, {
      props: { turns, onOpenTrace: noop },
    });
    expect(getByText('the settled reply')).toBeTruthy();
    expect(getByTestId('thread-open-trace')).toBeTruthy();
    expect(getByTestId('thread-pr-link')).toBeTruthy();
  });
});
