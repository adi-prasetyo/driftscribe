<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { type Workload, type ChatPrefill } from '../lib/workloads';
  import { t } from '../lib/i18n';
  import CrewMenu from './CrewMenu.svelte';
  import Icon from './Icon.svelte';

  // The prompt composer: the crew this will go to, a growing prompt input, and
  // Send. In historical mode the whole form is dimmed (.historical) and every
  // control is disabled — the operator is reviewing a past trace, not starting a
  // new one.
  //
  // The crew is NAMED here again (ds-uyo). It stopped being named in #255 along
  // with the picker that set it, and the two are not the same thing: an Adopt
  // click arms Provision, and with neither a picker nor a label the operator
  // could send a question to a crew they were never told about. `CrewMenu` puts
  // the name back and carries the control with it — but nothing forces a choice
  // before typing, Explore is still the default a fresh thread gets for free,
  // and the crew LOCK is untouched: a different crew opens a new thread rather
  // than moving this one.
  let {
    disabled = false,
    onSubmit,
    onSelectCrew,
    prefill = null,
    occupied = false,
    workload = $bindable('explore'),
  }: {
    disabled?: boolean;
    onSubmit: (prompt: string, workload: Workload) => void;
    /**
     * The operator picked a crew from the menu. OPTIONAL, and the fallback is
     * deliberately the dumb one: with no handler this just moves the composer's
     * own crew, which is all a standalone ChatForm can honestly do. Starting a
     * new thread is App's business — `newChat()` cancels an in-flight stream and
     * drops the open conversation, and a child component that could do that on
     * its own is a child component that will.
     */
    onSelectCrew?: (wl: Workload) => void;
    /** There is something on the chat screen a clean slate would clear, so
     *  another crew costs it. Passed straight through to the menu's pre-click
     *  hint — see its own doc for why this is broader than "a thread is open". */
    occupied?: boolean;
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

  // A crew was chosen. The draft is deliberately NOT cleared: redirecting a
  // half-typed question to the crew that can actually answer it is the whole
  // point of the control, so the text survives and the caret goes back to it.
  // Focus lands in the textarea rather than back on the trigger because the
  // operator's next act is to keep typing or send, not to reopen the menu.
  function selectCrew(wl: Workload): void {
    if (onSelectCrew) onSelectCrew(wl);
    else workload = wl;
    inputEl?.focus();
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
  <!-- Crew, prompt, Send. The New chat button is still gone (ds-jns PR 3 — it
       moved to the conversations rail, where the threads it starts and reopens
       live). The crew is back, and back as one element rather than the two it
       used to be: a card grid that made you choose before typing, plus nothing
       at all reporting what you had chosen. -->
  <CrewMenu value={workload} {disabled} {occupied} onSelect={selectCrew} />
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
    /* The row's resting control height, published here for the controls beside
       the field to match. Deliberately outside the design-token namespace,
       because this is not a design token — it is one row's local geometry, and
       tokens.css is the only file allowed to name one. (Naming it the other way
       would fail contrast.test.ts, which scans source text and is blunt on
       purpose.)
       It is the PROMPT FIELD's box, because the field is the row's protagonist
       and the others align to it: one line of --ds-fs-2 at line-height 1.4, plus
       .chat-form__input's own 0.62em padding top and bottom, plus its 1px
       borders. Every term below is a declaration in this file, so the arithmetic
       cannot quietly disagree with its own source.
       Custom properties inherit, so this reaches CrewMenu's trigger through the
       DOM even though Svelte scopes the two components' selectors apart. That
       direction matters: the container states the height and the controls read
       it, rather than each control keeping its own copy of the field's metrics
       and going stale on its own schedule. */
    --composer-control-h: calc(var(--ds-fs-2) * (1.4 + 0.62 * 2) + 2px);
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

  /* Visually-hidden helper for the aria-describedby keyboard hint (matches the
     ReplyPending sr-only pattern). */
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

  /* Filled controls take navy rather than --ds-stream. White on #4285f4 is
     3.56:1 and fails AA, while white on navy is 15.7:1. This also makes Send and the
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
    /* Hold the resting height instead of stretching with the row. The row is
       align-items: stretch, which is right for the field and wrong for a button:
       a prompt grown to five lines turned Send into a 126px navy slab, the
       heaviest thing on the page, sized by how much the operator happened to
       type. flex-start rather than flex-end so the three tops stay flush at
       every prompt length — the pill, the field's first line and Send read as
       one row that the field grows downward out of.
       min-height, not height: .ds-btn's line-height is 1.2 against the field's
       1.4, so its natural box is a few px short of the row and this floors it;
       a taller label in another locale still gets its room. */
    align-self: flex-start;
    min-height: var(--composer-control-h);
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
