import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import ChatForm from '../../src/components/ChatForm.svelte';

// Prefill is the Phase-4 adopt-button bridge: the Adopt affordance on the resource
// map prefills the chat input (text + workload) WITHOUT sending it — the operator
// stays in charge. An epoch counter lets the same/another Adopt click re-apply
// after the operator edits the draft. Design §2.6; Codex review 019eb572.

afterEach(cleanup);

const noop = () => {};

/**
 * The crew the composer would send to, observed the only way it is observable
 * now that the picker is gone: submit and read what onSubmit was handed. The
 * old helper read the checked radio, which no longer exists — and that is the
 * point, so asserting through the actual submit path is also the more honest
 * test.
 */
async function submittedWorkload(
  onSubmit: ReturnType<typeof vi.fn>,
  input: HTMLTextAreaElement | HTMLInputElement,
): Promise<string> {
  await fireEvent.input(input, { target: { value: 'x' } });
  await fireEvent.submit(document.getElementById('chat-form')!);
  return onSubmit.mock.calls.at(-1)?.[1] as string;
}

describe('ChatForm — prefill', () => {
  it('applies the prefilled text + workload and focuses the input', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, {
      props: {
        onSubmit,
        prefill: { text: 'Adopt the Storage bucket `x` into IaC management.', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => {
      expect(input.value).toBe('Adopt the Storage bucket `x` into IaC management.');
    });
    // The $effect focuses the input so the operator can edit / press Send.
    expect(document.activeElement).toBe(input);
    // Adopt is the one path that still names a crew: the prefill's workload has
    // to survive to the submit, or an Adopt click would open an Explore thread.
    expect(await submittedWorkload(onSubmit, input)).toBe('provision');
  });

  it('re-applies when the epoch bumps with new text (overwrites the draft)', async () => {
    const onSubmit = vi.fn();
    const { getByTestId, rerender } = render(ChatForm, {
      props: {
        onSubmit,
        prefill: { text: 'first', workload: 'provision', epoch: 1 },
      },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe('first'));

    await rerender({
      onSubmit,
      prefill: { text: 'second', workload: 'drift', epoch: 2 },
    });
    await waitFor(() => expect(input.value).toBe('second'));
    expect(await submittedWorkload(onSubmit, input)).toBe('drift');
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
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe('ChatForm — keyboard submit (Enter sends, Shift+Enter is a newline)', () => {
  it('submits on Enter (no Shift) and clears the field', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'send me' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledWith('send me', 'explore');
    expect(input.value).toBe('');
  });

  it('does NOT submit on Shift+Enter (lets the line break through)', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'line one' } });
    await fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('does NOT submit on Enter while an IME composition is active', async () => {
    // CJK input confirms a candidate with Enter; submitting mid-composition would
    // fire the prompt before the word is even committed.
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'こんにちは' } });
    await fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('does NOT submit on Enter when the field is empty/whitespace', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: '   ' } });
    await fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('does NOT submit on Enter reported with the legacy IME keyCode 229', async () => {
    // Some browser/IME combos still report the composition-confirm Enter as
    // keyCode 229 with isComposing already false; belt-and-suspenders guard.
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    await fireEvent.input(input, { target: { value: 'まだ変換中' } });
    await fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('exposes the Enter / Shift+Enter behaviour to assistive tech via aria-describedby', () => {
    // The textarea's accessible name is just "Prompt"; the non-native Enter
    // behaviour must be announced too, not hidden in the (often unspoken)
    // placeholder.
    const { getByTestId } = render(ChatForm, { props: { onSubmit: noop } });
    const input = getByTestId('chat-prompt') as HTMLTextAreaElement;
    const describedby = input.getAttribute('aria-describedby');
    expect(describedby).toBeTruthy();
    const hint = document.getElementById(describedby!);
    expect(hint).not.toBeNull();
    const text = hint!.textContent?.toLowerCase() ?? '';
    expect(text).toContain('enter');
    expect(text).toContain('shift');
  });
});

describe('ChatForm — no crew picker (single-door handoff)', () => {
  it('offers the operator no crew choice at all', () => {
    const { container } = render(ChatForm, { props: { onSubmit: noop } });
    // No cards, and — the load-bearing half — no control of ANY kind that
    // selects a workload. Asserting only on the old testids would still pass if
    // the picker came back wearing a <select>.
    for (const v of ['drift', 'upgrade', 'explore', 'provision']) {
      expect(container.querySelector(`[data-testid="crew-card-${v}"]`)).toBeNull();
    }
    expect(container.querySelector('input[type="radio"]')).toBeNull();
    expect(container.querySelector('select')).toBeNull();
  });

  it('sends a fresh thread to Explore, the crew that routes an unrouted question', async () => {
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, { props: { onSubmit } });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'hello' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    expect(onSubmit).toHaveBeenCalledWith('hello', 'explore');
  });

  it('still sends to the crew the OPEN THREAD is locked to', async () => {
    // The lock did not go away with the picker — it moved out of the operator's
    // hands. App sets `workload` from the resumed thread (or from a completed
    // handoff), and the composer must carry that crew, not its own default.
    const onSubmit = vi.fn();
    const { getByTestId } = render(ChatForm, {
      props: { onSubmit, workload: 'provision' },
    });
    const input = getByTestId('chat-prompt') as HTMLInputElement;
    await fireEvent.input(input, { target: { value: 'and the bucket?' } });
    await fireEvent.submit(document.getElementById('chat-form')!);
    expect(onSubmit).toHaveBeenCalledWith('and the bucket?', 'provision');
  });
});

describe('ChatForm — composer New chat button', () => {
  it('is hidden by default (fresh composer)', () => {
    const { queryByTestId } = render(ChatForm, { props: { onSubmit: () => {} } });
    expect(queryByTestId('composer-new-chat')).toBeNull();
  });

  it('renders when showNewChat and fires onNewChat on click', async () => {
    const onNewChat = vi.fn();
    const { getByTestId } = render(ChatForm, {
      props: { onSubmit: () => {}, showNewChat: true, onNewChat },
    });
    await fireEvent.click(getByTestId('composer-new-chat'));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it('stays clickable while the form is disabled (mid-stream cancel path)', async () => {
    const onNewChat = vi.fn();
    const { getByTestId } = render(ChatForm, {
      props: { onSubmit: () => {}, disabled: true, showNewChat: true, onNewChat },
    });
    const btn = getByTestId('composer-new-chat') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    await fireEvent.click(btn);
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

});
