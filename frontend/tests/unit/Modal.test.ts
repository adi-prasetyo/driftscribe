import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import Modal from '../../src/components/Modal.svelte';

afterEach(cleanup);

// A minimal body snippet with a focusable, autofocus-marked field.
const body = createRawSnippet(() => ({
  render: () =>
    `<div><input data-modal-autofocus data-testid="modal-field" /></div>`,
}));

function renderModal(open: boolean, onClose = () => {}) {
  return render(Modal, { props: { open, title: 'Search chats', onClose, children: body } });
}

describe('Modal', () => {
  it('renders no title/body and does not lock scroll while closed', () => {
    const { queryByText } = renderModal(false);
    expect(queryByText('Search chats')).toBeNull();
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('opens: renders the title + body, locks body scroll, and focuses the marked field', async () => {
    const { getByText, getByTestId } = renderModal(true);
    expect(getByText('Search chats')).toBeTruthy();
    expect(document.body.style.overflow).toBe('hidden');
    await waitFor(() => expect(document.activeElement).toBe(getByTestId('modal-field')));
  });

  it('the close button dismisses (calls onClose) and releases the scroll lock', async () => {
    const onClose = vi.fn();
    const { getByLabelText } = renderModal(true, onClose);
    await fireEvent.click(getByLabelText('Close'));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('Escape dismisses (one press, even from a search field) and releases the lock', async () => {
    const onClose = vi.fn();
    const { container } = renderModal(true, onClose);
    const dialog = container.querySelector('dialog')!;
    await fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('a backdrop click (on the dialog itself) dismisses', async () => {
    const onClose = vi.fn();
    const { container } = renderModal(true, onClose);
    const dialog = container.querySelector('dialog')!;
    await fireEvent.click(dialog); // event.target === dialog → backdrop
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('a click inside the panel does NOT dismiss', async () => {
    const onClose = vi.fn();
    const { getByText } = renderModal(true, onClose);
    await fireEvent.click(getByText('Search chats')); // target is the title, not the dialog
    expect(onClose).not.toHaveBeenCalled();
  });

  it('labels the dialog by its title for assistive tech', () => {
    const { container } = renderModal(true);
    const dialog = container.querySelector('dialog')!;
    const labelledby = dialog.getAttribute('aria-labelledby');
    expect(labelledby).toBeTruthy();
    expect(document.getElementById(labelledby!)?.textContent).toBe('Search chats');
  });
});
