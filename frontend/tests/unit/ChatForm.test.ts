import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/svelte';
import ChatForm from '../../src/components/ChatForm.svelte';

// Prefill is the Phase-4 adopt-button bridge: the Adopt affordance on the resource
// map prefills the chat input (text + workload) WITHOUT sending it — the operator
// stays in charge. An epoch counter lets the same/another Adopt click re-apply
// after the operator edits the draft. Design §2.6; Codex review 019eb572.

afterEach(cleanup);

const noop = () => {};

describe('ChatForm — prefill', () => {
  it('applies the prefilled text + workload and focuses the input', async () => {
    const { getByTestId } = render(ChatForm, {
      props: {
        onSubmit: noop,
        prefill: { text: 'Adopt the Storage bucket `x` into IaC management.', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    const select = document.getElementById('workload-select') as HTMLSelectElement;
    await waitFor(() => {
      expect(input.value).toBe('Adopt the Storage bucket `x` into IaC management.');
    });
    expect(select.value).toBe('provision');
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
    const select = document.getElementById('workload-select') as HTMLSelectElement;
    expect(select.value).toBe('drift');
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
    const select = document.getElementById('workload-select') as HTMLSelectElement;
    await waitFor(() => {
      expect(input.value).toBe('Explain PR #18 in plain language.');
    });
    expect(select.value).toBe('explore');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
