// HandoffChip — the confirmation that replaces the crew picker.
//
// The picker asked the operator to name a specialist before they had said what
// they wanted. The chip appears only after a crew has read the question and
// decided it belongs elsewhere, and it names the concrete route. So these tests
// mostly pin the honesty properties: it says which crew is arriving, it shows
// the crew's own reason as inert text, and it never quietly acts on its own.
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import HandoffChip from '../../src/components/HandoffChip.svelte';
import type { HandoffOffer } from '../../src/lib/sse';

afterEach(cleanup);

const noop = () => {};

function offer(over: Partial<HandoffOffer> = {}): HandoffOffer {
  return {
    from: 'explore',
    to: 'provision',
    reason: 'this needs a bucket created, which I cannot do',
    nonce: 'n0nc3',
    expires_at: '2026-07-28T12:15:00Z',
    ...over,
  };
}

describe('HandoffChip', () => {
  it('names BOTH crews — who is asking and who would arrive', () => {
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm: noop, onDecline: noop },
    });
    const text = getByTestId('handoff-chip').textContent ?? '';
    // Display names, not the symbolic workload keys the backend uses.
    expect(text).toContain('Explore');
    expect(text).toContain('Provision');
    expect(text).not.toContain('provision');
  });

  it('puts the arriving crew on the confirm button, not a bare "OK"', () => {
    // Confirming RUNS that crew immediately, so the button has to say what will
    // happen — this is the human-reads-it step the injection story leans on.
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm: noop, onDecline: noop },
    });
    expect(getByTestId('handoff-confirm').textContent).toContain('Provision');
  });

  it("renders the crew's reason as escaped text, never as markup", () => {
    // Model-authored text about operator-supplied content — the same XSS stance
    // the thread takes for every reply.
    const { container, getByTestId } = render(HandoffChip, {
      props: {
        offer: offer({ reason: '<img src=x onerror=alert(1)>' }),
        onConfirm: noop,
        onDecline: noop,
      },
    });
    expect(container.querySelector('img')).toBeNull();
    expect(getByTestId('handoff-chip-reason').textContent).toContain(
      '<img src=x onerror=alert(1)>',
    );
  });

  it('omits the reason block entirely when the crew gave none', () => {
    const { queryByTestId } = render(HandoffChip, {
      props: { offer: offer({ reason: '' }), onConfirm: noop, onDecline: noop },
    });
    expect(queryByTestId('handoff-chip-reason')).toBeNull();
  });

  it('fires onConfirm / onDecline and never acts by itself', () => {
    const onConfirm = vi.fn();
    const onDecline = vi.fn();
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm, onDecline },
    });
    // Nothing has happened just by rendering.
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onDecline).not.toHaveBeenCalled();
    void fireEvent.click(getByTestId('handoff-confirm'));
    void fireEvent.click(getByTestId('handoff-decline'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onDecline).toHaveBeenCalledTimes(1);
  });

  it('gives decline equal standing — it is a real POST, not an × dismiss', () => {
    // Declining burns the proposal and records the refusal so the crew stops
    // re-offering; a dismiss affordance would misrepresent that.
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm: noop, onDecline: noop },
    });
    const decline = getByTestId('handoff-decline') as HTMLButtonElement;
    expect(decline.tagName).toBe('BUTTON');
    expect(decline.textContent?.trim()).toBeTruthy();
  });

  it('locks both buttons and narrates progress while a POST is in flight', () => {
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), pending: true, onConfirm: noop, onDecline: noop },
    });
    expect((getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId('handoff-decline') as HTMLButtonElement).disabled).toBe(true);
    expect(getByTestId('handoff-confirm').textContent).toContain('Provision');
  });

  it('locks when the composer is unavailable — confirming would start a turn', () => {
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), disabled: true, onConfirm: noop, onDecline: noop },
    });
    expect((getByTestId('handoff-confirm') as HTMLButtonElement).disabled).toBe(true);
    expect((getByTestId('handoff-decline') as HTMLButtonElement).disabled).toBe(true);
  });

  it('reports a refusal assertively — the operator clicked and nothing happened', () => {
    const { getByTestId } = render(HandoffChip, {
      props: {
        offer: offer(),
        errorText: 'This suggestion expired.',
        onConfirm: noop,
        onDecline: noop,
      },
    });
    const err = getByTestId('handoff-error');
    expect(err.textContent).toContain('expired');
    expect(err.getAttribute('role')).toBe('alert');
  });

  it('shows no error region when there is nothing to report', () => {
    const { queryByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm: noop, onDecline: noop },
    });
    expect(queryByTestId('handoff-error')).toBeNull();
  });

  it('is announced as a confirmation the operator owes an answer to', () => {
    const { getByTestId } = render(HandoffChip, {
      props: { offer: offer(), onConfirm: noop, onDecline: noop },
    });
    expect(getByTestId('handoff-chip').getAttribute('aria-label')).toContain(
      'awaiting your confirmation',
    );
  });
});
