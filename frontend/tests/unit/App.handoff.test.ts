// App-level wiring for the crew handoff (slice 3): the confirmation chip, its
// two POSTs, and what happens to it around the edges — a reload, another
// device, a refusal, a typed prompt instead of a click.
//
// The JSON fallback transport is used throughout: it runs the same capture and
// settle logic as SSE, and keeps these tests about the handoff rather than
// about stream parsing (which sse.test.ts owns).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import App from '../../src/App.svelte';

function okJson(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function errJson(status: number, detail: string, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

const GRAPH = { totals: { resources: 0, managed: 0, drift: 0 }, groups: [], edges: [] };

const OFFER = {
  from: 'explore',
  to: 'provision',
  reason: 'this needs a bucket created, which I cannot do',
  nonce: 'nonce-abc',
  expires_at: '2999-01-01T00:00:00Z',
};

/** The conversation as it looks BEFORE the operator answers the suggestion. */
const PENDING_DETAIL = {
  conversation_id: 'c1',
  workload: 'explore',
  crews: ['explore'],
  title: 'can you create a bucket?',
  user_turn_count: 1,
  turns: [
    { seq: 0, role: 'user', text: 'can you create a bucket?', workload: 'explore' },
    { seq: 1, role: 'crew', text: 'I can only look, not build', workload: 'explore' },
  ],
  // The server-side half of the proposal. Note what is NOT here: the nonce.
  pending_handoff: {
    from: 'explore',
    to: 'provision',
    reason: OFFER.reason,
    expires_at: OFFER.expires_at,
  },
};

/** ...and AFTER a confirmed handoff: crew rewritten, transition row present. */
const JOINED_DETAIL = {
  conversation_id: 'c1',
  workload: 'provision',
  crews: ['explore', 'provision'],
  title: 'can you create a bucket?',
  user_turn_count: 1,
  turns: [
    ...PENDING_DETAIL.turns,
    {
      seq: 2,
      role: 'crew_change',
      text: OFFER.reason,
      workload: 'provision',
      handoff: { from: 'explore', to: 'provision' },
    },
    { seq: 3, role: 'crew', text: 'opened PR #42 with the bucket', workload: 'provision' },
  ],
};

const LIST = {
  conversations: [
    {
      conversation_id: 'c1',
      workload: 'explore',
      crews: ['explore'],
      title: 'can you create a bucket?',
      updated_at: new Date().toISOString(),
      user_turn_count: 1,
      turn_count: 2,
    },
  ],
};

type Handler = (url: string, init?: RequestInit) => Response | undefined;

/** Base stub: everything the chat view needs to mount, plus an override hook. */
function stubFetch(extra: Handler = () => undefined) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const override = extra(url, init);
      if (override) return override;
      if (url.includes('/conversations/')) return okJson(PENDING_DETAIL);
      if (url.includes('/conversations')) return okJson(LIST);
      if (url.includes('/trace/')) return okJson({ trace_id: 't', events: [], complete: true });
      if (url.includes('/decisions')) return okJson({ decisions: [] });
      if (url.includes('/infra/pending-approvals')) return okJson({ approvals: [] });
      if (url.includes('/infra/graph')) return okJson(GRAPH);
      return okJson({});
    }),
  );
}

function fetchCalls(): [RequestInfo | URL, RequestInit?][] {
  return (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls;
}

function handoffPosts(): Record<string, unknown>[] {
  return fetchCalls()
    .filter(([u, i]) => String(u).includes('/chat/handoff') && i?.method === 'POST')
    .map(([, i]) => JSON.parse(String(i!.body)));
}

/** The crew the composer would actually submit under, read from the wire.
 *
 *  The load-bearing observable for "who owns this conversation now". Asserting
 *  that the chip vanished says the click was processed; only this says the
 *  client agrees with the server about the answer. When they disagree the
 *  server's crew lock refuses the next turn, naming a crew the operator was
 *  never shown — a dead end reachable only by typing. */
async function typeAndReadWorkload(container: HTMLElement, text: string): Promise<unknown> {
  const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
  await waitFor(() => expect(input.disabled).toBe(false));
  await fireEvent.input(input, { target: { value: text } });
  await fireEvent.submit(document.getElementById('chat-form')!);
  let body: unknown;
  await waitFor(() => {
    const post = fetchCalls()
      .filter(([u, i]) => String(u).endsWith('/chat') && i?.method === 'POST')
      .at(-1);
    expect(post).toBeTruthy();
    body = JSON.parse(String(post![1]!.body)).workload;
  });
  return body;
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.sessionStorage.setItem('driftscribe_token', 'tok');
  window.localStorage.setItem('driftscribe_tour_done', '1');
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
  history.replaceState(null, '', '/?view=chat');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('App — a crew proposes a handoff', () => {
  it('renders the chip when the turn that proposed it persisted', async () => {
    stubFetch((url, init) => {
      if (url.includes('/chat') && init?.method === 'POST') {
        return okJson({
          reply: 'I can only look, not build',
          tool_calls: [],
          conversation_id: 'c1',
          handoff: OFFER,
        });
      }
      return undefined;
    });
    const { findByTestId, getByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'can you create a bucket?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    const chip = await findByTestId('handoff-chip');
    expect(chip.textContent).toContain('Explore');
    expect(chip.textContent).toContain('Provision');
    expect(getByTestId('handoff-chip-reason').textContent).toContain('bucket created');
  });

  it('takes custody of the nonce so a reload can still act on it', async () => {
    // The `done` frame is the only time this value is ever transmitted — the
    // server keeps a digest and can never re-serve it.
    stubFetch((url, init) => {
      if (url.includes('/chat') && init?.method === 'POST') {
        return okJson({ reply: 'x', tool_calls: [], conversation_id: 'c1', handoff: OFFER });
      }
      return undefined;
    });
    const { findByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'q' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    await findByTestId('handoff-chip');
    expect(JSON.parse(window.sessionStorage.getItem('ds.handoff.c1')!).nonce).toBe('nonce-abc');
  });

  it('renders NO chip when the turn did not persist (nothing to redeem against)', async () => {
    // A one-shot / paused turn echoes no conversation_id, so the backend drops
    // any proposal with it. A chip here would carry an unredeemable nonce.
    stubFetch((url, init) => {
      if (url.includes('/chat') && init?.method === 'POST') {
        return okJson({ reply: 'x', tool_calls: [], handoff: OFFER });
      }
      return undefined;
    });
    const { findByTestId, queryByTestId } = render(App);
    const input = (await findByTestId('chat-prompt')) as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'q' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    await waitFor(() => expect(queryByTestId('chat-prompt')).not.toBeNull());
    expect(queryByTestId('handoff-chip')).toBeNull();
  });
});

describe('App — confirming a handoff', () => {
  function stubConfirmFlow(handoffResponse?: Response) {
    let redeemed = false;
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        if (handoffResponse) return handoffResponse;
        redeemed = true;
        return okJson({
          reply: 'opened PR #42 with the bucket',
          tool_calls: [],
          conversation_id: 'c1',
        });
      }
      if (url.includes('/conversations/')) return okJson(redeemed ? JOINED_DETAIL : PENDING_DETAIL);
      return undefined;
    });
  }

  /** Resume a thread that already has an open proposal, with custody seeded —
   *  the state an operator is in after a reload. */
  async function renderWithChip() {
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const r = render(App);
    await fireEvent.click(await r.findByTestId('conversation-open'));
    await r.findByTestId('handoff-chip');
    return r;
  }

  it('posts the conversation, the nonce and accept:true — and never names a crew', async () => {
    // The target crew is read from the persisted proposal server-side. If the
    // client could name one, this would be a second way to ask for an arbitrary
    // workload, which is exactly what the crew-lock 409 exists to prevent.
    stubConfirmFlow();
    const r = await renderWithChip();
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    await waitFor(() => expect(handoffPosts()).toHaveLength(1));
    expect(handoffPosts()[0]).toEqual({
      conversation_id: 'c1',
      nonce: 'nonce-abc',
      accept: true,
    });
    expect(Object.keys(handoffPosts()[0])).not.toContain('workload');
  });

  it('refetches the thread so the transition row and the joining crew appear', async () => {
    // Deliberately not an optimistic append: the rows written here are a
    // server-authored transition and a crew reply with NO operator prompt in
    // front of it — a shape the client cannot reconstruct correctly.
    stubConfirmFlow();
    const r = await renderWithChip();
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    await r.findByTestId('thread-turn-crew-change');
    await waitFor(() => expect(r.getByText('opened PR #42 with the bucket')).toBeTruthy());
  });

  it('retires the chip and its nonce once the proposal is redeemed', async () => {
    stubConfirmFlow();
    const r = await renderWithChip();
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    await waitFor(() => expect(r.queryByTestId('handoff-chip')).toBeNull());
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
  });

  it('moves the composer to the crew that joined', async () => {
    // The conversation is still crew-LOCKED — the lock just moved. The next
    // typed prompt has to go to Provision or the backend 409s it.
    stubConfirmFlow();
    const r = await renderWithChip();
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    await r.findByTestId('thread-turn-crew-change');

    const input = r.container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    await fireEvent.input(input, { target: { value: 'and the lifecycle rule?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    await waitFor(() => {
      const post = fetchCalls()
        .filter(([u, i]) => String(u).endsWith('/chat') && i?.method === 'POST')
        .at(-1);
      expect(JSON.parse(String(post![1]!.body)).workload).toBe('provision');
    });
  });
});

describe('App — a redemption that answers 2xx without redeeming', () => {
  it('keeps the chip and its nonce when the kill switch refuses', async () => {
    // The pause check runs BEFORE redemption, and answers 200 with paused:true.
    // "Any 2xx means the nonce is spent" would delete custody for a proposal
    // that is still open — and since the server keeps only a digest, no reload
    // could ever bring the chip back.
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return okJson({
          reply: 'DriftScribe is paused.',
          tool_calls: [],
          session_id: '',
          paused: true,
          conversation_id: 'c1',
        });
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, getByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    await waitFor(() => expect(handoffPosts()).toHaveLength(1));
    // Chip survives, unlocked, ready to confirm again once the pause lifts.
    expect(await findByTestId('handoff-chip')).toBeTruthy();
    await waitFor(() =>
      expect((getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(false),
    );
    expect(window.sessionStorage.getItem('ds.handoff.c1')).not.toBeNull();
  });
});

describe('App — a failure AFTER the redemption committed', () => {
  it('moves with the crew instead of treating it as retryable', async () => {
    // The joining crew failed to start, but the flip already committed: the
    // nonce is spent and the conversation belongs to Provision now. Reading
    // this as an ordinary refusal would keep a dead chip AND leave the composer
    // on Explore, whose next typed turn the crew lock refuses — so the
    // "retry by typing" the backend promises would not work.
    let redeemed = false;
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        redeemed = true;
        return errJson(503, "workload 'provision' is not deployed", {
          'X-Handoff-Redeemed': '1',
        });
      }
      if (url.includes('/conversations/')) {
        return okJson(
          redeemed
            ? { ...JOINED_DETAIL, turns: JOINED_DETAIL.turns.slice(0, 3), pending_handoff: null }
            : PENDING_DETAIL,
        );
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId, getByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    // The chip is gone and its credential with it — it is spent, not retryable.
    await waitFor(() => expect(queryByTestId('handoff-chip')).toBeNull());
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
    // The transition the operator confirmed is still visible.
    await findByTestId('thread-turn-crew-change');
    // And the composer moved with it, so the retry actually works.
    const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    await fireEvent.input(input, { target: { value: 'try again' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    await waitFor(() => {
      const post = fetchCalls()
        .filter(([u, i]) => String(u).endsWith('/chat') && i?.method === 'POST')
        .at(-1);
      expect(JSON.parse(String(post![1]!.body)).workload).toBe('provision');
    });
  });

  it('retires the chip even when the post-redemption refetch fails', async () => {
    // Fail-soft on the refetch is fine — the reply is already on screen. What
    // is not fine is leaving a clickable chip holding a nonce the server
    // already burned.
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return okJson({ reply: 'opened PR #42', tool_calls: [], conversation_id: 'c1' });
      }
      if (url.includes('/conversations/')) {
        // First call (the resume) succeeds; the post-redemption refetch 500s.
        return handoffPosts().length ? errJson(500, 'boom') : okJson(PENDING_DETAIL);
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId, getByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    await waitFor(() => expect(queryByTestId('handoff-chip')).toBeNull());
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
    // ...and ownership moved anyway. This is the half the chip assertion above
    // cannot see: the refetch is the ONLY step here allowed to fail silently,
    // so hanging the crew flip off it meant a committed redemption could leave
    // the composer submitting as Explore, which the server's Provision lock
    // then refuses. The redemption already told us the answer — `offer.to`.
    expect(await typeAndReadWorkload(container, 'try again')).toBe('provision');
  });

  it('moves ownership when the JOIN fails post-commit and the refetch fails too', async () => {
    // The compounding case, and the one that most looks like "nothing
    // happened": the crew flip COMMITTED, the joining crew's first reply blew
    // up, and the refetch that would have revealed the new owner also failed.
    // Every signal the client could passively observe is an error, yet the
    // conversation genuinely belongs to Provision now — so the retry the error
    // message promises has to actually go to Provision.
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return errJson(500, 'the crew changed, but its first reply could not be started', {
          'X-Handoff-Redeemed': '1',
        });
      }
      if (url.includes('/conversations/')) {
        return handoffPosts().length ? errJson(500, 'boom') : okJson(PENDING_DETAIL);
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, getByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    await waitFor(() => expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull());
    expect(await typeAndReadWorkload(container, 'try again')).toBe('provision');
  });
});

describe('App — declining a handoff', () => {
  it('posts accept:false rather than dismissing client-side', async () => {
    // A client-side dismiss would leave the proposal live: the crew would
    // re-offer every turn and no prompt-level restraint could see the refusal.
    let declined = false;
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        declined = true;
        return okJson({
          reply: 'Staying with Explore. Provision was not brought in.',
          tool_calls: [],
          conversation_id: 'c1',
          handoff_declined: { from: 'explore', to: 'provision' },
        });
      }
      if (url.includes('/conversations/')) {
        return okJson(
          declined
            ? {
                ...PENDING_DETAIL,
                pending_handoff: null,
                turns: [
                  ...PENDING_DETAIL.turns,
                  {
                    seq: 2,
                    role: 'handoff_declined',
                    text: OFFER.reason,
                    workload: 'explore',
                    handoff: { from: 'explore', to: 'provision' },
                  },
                ],
              }
            : PENDING_DETAIL,
        );
      }
      return undefined;
    });
    const r = render(App);
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    await fireEvent.click(await r.findByTestId('conversation-open'));
    await r.findByTestId('handoff-chip');
    await fireEvent.click(r.getByTestId('handoff-decline'));

    await waitFor(() => expect(handoffPosts()).toHaveLength(1));
    expect(handoffPosts()[0].accept).toBe(false);
    // The refusal is visible in the transcript, and the chip is gone.
    await r.findByTestId('thread-turn-handoff-declined');
    await waitFor(() => expect(r.queryByTestId('handoff-chip')).toBeNull());
  });
});

describe('App — restoring a proposal across a reload', () => {
  it('rebuilds the chip when this client holds the nonce', async () => {
    stubFetch();
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    expect(await findByTestId('handoff-chip')).toBeTruthy();
  });

  it('shows NO chip on a device that never received the nonce', async () => {
    // The cross-device case: `pending_handoff` says a proposal is open, but the
    // nonce is a capability, not a view, so a shared ?conversation= link cannot
    // carry it. The thread still shows what the crew asked; the operator can
    // answer in the composer, which is the pre-handoff behaviour.
    stubFetch();
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    expect(queryByTestId('handoff-chip')).toBeNull();
  });

  it('shows no chip once the server says the proposal is closed', async () => {
    // Custody alone is not evidence: it was redeemed or superseded elsewhere.
    stubFetch((url) => {
      if (url.includes('/conversations/')) {
        return okJson({ ...PENDING_DETAIL, pending_handoff: null });
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('conversation-thread');
    expect(queryByTestId('handoff-chip')).toBeNull();
  });
});

describe('App — a refused confirmation', () => {
  async function confirmAgainst(resp: Response) {
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') return resp;
      return undefined;
    });
    const r = render(App);
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    await fireEvent.click(await r.findByTestId('conversation-open'));
    await r.findByTestId('handoff-chip');
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    return r;
  }

  it('explains an expiry and locks the chip', async () => {
    const r = await confirmAgainst(
      errJson(410, 'this handoff has expired', { 'X-Handoff-Refusal': 'expired' }),
    );
    const err = await r.findByTestId('handoff-error');
    expect(err.textContent).toContain('expired');
    expect((r.getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(true);
  });

  // The next two are the same HTTP status with opposite recoveries — which is
  // exactly why the server sends a reason token alongside it. Reading the
  // status alone would tell an operator to give up on a proposal that would
  // work fine a moment later.
  it('reads a 409 "already used" as dead', async () => {
    const r = await confirmAgainst(
      errJson(409, 'no crew handoff is awaiting confirmation', {
        'X-Handoff-Refusal': 'no_pending',
      }),
    );
    expect((await r.findByTestId('handoff-error')).textContent).toContain('no longer available');
    expect((r.getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(true);
  });

  it('the tab that LOSES a two-tab race still learns who won', async () => {
    // Duplicate a tab and both hold the same nonce in sessionStorage. Tab A
    // accepts; tab B's chip is still on screen, and its click gets 409
    // no_pending. Disabling the chip tells tab B the click failed — it does
    // NOT tell it that Provision now owns the conversation, and `no_pending`
    // cannot say on its own (accepted elsewhere, declined elsewhere and
    // expired all produce it). So tab B has to ask, or it sits on Explore and
    // every message it sends is refused by a lock it never saw.
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return errJson(409, 'no crew handoff is awaiting confirmation', {
          'X-Handoff-Refusal': 'no_pending',
        });
      }
      if (url.includes('/conversations/')) {
        // The resume still sees the proposal open (tab B loaded first); by the
        // time it asks again, tab A has already moved the conversation.
        return handoffPosts().length ? okJson(JOINED_DETAIL) : okJson(PENDING_DETAIL);
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, getByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    // The explanation survives — it is the only thing telling this operator
    // why their click did nothing, so reconciling must not wipe it.
    expect((await findByTestId('handoff-error')).textContent).toContain('no longer available');
    expect(await typeAndReadWorkload(container, 'carry on then')).toBe('provision');
  });

  it('reads a 409 "a turn is running" as retryable', async () => {
    const r = await confirmAgainst(
      errJson(409, 'this conversation already has a turn in flight', {
        'X-Handoff-Refusal': 'busy',
      }),
    );
    expect((await r.findByTestId('handoff-error')).textContent).toContain(
      'already has a turn running',
    );
    // Eventually, not immediately: a refusal now reconciles ownership before
    // it releases the chip, and the button stays locked across that fetch so
    // a second click cannot race it.
    await waitFor(() =>
      expect((r.getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it('treats an unknown refusal as retryable rather than stranding the chip', async () => {
    const r = await confirmAgainst(errJson(500, 'boom'));
    await r.findByTestId('handoff-error');
    // Eventually, not immediately: a refusal now reconciles ownership before
    // it releases the chip, and the button stays locked across that fetch so
    // a second click cannot race it.
    await waitFor(() =>
      expect((r.getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(false),
    );
  });

  it('forgets a dead proposal so a reload does not resurrect it', async () => {
    await confirmAgainst(
      errJson(409, 'this confirmation is not valid', { 'X-Handoff-Refusal': 'invalid_nonce' }),
    );
    await waitFor(() => expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull());
  });

  it('a bad nonce must NOT be mistaken for a rejected operator token', async () => {
    // If the backend answered 403 here, apiFetch would clear the stored token
    // and raise the auth modal — throwing an anonymous demo visitor out for
    // clicking a superseded chip. The token must survive a refused handoff.
    const r = await confirmAgainst(
      errJson(409, 'this confirmation is not valid', { 'X-Handoff-Refusal': 'invalid_nonce' }),
    );
    await r.findByTestId('handoff-error');
    expect(window.sessionStorage.getItem('driftscribe_token')).toBe('tok');
    expect(r.queryByTestId('auth-panel')).toBeNull();
  });

  it('runs no turn: a refusal must not append anything to the thread', async () => {
    const r = await confirmAgainst(
      errJson(410, 'expired', { 'X-Handoff-Refusal': 'expired' }),
    );
    await r.findByTestId('handoff-error');
    expect(r.queryByTestId('thread-turn-crew-change')).toBeNull();
    expect(r.queryByTestId('thread-turn-handoff-declined')).toBeNull();
  });
});

describe('App — leaving the suggestion unanswered', () => {
  it('retires the suggestion: the operator answered it by carrying on', async () => {
    // Leaving a clickable chip under a NEWER reply would attach the suggestion
    // to a question it was never about. If the crew still wants the sibling it
    // proposes again, which supersedes the old proposal server-side anyway.
    stubFetch((url, init) => {
      if (url.endsWith('/chat') && init?.method === 'POST') {
        return okJson({ reply: 'here is more detail', tool_calls: [], conversation_id: 'c1' });
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');

    const input = container.querySelector('#prompt-input') as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    await fireEvent.input(input, { target: { value: 'actually, tell me more first' } });
    await fireEvent.submit(document.getElementById('chat-form')!);

    await waitFor(() => expect(queryByTestId('handoff-chip')).toBeNull());
    // And it stays gone across a reload — the view and the stored custody agree.
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
  });

  it('leaving the chat view takes the chip with the thread it belongs to', async () => {
    // navigate() away from chat drops the open thread, and redeeming needs that
    // thread's id — a chip left on screen would render a Confirm button whose
    // handler returns immediately on the null id. A dead button is exactly what
    // this whole design replaced.
    stubFetch();
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId, getByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');

    await fireEvent.click(getByTestId('nav-desk'));
    await waitFor(() => expect(queryByTestId('handoff-chip')).toBeNull());
    // Custody survives — the thread is still in the rail, and reopening it
    // brings the chip back. (The rail only mounts on the chat view, so come
    // back first, the way an operator would.)
    expect(window.sessionStorage.getItem('ds.handoff.c1')).not.toBeNull();
    await fireEvent.click(getByTestId('nav-chat'));
    await fireEvent.click(await findByTestId('conversation-open'));
    expect(await findByTestId('handoff-chip')).toBeTruthy();
  });

  it('New chat carries the chip away but keeps the proposal reachable from the rail', async () => {
    // The proposal belongs to that thread, not to the screen. Forgetting the
    // nonce here would punish the operator for looking away.
    stubFetch();
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, queryByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(await findByTestId('composer-new-chat'));
    await waitFor(() => expect(queryByTestId('handoff-chip')).toBeNull());
    expect(window.sessionStorage.getItem('ds.handoff.c1')).not.toBeNull();

    // Reopening the thread brings it back.
    await fireEvent.click(await findByTestId('conversation-open'));
    expect(await findByTestId('handoff-chip')).toBeTruthy();
  });
});

describe('App — the client does not know what happened', () => {
  // A 2xx is not an answer on its own: the pause check replies 200 BEFORE
  // redeeming, so a truncated success and a truncated "paused, nothing
  // happened" arrive identically. The client cannot tell them apart from the
  // response — so it asks, and the SERVER's answer decides. These two are the
  // same interrupted request with opposite truths behind it.

  /** Confirm against a 200 whose body is truncated, with the conversation
   *  reported as `detail` when the client asks what actually happened. */
  async function confirmAgainstTruncated(detail: unknown) {
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return new Response('{"reply": "trunca', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/conversations/')) {
        return handoffPosts().length ? okJson(detail) : okJson(PENDING_DETAIL);
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const r = render(App);
    await fireEvent.click(await r.findByTestId('conversation-open'));
    await r.findByTestId('handoff-chip');
    await fireEvent.click(r.getByTestId('handoff-confirm'));
    return r;
  }

  it('keeps custody when the server says the proposal is still open', async () => {
    // The paused case whose single frame was lost. Nothing was redeemed, so
    // discarding the nonce would strand a live proposal permanently: the
    // server keeps only a digest and can never reissue it.
    const r = await confirmAgainstTruncated(PENDING_DETAIL);

    // Settled = the chip is interactable again. Polling storage for the nonce
    // would pass instantly, since it is written on this test's first line.
    await waitFor(() =>
      expect((r.getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(false),
    );
    expect(JSON.parse(window.sessionStorage.getItem('ds.handoff.c1')!).nonce).toBe('nonce-abc');
  });

  it('drops custody when the server says the proposal is gone', async () => {
    // Same truncated response, opposite truth: it committed. Holding the nonce
    // would only buy a button that fails, so the chip retires and the composer
    // moves to the crew that now owns the thread.
    const r = await confirmAgainstTruncated(JOINED_DETAIL);

    await waitFor(() => expect(r.queryByTestId('handoff-chip')).toBeNull());
    expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull();
    expect(await typeAndReadWorkload(r.container, 'what happened?')).toBe('provision');
  });

  it('a DECLINE with a failed refetch leaves the crew exactly where it was', async () => {
    // The mirror of the accept case, and the reason ownership is derived from
    // `accept` rather than from `offer.to` alone: declining moves nothing. A
    // successful refetch would paper over a regression here, so this one fails
    // the refetch to make the direct decision the only thing under test.
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        return okJson({
          reply: 'Staying with Explore. Provision was not brought in.',
          tool_calls: [],
          conversation_id: 'c1',
          handoff_declined: { from: 'explore', to: 'provision' },
        });
      }
      if (url.includes('/conversations/')) {
        return handoffPosts().length ? errJson(500, 'boom') : okJson(PENDING_DETAIL);
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, getByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-decline'));

    await waitFor(() => expect(window.sessionStorage.getItem('ds.handoff.c1')).toBeNull());
    expect(await typeAndReadWorkload(container, 'ok then')).toBe('explore');
  });

  it('adopts the crew the lock names when a turn is refused', async () => {
    // The backstop. Every path that moves a conversation also moves the
    // composer, and every one of them can fail on its own — so the refusal
    // itself carries the answer. Without this, a client that fell out of step
    // was stuck: refused each time, by a message naming the crew it needed.
    stubFetch((url, init) => {
      if (url.endsWith('/chat') && init?.method === 'POST') {
        const first = fetchCalls().filter(
          ([u, i]) => String(u).endsWith('/chat') && i?.method === 'POST',
        ).length === 1;
        return first
          ? errJson(409, "conversation is locked to crew 'provision'", {
              'X-Conversation-Crew': 'provision',
            })
          : undefined;
      }
      return undefined;
    });
    const { findByTestId, container } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('chat-prompt');

    expect(await typeAndReadWorkload(container, 'first try')).toBe('explore');
    expect(await typeAndReadWorkload(container, 'second try')).toBe('provision');
  });
});

// ---------------------------------------------------------------------------
// A committed redemption whose joining crew failed to run (ds-jns).
//
// The transition persisted; the joining crew's first reply did not. The thread
// therefore refetches — and that refetch replaces every overlay in one
// synchronous block, deliberately, because an overlay keyed against the OLD
// turn count collides with the refetched rows. The explanation still has to
// survive it, or the operator is left with a crew change and no account of the
// missing reply.
// ---------------------------------------------------------------------------
describe('App — a committed handoff whose join failed keeps its explanation', () => {
  it('re-seats the failure turn after the successful refetch', async () => {
    let redeemed = false;
    stubFetch((url, init) => {
      if (url.includes('/chat/handoff') && init?.method === 'POST') {
        redeemed = true;
        return errJson(503, "workload 'provision' is not deployed", {
          'X-Handoff-Redeemed': '1',
        });
      }
      if (url.includes('/conversations/')) {
        // The refetch SUCCEEDS — the branch where the old code lost the message.
        return okJson(
          redeemed
            ? { ...JOINED_DETAIL, turns: JOINED_DETAIL.turns.slice(0, 3), pending_handoff: null }
            : PENDING_DETAIL,
        );
      }
      return undefined;
    });
    window.sessionStorage.setItem('ds.handoff.c1', JSON.stringify(OFFER));
    const { findByTestId, getByTestId } = render(App);
    await fireEvent.click(await findByTestId('conversation-open'));
    await findByTestId('handoff-chip');
    await fireEvent.click(getByTestId('handoff-confirm'));

    // The confirmed transition is in the thread…
    await findByTestId('thread-turn-crew-change');
    // …and so is the reason the joining crew never answered.
    await waitFor(() => expect(getByTestId('thread-turn-error')).toBeTruthy());
    expect(getByTestId('conversation-thread').textContent).toContain(
      'its first reply failed',
    );
  });
});
