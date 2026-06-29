<script module lang="ts">
  // Unique id per instance to wire the dialog's aria-labelledby to its title.
  // Module counter (not `$props.id()`) to match HelpHint/CrewPicker's
  // version-safe pattern.
  let _modalSeq = 0;
  function nextModalId(): number {
    _modalSeq += 1;
    return _modalSeq;
  }
</script>

<script lang="ts">
  // Modal — the app's reusable accessible dialog shell, built on the native
  // <dialog> element driven via showModal()/close(). Native modal mode gives us
  // a real focus trap, focus restore on close, Escape-to-close, and top-layer
  // rendering (so no z-index war with AuthPanel) for free. We add the two things
  // <dialog> does NOT do: body scroll-lock (ref-counted) and focusing a chosen
  // field on open. Backdrop clicks close (a ::backdrop click reports the dialog
  // as the event target). Consumers pass the body via the `children` snippet and
  // mark the element to focus with `data-modal-autofocus`.
  import { tick } from 'svelte';
  import Icon from './Icon.svelte';
  import { lockBodyScroll, unlockBodyScroll } from '../lib/scrollLock';
  import type { Snippet } from 'svelte';

  let {
    open,
    title,
    onClose,
    children,
  }: {
    open: boolean;
    title: string;
    onClose: () => void;
    children: Snippet;
  } = $props();

  let dialogEl = $state<HTMLDialogElement | null>(null);
  const titleId = `modal-title-${nextModalId()}`;

  // Track whether THIS modal currently holds the scroll lock, so close and
  // unmount-while-open each release exactly once (idempotent).
  let locked = false;
  function acquireLock(): void {
    if (!locked) {
      lockBodyScroll();
      locked = true;
    }
  }
  function releaseLock(): void {
    if (locked) {
      unlockBodyScroll();
      locked = false;
    }
  }

  // Drive the native dialog from the `open` prop. Keyed narrowly on `open` +
  // `dialogEl` so it runs only on the open/close transition. On open: showModal,
  // lock scroll, focus the chosen field after the body renders. On close (prop
  // flipped to false while still natively open): el.close(), which fires the
  // native `close` event handled below.
  $effect(() => {
    const el = dialogEl;
    if (!el) return;
    if (open && !el.open) {
      el.showModal();
      acquireLock();
      void tick().then(() => {
        // A fast open→close before this microtask resolves would otherwise
        // focus a now-closed dialog — bail if we're no longer open.
        if (!open || !el.open) return;
        const target =
          el.querySelector<HTMLElement>('[data-modal-autofocus]') ?? el;
        target.focus();
      });
    } else if (!open && el.open) {
      el.close();
    }
  });

  // Unmount safety: removing an open <dialog> does not fire `close`, so release
  // the lock here too (idempotent via the `locked` flag).
  $effect(() => releaseLock);

  // The single close sync point: fires for Escape (native `cancel`→`close`),
  // for el.close() from the effect, and for requestClose() below. Releases the
  // lock and tells the parent to flip `open` to false.
  function onNativeClose(): void {
    releaseLock();
    onClose();
  }

  // Funnel the close button and a backdrop click through the native close so
  // there is one code path (onNativeClose) regardless of how the modal is
  // dismissed.
  function requestClose(): void {
    dialogEl?.close();
  }

  function onBackdropClick(event: MouseEvent): void {
    if (event.target === dialogEl) requestClose();
  }

  // Escape always closes the modal — handled explicitly (rather than relying on
  // the native dialog `cancel`) because a non-empty `<input type="search">`
  // swallows the first Escape to clear itself, so the native close would need a
  // second press. preventDefault stops that clear; requestClose drives the one
  // close path. Works regardless of which element inside the modal has focus.
  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation(); // don't also trigger any app-level Escape handler
      requestClose();
    }
  }
</script>

<dialog
  bind:this={dialogEl}
  class="modal"
  aria-labelledby={titleId}
  onclose={onNativeClose}
  onclick={onBackdropClick}
  onkeydown={onKeydown}
>
  {#if open}
    <div class="modal__panel">
      <header class="modal__head">
        <h2 id={titleId} class="ds-h2 modal__title">{title}</h2>
        <button
          type="button"
          class="modal__close"
          aria-label="Close"
          onclick={requestClose}
        >
          <Icon name="x" size={18} />
        </button>
      </header>
      <div class="modal__body">
        {@render children()}
      </div>
    </div>
  {/if}
</dialog>

<style>
  /* The <dialog> is a transparent, viewport-centered flex container; the visible
     card is `.modal__panel`. padding:0 + a separate padded panel means a click
     on the panel never reports the dialog as target — only a ::backdrop click
     does, which is how backdrop-to-close is detected. */
  .modal {
    padding: 0;
    border: none;
    background: transparent;
    width: 100%;
    max-width: min(92vw, 40rem);
    max-height: 85vh;
    overflow: visible;
    color: var(--ds-fg);
  }

  .modal[open] {
    display: flex;
    flex-direction: column;
  }

  .modal::backdrop {
    background: rgba(26, 26, 24, 0.42);
    backdrop-filter: blur(2px);
  }

  .modal__panel {
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--ds-surface);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    box-shadow: var(--ds-shadow-lg);
    overflow: hidden;
    animation: modal-rise var(--ds-dur) var(--ds-ease-out);
  }

  .modal::backdrop {
    animation: modal-fade var(--ds-dur) var(--ds-ease-out);
  }

  .modal__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-4) var(--ds-sp-5);
    border-bottom: 1px solid var(--ds-border);
    flex-shrink: 0;
  }

  .modal__title {
    margin: 0;
    font-size: var(--ds-fs-3);
  }

  .modal__close {
    appearance: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--ds-sp-1);
    border: none;
    border-radius: var(--ds-radius-sm);
    background: none;
    color: var(--ds-muted);
    cursor: pointer;
    transition:
      color var(--ds-dur-fast) var(--ds-ease),
      background-color var(--ds-dur-fast) var(--ds-ease);
  }

  .modal__close:hover {
    color: var(--ds-fg);
    background: var(--ds-surface-2);
  }

  .modal__close:focus-visible {
    outline: none;
    box-shadow: var(--ds-ring);
  }

  .modal__body {
    overflow-y: auto;
    min-height: 0;
    padding: var(--ds-sp-4) var(--ds-sp-5) var(--ds-sp-5);
  }

  @keyframes modal-fade {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }

  @keyframes modal-rise {
    from {
      opacity: 0;
      transform: translateY(8px) scale(0.985);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .modal__panel,
    .modal::backdrop {
      animation: none;
    }
  }
</style>
