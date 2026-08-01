<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { type Workload, type ChatPrefill } from '../lib/workloads';
  import { t } from '../lib/i18n';
  import Icon from './Icon.svelte';

  // The prompt composer: a growing prompt input + Send button. In historical
  // mode the whole form is dimmed (.historical) and every control is disabled —
  // the operator is reviewing a past trace, not starting a new one.
  //
  // There is no crew picker. Choosing a crew up front asked the operator to
  // already know which of four specialists owns their question, which is the
  // system's own taxonomy leaking into the front door. A new thread goes to
  // Explore, and any crew that finds the question belongs to a sibling offers
  // the handoff itself for the operator to confirm. `workload` survives as a
  // prop because the thread is still crew-LOCKED — the composer has to send
  // whichever crew currently holds the conversation.
  let {
    disabled = false,
    onSubmit,
    prefill = null,
    workload = $bindable('explore'),
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
    /**
     * The crew this composer will send to, a two-way binding (P2): App reads it
     * for the crew-lock check on a multi-turn thread, and SETS it when the
     * operator resumes a conversation from the rail (or a handoff moves the
     * thread to a new crew) so the composer follows the thread. With the picker
     * gone the operator never writes it — only the prefill effect below (Adopt
     * carries an explicit Provision intent) and App do. A fresh thread defaults
     * to Explore, the crew whose job is to figure out where a question belongs.
     */
    workload?: Workload;
  } = $props();

  let prompt = $state('');
  let inputEl = $state<HTMLTextAreaElement | null>(null);

  // Apply the prefill on each NEW epoch (tracked dependency); set the workload
  // and focus the input so the operator can edit / press Send. Keyed on
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
      // Only a prefill that NAMES a crew moves the composer. A text-only
      // prefill (a suggestion chip) leaves `workload` exactly as it was, which
      // on a resumed thread is the crew that thread is locked to — writing
      // 'explore' here instead of nothing would send the next turn to the wrong
      // crew and earn a 409.
      if (p.workload !== undefined) workload = p.workload;
      inputEl?.focus();
    });
    // Re-fit the textarea AFTER the bind:value DOM write commits. The prompt-
    // tracking auto-grow effect below also re-runs on this write, but because the
    // write originates inside THIS effect it measures scrollHeight against the
    // pre-commit (stale, empty) layout, leaving a multi-line prefill one line
    // tall until the operator edits it. tick() waits for the DOM to flush so the
    // resize measures the real content height. (Typing needs no tick — the
    // browser commits the DOM value before the input event, so that path is fine.)
    tick().then(autoResize);
  });

  function submit() {
    const trimmed = prompt.trim();
    if (!trimmed) return;
    onSubmit(trimmed, workload);
    prompt = '';
  }

  function handle(e: SubmitEvent) {
    e.preventDefault();
    submit();
  }

  // Chat-composer key handling: Enter sends, Shift+Enter inserts a newline (the
  // textarea's native behaviour, so we just let it through). The IME guards stop
  // a submit while CJK input is mid-composition — pressing Enter to confirm a
  // candidate must not fire the prompt mid-word. `isComposing` is the modern
  // signal; `keyCode === 229` is the legacy belt-and-suspenders for browser/IME
  // combos that report the confirm Enter after composition already ended.
  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      submit();
    }
  }

  // Fit the textarea's height to its content so line breaks (Shift+Enter) and
  // multi-line prefills are actually visible; CSS caps it with a max-height +
  // scroll. Called reactively on every `prompt` change (below) and again after a
  // prefill's DOM commit (above).
  function autoResize(): void {
    const el = inputEl;
    if (!el) return;
    el.style.height = 'auto';
    // box-sizing is border-box, but scrollHeight excludes the borders — so
    // setting height = scrollHeight leaves the content box ~2px short and
    // overflow-y:auto shows a scrollbar even on an empty, single-line field.
    // Add the vertical borders back so the box fits its content exactly and the
    // scrollbar only appears once the content really exceeds the max-height cap.
    const cs = getComputedStyle(el);
    const borderY =
      parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    el.style.height = `${el.scrollHeight + borderY}px`;
  }
  // Re-fit on every prompt change so typing and post-send clearing stay sized.
  $effect(() => {
    prompt;
    autoResize();
  });
</script>

<form id="chat-form" class="chat-form" class:historical={disabled} onsubmit={handle}>
  <!-- Prompt + Send, and nothing else. The crew picker went first (a fresh
       thread routes itself), the New chat button second (ds-jns PR 3 — it moved
       to the conversations rail, where the threads it starts and reopens live).
       What is left is the box you type in. -->
  <textarea
    id="prompt-input"
    data-testid="chat-prompt"
    class="chat-form__input"
    rows="1"
    autocomplete="off"
    placeholder={$t('composer.chatForm.placeholder')}
    aria-label={$t('composer.chatForm.promptAriaLabel')}
    aria-describedby="prompt-input-hint"
    bind:this={inputEl}
    bind:value={prompt}
    onkeydown={handleKeydown}
    {disabled}
  ></textarea>
  <!-- The placeholder carries the Enter/Shift+Enter hint for sighted operators,
       but it vanishes once typing starts and is unreliable for screen readers —
       so the same hint lives here, visually hidden, wired via aria-describedby. -->
  <p id="prompt-input-hint" class="chat-form__sr-only">
    {$t('composer.chatForm.enterShiftHint')}
  </p>

  <button
    id="send-btn"
    data-testid="chat-submit"
    class="ds-btn chat-form__send"
    type="submit"
    {disabled}
  >
    <Icon name="send" size={14} />{$t('composer.chatForm.send')}
  </button>
</form>

<style>
  .chat-form {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-2);
    /* White fill like the other cards in this column; a thin blue border is the
       only accent, marking this as the interactive composer without the heavier
       tinted fill + 3px left accent bar it used to wear. */
    background: var(--ds-surface);
    /* Promoted one level at ds-7ag.5: the composer is the one thing on this
       page the operator ACTS with, and at shadow-sm it sat at the same weight as
       every card around it. The surrounding metadata drawers went quiet in the
       same change, so this is the page's visual primary by contrast rather than
       by shouting. The blue 送信 button stays its only saturated element. */
    border: 1px solid var(--ds-stream-border);
    border-radius: var(--ds-radius);
    box-shadow: var(--ds-shadow);
    transition: opacity var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease);
  }

  /* No whole-card focus treatment by design — focus is handled (deliberately
     quietly) at the input rule below. See .chat-form__input:focus-visible. */

  /* Historical replay: the composer is inert and visually receded. */
  .chat-form.historical {
    opacity: 0.55;
    box-shadow: none;
    background: var(--ds-surface-2);
    /* Inert replay: drop the blue border so the composer reads as receded,
       not "ready for input". */
    border-color: var(--ds-border);
  }

  /* The crew picker + New chat button share the full-width row above the input:
     picker grows, button hugs the trailing edge. */
  /* Visually-hidden helper for the aria-describedby keyboard hint (matches the
     CrewPicker / ReplyPending sr-only pattern). */
  .chat-form__sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    border: 0;
    white-space: nowrap;
    clip: rect(0 0 0 0);
    clip-path: inset(50%);
  }

  /* The prompt input is the protagonist: it grows to fill the row. As a
     textarea it auto-grows in height with its content (JS sets the height from
     scrollHeight); we cap it here and scroll past the cap. */
  .chat-form__input {
    flex: 1 1 16rem;
    min-width: 0;
    padding: 0.62em 0.85em;
    /* A bordered well — the same thin blue border as the card — so the input
       reads as "click to type", set off from the card by its border + the
       surrounding padding rather than a fill of its own. */
    border: 1px solid var(--ds-stream-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg);
    font-family: inherit;
    font-size: var(--ds-fs-2);
    line-height: 1.4;
    /* A single comfortable row by default, growing up to ~8 lines before it
       starts scrolling. resize:none — the auto-grow owns the height. */
    resize: none;
    max-height: 12rem;
    overflow-y: auto;
    transition: border-color var(--ds-dur) var(--ds-ease);
  }
  .chat-form__input::placeholder {
    color: var(--ds-faint);
  }
  /* Active field: no extra highlight — the field looks the same focused as at
     rest, so nothing flares up when you click in. The blinking caret is the only
     focus cue. We null box-shadow too because the global focus rule
     (base.css `:where(...):focus-visible`) otherwise paints a blue ring on the
     textarea; outline:none alone left that ring in place. */
  .chat-form__input:focus-visible {
    outline: none;
    box-shadow: none;
  }

  /* Filled controls take navy, not --ds-stream: white on #4285f4 is 3.56:1 and
     fails AA, while white on navy is 15.7:1. This also makes Send and the
     desk's "approve" CTA the same control in two places, which is the point of
     the unification. Hover LIFTS instead of deepening: the old Send deepened
     #4285f4 -> #1858c0, which was visible because the base was light, but navy
     is already near-black so darkening it further reads as nothing. The mix is
     derived from the token rather than a new literal — white text stays 11.2:1. */
  .chat-form__send {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    background: var(--ds-navy);
    border-color: var(--ds-navy);
    color: #fff;
  }
  .chat-form__send:hover {
    background: color-mix(in srgb, var(--ds-navy) 88%, #fff);
    border-color: color-mix(in srgb, var(--ds-navy) 88%, #fff);
  }

  /* When the row is dimmed for historical mode the disabled input doesn't
     need its own greyed-out treatment fighting the parent opacity. */
  .chat-form__input:disabled {
    cursor: not-allowed;
    color: var(--ds-muted);
    /* Shed the field chrome when inert so it recedes into the dimmed card. */
    background: transparent;
    border-color: transparent;
  }

  /* Narrow widths: the input takes the full row above Send. */
  @media (max-width: 30rem) {
    .chat-form__input {
      flex: 1 1 100%;
    }
  }
</style>
