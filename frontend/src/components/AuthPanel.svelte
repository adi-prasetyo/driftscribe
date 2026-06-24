<script lang="ts">
  // Inline operator-token entry that REPLACES window.prompt(). Rendered as a
  // centered, accessible modal (role="dialog" aria-modal) over a dim backdrop.
  // App owns sessionStorage['driftscribe_token']; this panel only collects the
  // value and hands it back via onSubmit. When `open` is false, render nothing.
  let {
    open,
    onSubmit,
    onCancel,
  }: {
    open: boolean;
    onSubmit: (token: string) => void;
    onCancel: () => void;
  } = $props();

  let value = $state('');
  let inputEl = $state<HTMLInputElement | null>(null);

  // Autofocus + reset whenever the panel transitions to open. The $effect reads
  // `open` and `inputEl`, so it re-runs once the bound input is mounted.
  $effect(() => {
    if (open && inputEl) {
      value = '';
      inputEl.focus();
    }
  });

  function save(): void {
    const trimmed = value.trim();
    if (trimmed === '') return;
    onSubmit(trimmed);
  }

  // Submit on Enter from the input; Escape cancels (familiar dialog ergonomics).
  function onInputKeydown(e: KeyboardEvent): void {
    if (e.key === 'Enter') {
      e.preventDefault();
      save();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onCancel();
    }
  }
</script>

{#if open}
  <!-- Backdrop: clicking outside the card cancels. It is a presentational
       layer (role="presentation"); the real dismissal affordances are the
       Cancel button and the Escape key handled on the input. -->
  <div
    class="authpanel__backdrop"
    role="presentation"
    onclick={(e) => {
      if (e.target === e.currentTarget) onCancel();
    }}
  >
    <div
      class="ds-card authpanel"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-labelledby="authpanel-title"
      aria-describedby="authpanel-desc"
    >
      <h2 id="authpanel-title" class="ds-h2 authpanel__title">Operator token</h2>
      <p id="authpanel-desc" class="ds-subtle authpanel__desc">
        Stored in <code class="ds-code">sessionStorage</code> for this tab only.
        Cleared when you close the tab, never sent anywhere but the coordinator.
      </p>

      <input
        bind:this={inputEl}
        bind:value
        type="text"
        class="authpanel__input"
        aria-label="Operator token"
        placeholder="Paste your operator token…"
        autocomplete="off"
        autocapitalize="off"
        autocorrect="off"
        spellcheck="false"
        onkeydown={onInputKeydown}
      />

      <div class="authpanel__actions">
        <button type="button" class="ds-btn ds-btn--ghost" onclick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          class="ds-btn ds-btn--approve"
          disabled={value.trim() === ''}
          onclick={save}
        >
          Save
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .authpanel__backdrop {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--ds-sp-5);
    background: rgba(26, 26, 24, 0.42);
    backdrop-filter: blur(2px);
    animation: authpanel-fade var(--ds-dur) var(--ds-ease-out);
  }

  /* Override .ds-card defaults for the modal surface: tighter top margin,
     lifted elevation, and a comfortable fixed reading width. */
  .authpanel {
    width: 100%;
    max-width: 30rem;
    margin: 0;
    padding: var(--ds-sp-6);
    box-shadow: var(--ds-shadow-lg);
    animation: authpanel-rise var(--ds-dur) var(--ds-ease-out);
  }

  .authpanel__title {
    margin: 0 0 var(--ds-sp-2);
  }

  .authpanel__desc {
    margin: 0 0 var(--ds-sp-5);
    max-width: none;
    line-height: var(--ds-lh-body);
  }

  .authpanel__input {
    display: block;
    width: 100%;
    padding: 0.7em 0.85em;
    font-family: var(--ds-font-mono);
    font-size: var(--ds-fs-2);
    color: var(--ds-fg);
    background: var(--ds-surface-2);
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    transition: border-color var(--ds-dur-fast) var(--ds-ease),
      box-shadow var(--ds-dur-fast) var(--ds-ease),
      background-color var(--ds-dur-fast) var(--ds-ease);
  }

  .authpanel__input::placeholder {
    color: var(--ds-faint);
    font-family: var(--ds-font);
  }

  .authpanel__input:focus-visible {
    outline: none;
    background: var(--ds-surface);
    border-color: var(--ds-stream);
    box-shadow: var(--ds-ring);
  }

  .authpanel__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--ds-sp-3);
    margin-top: var(--ds-sp-5);
  }

  @keyframes authpanel-fade {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }

  @keyframes authpanel-rise {
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
    .authpanel__backdrop,
    .authpanel {
      animation: none;
    }
  }
</style>
