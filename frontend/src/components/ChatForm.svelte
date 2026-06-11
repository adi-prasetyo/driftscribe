<script lang="ts">
  import { untrack } from 'svelte';
  import { WORKLOADS, type Workload, type ChatPrefill } from '../lib/workloads';

  // The prompt composer. A single-row form: a growing prompt input, a compact
  // workload select, and a Send button. In historical mode the whole row is
  // dimmed (.historical) and every control is disabled — the operator is
  // reviewing a past trace, not starting a new one.
  let {
    disabled = false,
    onSubmit,
    prefill = null,
  }: {
    disabled?: boolean;
    onSubmit: (prompt: string, workload: Workload) => void;
    /**
     * Adopt-button bridge (Phase 4): prefill the composer WITHOUT sending — the
     * operator stays in charge (design §6). `epoch` lets the same/another Adopt
     * click re-apply after the operator edits; a no-op rerender at the same epoch
     * never clobbers an edited draft (Codex review 019eb572).
     */
    prefill?: ChatPrefill | null;
  } = $props();

  let prompt = $state('');
  let workload = $state<Workload>('drift');
  let inputEl = $state<HTMLInputElement | null>(null);

  // Apply the prefill on each NEW epoch (tracked dependency); set the workload
  // select and focus the input so the operator can edit / press Send. Keyed on
  // epoch (not text) so identical re-prefills still re-apply after an edit, and a
  // same-epoch rerender leaves an edited draft alone. untrack the writes so this
  // effect depends ONLY on prefill?.epoch.
  let lastPrefillEpoch = -1;
  $effect(() => {
    const p = prefill;
    if (!p || p.epoch === lastPrefillEpoch) return;
    lastPrefillEpoch = p.epoch;
    untrack(() => {
      prompt = p.text;
      workload = p.workload;
      inputEl?.focus();
    });
  });

  function handle(e: SubmitEvent) {
    e.preventDefault();
    const trimmed = prompt.trim();
    if (!trimmed) return;
    onSubmit(trimmed, workload);
    prompt = '';
  }
</script>

<form id="chat-form" class="chat-form" class:historical={disabled} onsubmit={handle}>
  <input
    id="prompt-input"
    data-testid="chat-prompt"
    class="chat-form__input"
    type="text"
    autocomplete="off"
    placeholder="Ask the coordinator…"
    aria-label="Prompt"
    bind:this={inputEl}
    bind:value={prompt}
    {disabled}
  />

  <select
    id="workload-select"
    class="chat-form__select"
    aria-label="Workload"
    bind:value={workload}
    {disabled}
  >
    {#each WORKLOADS as option (option.value)}
      <option value={option.value}>{option.label}</option>
    {/each}
  </select>

  <button
    id="send-btn"
    data-testid="chat-submit"
    class="ds-btn chat-form__send"
    type="submit"
    {disabled}
  >
    Send
  </button>
</form>

<style>
  .chat-form {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-2);
    background: var(--ds-surface);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    box-shadow: var(--ds-shadow-sm);
    transition: opacity var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease);
  }

  /* Lift the whole composer when any control inside is focused — a calm,
     editorial focus affordance rather than per-control rings stacking up. */
  .chat-form:focus-within {
    border-color: var(--ds-stream-border);
    box-shadow: var(--ds-shadow-sm), var(--ds-ring);
  }

  /* Historical replay: the composer is inert and visually receded. */
  .chat-form.historical {
    opacity: 0.55;
    box-shadow: none;
    background: var(--ds-surface-2);
  }
  .chat-form.historical:focus-within {
    border-color: var(--ds-border);
    box-shadow: none;
  }

  /* The prompt input is the protagonist: it grows to fill the row. */
  .chat-form__input {
    flex: 1 1 16rem;
    min-width: 0;
    padding: 0.62em 0.85em;
    border: 1px solid transparent;
    border-radius: var(--ds-radius-sm);
    background: transparent;
    color: var(--ds-fg);
    font-size: var(--ds-fs-2);
    line-height: 1.4;
  }
  .chat-form__input::placeholder {
    color: var(--ds-faint);
  }
  .chat-form__input:focus-visible {
    outline: none;
    box-shadow: none;
  }

  /* Compact supporting controls. */
  .chat-form__select {
    flex: 0 0 auto;
    padding: 0.55em 2em 0.55em 0.7em;
    border: 1px solid var(--ds-border-strong);
    border-radius: var(--ds-radius-sm);
    background-color: var(--ds-surface);
    color: var(--ds-fg-soft);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    cursor: pointer;
    /* hand-rolled caret so the control reads as part of the editorial set */
    appearance: none;
    -webkit-appearance: none;
    background-image: linear-gradient(45deg, transparent 50%, var(--ds-muted) 50%),
      linear-gradient(135deg, var(--ds-muted) 50%, transparent 50%);
    background-position: calc(100% - 1.05em) center, calc(100% - 0.75em) center;
    background-size: 0.3em 0.3em, 0.3em 0.3em;
    background-repeat: no-repeat;
  }
  .chat-form__select:hover {
    border-color: var(--ds-muted);
  }

  .chat-form__send {
    flex: 0 0 auto;
    background: var(--ds-stream);
    border-color: var(--ds-stream-ink);
    color: #fff;
  }
  .chat-form__send:hover {
    background: var(--ds-stream-ink);
  }

  /* When the row is dimmed for historical mode the disabled controls don't
     need their own greyed-out treatment fighting the parent opacity. */
  .chat-form__input:disabled,
  .chat-form__select:disabled {
    cursor: not-allowed;
    color: var(--ds-muted);
  }

  /* Narrow widths: stack the controls; the input keeps the top row, the
     select + Send share the next line. */
  @media (max-width: 30rem) {
    .chat-form__input {
      flex: 1 1 100%;
    }
    .chat-form__select {
      flex: 1 1 auto;
    }
  }
</style>
