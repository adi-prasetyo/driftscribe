// The empty new-chat state (ds-jns PR 3, Task 3.1).
//
// A fresh chat is a front door, not the tail of an empty log: a greeting, the
// composer, and four example questions. What is pinned HERE is the wiring —
// when the state shows, what a chip actually does, and when it gets out of the
// way. The GEOMETRY (composer centred rather than pinned, the pause banner
// still at the top, a short viewport scrolling instead of clipping) needs a
// layout engine and lives in tests/visual/chat-shell.visual.ts.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';
import { enMessages } from '../../src/locales';

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

const GRAPH = {
  generated_at: null,
  project: 'demo-proj',
  caveat: '',
  degraded: false,
  degraded_reason: null,
  totals: { resources: 1, managed: 0, drift: 1 },
  groups: [],
  edges: [],
};

/** The smallest body CapabilityCard's structural check accepts and its template
 *  can render end to end. Content fidelity is CapabilityCard.test.ts's job with
 *  the verbatim DTO fixture; what is tested from here is the wiring. */
const CAPS = {
  version: 1,
  provenance: 'generated',
  iam_note: 'least privilege',
  workloads: [],
  human_gates: [{ id: 'iac_apply', title: 'IaC plan apply', description: 'you approve the plan' }],
  denylist: { summary: 'nothing destructive', enforced_at: ['plan'], rules: [] },
};

/** Every endpoint the chat view touches on boot, all empty. */
type Override = (url: string) => Response | null | Promise<Response | null>;

function stubFetch(extra: Override = () => null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const override = await extra(url);
      if (override) return override;
      if (url.includes('/conversations')) return okJson({ conversations: [] });
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(GRAPH);
      return okJson({});
    }),
  );
}

/** The workload on the last POST /chat, read off the wire. */
function lastChatPostWorkload(): string | undefined {
  const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } })
    .mock.calls;
  const post = calls
    .filter(([u, i]) => String(u).includes('/chat') && i?.method === 'POST')
    .at(-1);
  if (!post?.[1]?.body) return undefined;
  return JSON.parse(String(post[1].body)).workload;
}

function chatPosts(): unknown[] {
  const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } })
    .mock.calls;
  return calls.filter(([u, i]) => String(u).includes('/chat') && i?.method === 'POST');
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.sessionStorage.setItem('driftscribe_token', 'tok');
  window.localStorage.setItem('driftscribe_tour_done', '1');
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  // EN so the chip assertions read against the catalog the test imports.
  window.localStorage.setItem('driftscribe.locale', 'en');
  history.replaceState(null, '', '/?view=chat');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — the empty new-chat state', () => {
  it('greets, and offers one example question per crew flavour', async () => {
    stubFetch();
    const { findByTestId, getAllByTestId } = render(App);

    expect((await findByTestId('chat-empty-greeting')).textContent?.trim()).toBe(
      enMessages['chat.empty.greeting'],
    );

    // Four chips, and each one carries EXACTLY a catalog string — no chip whose
    // visible label differs from the prompt it will drop in the box.
    const chips = getAllByTestId('chat-empty-chip').map((b) => b.textContent?.trim());
    expect(chips).toEqual([
      enMessages['chat.empty.chip.explore'],
      enMessages['chat.empty.chip.anchor'],
      enMessages['chat.empty.chip.patch'],
      enMessages['chat.empty.chip.provision'],
    ]);
    // None of them names its crew: the operator never has to learn the taxonomy
    // to ask a question, which is what the crew picker was removed for.
    for (const c of chips) {
      expect(c).not.toMatch(/Anchor|Patch|Provision|Explore/i);
    }
  });

  it('replaces the estate diagram and the capability card, rather than sitting under them', async () => {
    // The front door is what is on a fresh chat now. Both of these belong
    // elsewhere (the desk owns the estate; the capability card is behind the
    // link below) and PR 3 Task 3.3 deletes both mounts outright.
    stubFetch();
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('chat-empty-chips');
    expect(queryByTestId('infra-panel')).toBeNull();
    expect(queryByTestId('capability-card')).toBeNull();
  });

  it('keeps the safety cage one click away, and fetches nothing until it is asked for', async () => {
    // The capability detail is a long read, asked for once on the way in. As an
    // inline card it was a column of it to scroll past on every visit; as a link
    // it is a footnote that costs nothing until clicked. That "costs nothing" is
    // half the point of the move, so the no-fetch-yet assertion is not a detail.
    const capPaths: string[] = [];
    stubFetch((url) => {
      if (!/\/capabilities$/.test(url)) return null;
      capPaths.push(url);
      return okJson(CAPS);
    });
    const { findByTestId, queryByTestId } = render(App);

    const link = await findByTestId('capability-link');
    // The label IS the destination's title — no guessing what the click gets you.
    expect(link.textContent?.trim()).toBe(enMessages['capability.card.title']);
    expect(capPaths).toHaveLength(0);
    expect(queryByTestId('capability-card')).toBeNull();

    await fireEvent.click(link);

    // Open on arrival: the modal contains the answer, not a disclosure that
    // still has to be opened to reveal it.
    await findByTestId('cap-gates');
    expect(queryByTestId('cap-summary')).toBeNull();
    await waitFor(() => expect(capPaths).toHaveLength(1));
    // And it is a real dialog, titled the same as the link that opened it.
    // Located FROM the card rather than by `document.querySelector('dialog')`:
    // the chat view mounts other Modals (the rails' search), and the first
    // <dialog> in the document is a shut one whose body has not rendered.
    const dialog = (await findByTestId('capability-card')).closest('dialog');
    expect(dialog).toBeTruthy();
    expect(dialog?.textContent).toContain(enMessages['capability.card.title']);
  });

  it('a chip PREFILLS the composer with its own text and sends nothing', async () => {
    // Same contract as Adopt (design §6): the operator reads what they are
    // about to ask, edits it if they want, and presses Send themselves.
    stubFetch();
    const { findAllByTestId, container } = render(App);
    const chips = await findAllByTestId('chat-empty-chip');
    await fireEvent.click(chips[1]);

    const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.value).toBe(enMessages['chat.empty.chip.anchor']));
    expect(chatPosts()).toHaveLength(0);
    // Still the front door — clicking a suggestion is not the first turn.
    expect(await findAllByTestId('chat-empty-chip')).toHaveLength(4);
  });

  it('a chip does not route the turn to a crew of its own', async () => {
    // A chip is an example question, not a routing decision: the turn goes to
    // Explore like any other opening prompt, and Explore hands off if the
    // question belongs to a sibling. (That a workload-less prefill LEAVES the
    // composer's crew alone — the part that matters on a resumed thread — is
    // pinned at the component level in ChatForm.test.ts, where a non-default
    // starting crew can tell "left alone" apart from "set to explore".)
    stubFetch();
    const { findAllByTestId } = render(App);
    const chips = await findAllByTestId('chat-empty-chip');
    // The Provision-flavoured one: if any chip were going to name a crew, this
    // is the one that would.
    await fireEvent.click(chips[3]);
    await fireEvent.submit(document.getElementById('chat-form')!);
    await waitFor(() => expect(lastChatPostWorkload()).toBe('explore'));
  });

  it('gets out of the way once the first turn lands', async () => {
    stubFetch((url) =>
      url.includes('/chat')
        ? okJson({ reply: 'nothing has drifted', trace_id: 'a'.repeat(32), conversation_id: 'c9' })
        : null,
    );
    const { findByTestId, queryByTestId, container } = render(App);
    await findByTestId('chat-empty-chips');

    const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'what changed?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => {
      expect(queryByTestId('chat-empty-greeting')).toBeNull();
      expect(queryByTestId('chat-empty-chips')).toBeNull();
      expect(queryByTestId('capability-link')).toBeNull();
    });
  });

  it('is not the state a historical replay opens in', async () => {
    // A replay has a transcript — someone else's, from the past. Offering it a
    // greeting and four fresh questions would misread the whole screen.
    const TID = 'e'.repeat(32);
    stubFetch((url) =>
      url.includes(`/trace/${TID}`)
        ? okJson({ trace_id: TID, complete: true, decision: null, events: [] })
        : null,
    );
    history.replaceState(null, '', `/?view=chat&reasoning=${TID}`);
    const { findByTestId, queryByTestId } = render(App);
    await findByTestId('historical-banner');
    expect(queryByTestId('chat-empty-greeting')).toBeNull();
    expect(queryByTestId('chat-empty-chips')).toBeNull();
  });

  it('never flashes up in front of a thread that is still loading', async () => {
    // A window where nothing is on screen and the screen is nevertheless not
    // empty. Two independent things keep the front door shut through it, and
    // this test outlives either: openConversation writes conversationId before
    // it awaits the detail, and `resumingConversation` is a term in the
    // emptiness rule. Injected separately, neither alone reddens this; injected
    // together, the greeting and four chips appear for the length of the
    // request and are then yanked away by the arriving thread. What is pinned
    // here is the BEHAVIOUR, not whichever arm currently delivers it.
    let releaseDetail!: () => void;
    const detailArrived = new Promise<void>((r) => {
      releaseDetail = r;
    });
    stubFetch(async (url) => {
      if (!/\/conversations\/c1$/.test(url)) return null;
      await detailArrived;
      return okJson({
        conversation_id: 'c1',
        workload: 'drift',
        title: 'a prior thread',
        turns: [{ seq: 0, role: 'user', text: 'what changed?', workload: 'drift' }],
      });
    });
    history.replaceState(null, '', '/?conversation=c1');
    const { findByTestId, queryByTestId } = render(App);

    // Mid-flight: the composer is up, the thread is not, and the front door is
    // still not offered. Several ticks so this is the settled state and not a
    // race the assertion happened to win.
    await waitFor(() => expect(document.getElementById('chat-form')).toBeTruthy());
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(queryByTestId('conversation-thread')).toBeNull();
    expect(queryByTestId('chat-empty-greeting')).toBeNull();

    releaseDetail();
    await findByTestId('conversation-thread');
    expect(queryByTestId('chat-empty-greeting')).toBeNull();
  });
});
