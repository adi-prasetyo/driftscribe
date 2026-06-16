import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import ChatForm from '../../src/components/ChatForm.svelte';

// Prefill is the Phase-4 adopt-button bridge: the Adopt affordance on the resource
// map prefills the chat input (text + workload) WITHOUT sending it — the operator
// stays in charge. An epoch counter lets the same/another Adopt click re-apply
// after the operator edits the draft. Design §2.6; Codex review 019eb572.

afterEach(cleanup);

const noop = () => {};

/** The workload the crew picker currently has selected (its checked radio). */
function checkedWorkload(): string | undefined {
  return (document.querySelector('input[type="radio"]:checked') as HTMLInputElement | null)?.value;
}

describe('ChatForm — prefill', () => {
  it('applies the prefilled text + workload and focuses the input', async () => {
    const { getByTestId } = render(ChatForm, {
      props: {
        onSubmit: noop,
        prefill: { text: 'Adopt the Storage bucket `x` into IaC management.', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => {
      expect(input.value).toBe('Adopt the Storage bucket `x` into IaC management.');
    });
    expect(checkedWorkload()).toBe('provision');
    // The $effect focuses the input so the operator can edit / press Send.
    expect(document.activeElement).toBe(input);
  });

  it('re-applies when the epoch bumps with new text (overwrites the draft)', async () => {
    const { getByTestId, rerender } = render(ChatForm, {
      props: {
        onSubmit: noop,
        prefill: { text: 'first', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe('first'));

    await rerender({
      onSubmit: noop,
      prefill: { text: 'second', workload: 'drift', epoch: 2 },
    });
    await waitFor(() => expect(input.value).toBe('second'));
    expect(checkedWorkload()).toBe('drift');
  });

  it('does NOT re-apply when prefill stays the same epoch (operator edits survive)', async () => {
    const { getByTestId, rerender } = render(ChatForm, {
      props: {
        onSubmit: noop,
        prefill: { text: 'seed', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe('seed'));

    // Operator edits the draft...
    input.value = 'operator edit';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    // ...and an unrelated rerender keeps the SAME epoch → no clobber.
    await rerender({
      onSubmit: noop,
      prefill: { text: 'seed', workload: 'provision', epoch: 1 },
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(input.value).toBe('operator edit');
  });

  it('renders normally with no prefill prop', () => {
    const { getByTestId } = render(ChatForm, { props: { onSubmit: noop } });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    expect(input.value).toBe('');
  });

  it('applies a boot-seeded explore-workload prefill on mount without submitting', async () => {
    // Simulates arriving from the approval page's "ask about this change" link:
    // App boots with initialChatPrefill(?ask_pr=N) → explore workload, epoch 1.
    // ChatForm must apply text + workload on mount WITHOUT calling onSubmit.
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, {
      props: {
        onSubmit,
        prefill: { text: 'Explain PR #18 in plain language.', workload: 'explore', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => {
      expect(input.value).toBe('Explain PR #18 in plain language.');
    });
    expect(checkedWorkload()).toBe('explore');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe('ChatForm — crew picker integration', () => {
  it('renders the four crew cards, each describing itself for assistive tech', () => {
    const { container } = render(ChatForm, { props: { onSubmit: noop } });
    for (const v of ['drift', 'upgrade', 'explore', 'provision']) {
      const card = container.querySelector(`[data-testid="crew-card-${v}"]`);
      expect(card, `crew card for ${v}`).not.toBeNull();
      // The radio carries an aria-describedby pointing at its descriptor hint.
      const radio = card!.querySelector('input[type="radio"]') as HTMLInputElement;
      expect(radio.getAttribute('aria-describedby')).toBeTruthy();
    }
  });

  it('submits the prompt with whichever crew card is selected (binding round-trips)', async () => {
    const onSubmit = vi.fn();
    const { container, getByTestId } = render(ChatForm, { props: { onSubmit } });
    // Pick Provision via its card, type, and send.
    await fireEvent.click(
      container.querySelector('[data-testid="crew-card-provision"] input')!,
    );
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'provision please' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    expect(onSubmit).toHaveBeenCalledWith('provision please', 'provision');
  });

  it('defaults to Anchor (drift) when the picker is left untouched', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'hello' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    expect(onSubmit).toHaveBeenCalledWith('hello', 'drift');
  });
});
