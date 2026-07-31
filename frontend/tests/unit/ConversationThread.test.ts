import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import ConversationThread from '../../src/components/ConversationThread.svelte';
import { createTraceCache } from '../../src/lib/traceCache';
import type { ConversationTurn } from '../../src/lib/types';

afterEach(cleanup);

// The thread no longer owns an open-trace callback — each crew turn carries an
// inline ReasoningDisclosure reading this cache. A never-answering `call` keeps
// these tests about the THREAD: the disclosure's own fetch behaviour is pinned
// in ReasoningDisclosure.test.ts.
function makeCache() {
  return createTraceCache(async () => new Response('{}', { status: 200 }));
}

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
      props: { turns, cache: makeCache() },
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
      props: { turns, cache: makeCache() },
    });
    // The malicious markup must appear as literal text, never as a real element.
    expect(container.querySelector('img')).toBeNull();
    const body = container.querySelector('.turn__text') as HTMLElement;
    expect(body.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('carries the reasoning inline on a crew turn with a trace id', () => {
    // Replaces the old "open trace" button, which swapped the whole column
    // into replay mode. The reasoning now expands in place, under the reply
    // that produced it.
    const turns = [turn({ seq: 1, role: 'crew', text: 'done', trace_id: 'tid-9' })];
    const { getByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(getByTestId('reasoning-disclosure')).toBeTruthy();
    expect(getByTestId('reasoning-disclosure').getAttribute('aria-expanded')).toBe('false');
  });

  it('places the reasoning line between the crew header and the reply body', () => {
    const turns = [turn({ seq: 1, role: 'crew', text: 'the reply', trace_id: 'tid-9' })];
    const { container } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    const bubble = container.querySelector('.bubble--crew') as HTMLElement;
    const order = [...bubble.children].map((n) =>
      n.classList.contains('turn__byline')
        ? 'byline'
        : n.querySelector('[data-testid="reasoning-disclosure"]')
          ? 'reasoning'
          : n.classList.contains('turn__text')
            ? 'text'
            : 'other',
    );
    expect(order.slice(0, 3)).toEqual(['byline', 'reasoning', 'text']);
  });

  it('omits the reasoning line when a crew turn has no trace id', () => {
    const turns = [turn({ seq: 1, role: 'crew', text: 'no trace', trace_id: null })];
    const { queryByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(queryByTestId('reasoning-disclosure')).toBeNull();
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
      props: { turns, cache: makeCache() },
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
      props: { turns, cache: makeCache() },
    });
    // The typing indicator stands in for the reply body…
    expect(getByTestId('thread-typing')).toBeTruthy();
    expect(container.querySelector('.turn__text')).toBeNull();
    // …and the assistive-tech line announces the in-progress state.
    expect(getByText('Generating reply…')).toBeTruthy();
    // The reasoning line renders even though the turn has not persisted —
    // watching the crew think IS the live turn. Expanding it only reads the
    // per-trace cache, so it cannot drop the in-flight settle the way the old
    // open-trace button could.
    expect(getByTestId('reasoning-disclosure')).toBeTruthy();
    // The PR link still waits for persistence: it points at an approval page
    // for a turn the backend has not committed.
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
    const { getByText, getByTestId, queryByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(getByText('here is the streamed reply')).toBeTruthy();
    expect(queryByTestId('thread-typing')).toBeNull();
    expect(getByTestId('reasoning-disclosure')).toBeTruthy();
    expect(queryByTestId('thread-pr-link')).toBeNull();
  });

  it('regression: a persisted crew turn renders text plus the reasoning line and PR link', () => {
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
      props: { turns, cache: makeCache() },
    });
    expect(getByText('the settled reply')).toBeTruthy();
    expect(getByTestId('reasoning-disclosure')).toBeTruthy();
    expect(getByTestId('thread-pr-link')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Crew-handoff transition rows.
//
// A conversation's crew used to be fixed for life. Now a confirmed handoff
// rewrites it, and the thread is the one place that change is legible — so
// these rows are the transcript's record that the conversation changed hands.
// They are server-authored: nobody said them, which is why they must not render
// as a crew bubble.
// ---------------------------------------------------------------------------

describe('ConversationThread — crew transitions', () => {
  it('renders an accepted handoff as a transition row naming both crews', () => {
    const turns = [
      turn({ seq: 0, role: 'user', text: 'can you create a bucket?', workload: 'explore' }),
      turn({
        seq: 1,
        role: 'crew_change',
        text: 'this needs a bucket created',
        workload: 'provision',
        handoff: { from: 'explore', to: 'provision' },
      }),
    ];
    const { getByTestId, queryAllByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    const row = getByTestId('thread-turn-crew-change');
    // Both crews, by display name. A row that named only the survivor would
    // leave the reader working out who left.
    expect(row.textContent).toContain('Explore');
    expect(row.textContent).toContain('Provision');
    // NOT a crew bubble: no crew said this.
    expect(queryAllByTestId('thread-turn-crew')).toHaveLength(0);
  });

  it('renders a declined handoff distinctly, and says who stayed', () => {
    const turns = [
      turn({
        seq: 1,
        role: 'handoff_declined',
        text: 'this needs a bucket created',
        workload: 'explore',
        handoff: { from: 'explore', to: 'provision' },
      }),
    ];
    const { getByTestId, queryByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(queryByTestId('thread-turn-crew-change')).toBeNull();
    const row = getByTestId('thread-turn-handoff-declined');
    expect(row.textContent).toContain('Explore');
    expect(row.textContent).toContain('Provision');
  });

  it("carries the proposing crew's reason as escaped text, never as markup", () => {
    const turns = [
      turn({
        seq: 1,
        role: 'crew_change',
        text: '<img src=x onerror=alert(1)>',
        workload: 'provision',
        handoff: { from: 'explore', to: 'provision' },
      }),
    ];
    const { container, getByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(container.querySelector('img')).toBeNull();
    expect(getByTestId('thread-transition-reason').textContent).toContain(
      '<img src=x onerror=alert(1)>',
    );
  });

  it('omits the reason block when the transition carries none', () => {
    const turns = [
      turn({
        seq: 1,
        role: 'crew_change',
        text: '',
        workload: 'provision',
        handoff: { from: 'explore', to: 'provision' },
      }),
    ];
    const { queryByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(queryByTestId('thread-transition-reason')).toBeNull();
  });

  it('gives a transition row no reasoning/PR actions — it is a record, not a turn', () => {
    const turns = [
      turn({
        seq: 1,
        role: 'crew_change',
        text: 'handing over',
        workload: 'provision',
        trace_id: 'tid-x',
        handoff: { from: 'explore', to: 'provision' },
      }),
    ];
    const { queryByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(queryByTestId('reasoning-disclosure')).toBeNull();
    expect(queryByTestId('thread-pr-link')).toBeNull();
  });

  it('still reads sensibly if a transition row arrives without its handoff pair', () => {
    // Defensive: a truncated/legacy row must not render an empty proper noun
    // where a crew name belongs.
    const turns = [
      turn({ seq: 1, role: 'crew_change', text: '', workload: 'provision' }),
    ];
    const { getByTestId } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    expect(getByTestId('thread-turn-crew-change').textContent).toContain('Provision');
  });

  it('interleaves transitions with the surrounding dialogue in seq order', () => {
    // The whole point of the row is WHERE it sits: the turns before it belong
    // to one crew and the turns after it to another.
    const turns = [
      turn({ seq: 0, role: 'user', text: 'can you create a bucket?', workload: 'explore' }),
      turn({ seq: 1, role: 'crew', text: 'I can only look', workload: 'explore' }),
      turn({
        seq: 2,
        role: 'crew_change',
        text: 'needs a bucket',
        workload: 'provision',
        handoff: { from: 'explore', to: 'provision' },
      }),
      turn({ seq: 3, role: 'crew', text: 'opened a PR', workload: 'provision' }),
    ];
    const { container } = render(ConversationThread, {
      props: { turns, cache: makeCache() },
    });
    const rows = Array.from(container.querySelectorAll('li')).map((li) =>
      li.getAttribute('data-testid'),
    );
    expect(rows).toEqual([
      'thread-turn-user',
      'thread-turn-crew',
      'thread-turn-crew-change',
      'thread-turn-crew',
    ]);
  });
});
