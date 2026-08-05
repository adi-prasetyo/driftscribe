<script module lang="ts">
  // Ids have to be unique per instance: two composers on one page would
  // otherwise cross-wire `aria-controls` and `aria-describedby` onto the first
  // one's nodes. A module counter rather than `$props.id()`, which needs Svelte
  // >= 5.20 and the repo pins ^5.19 — the same reason the retired CrewPicker
  // carried one.
  let _menuSeq = 0;
  function nextMenuUid(): string {
    _menuSeq += 1;
    return `crew-menu-${_menuSeq}`;
  }
</script>

<script lang="ts">
  /**
   * CrewMenu — the crew's name at the leading edge of the composer, and the
   * control that changes it. ds-uyo; design
   * docs/plans/2026-08-05-composer-crew-menu-design.md.
   *
   * ONE element carries display and control, because a separate chip and a
   * separate picker can drift apart and this cannot. The state lives on the
   * button, not in the placeholder: a placeholder disappears the moment typing
   * starts, which is exactly when the operator is about to send.
   *
   * This partially reverses PR #255, which retired the crew picker, and the
   * design says so rather than dressing it up. What justifies reopening it is
   * that #255 removed the crew DISPLAY along with the crew CONTROL: an Adopt
   * click arms Provision, and until the reply landed nothing on screen said so.
   * The three things #255 bought are kept and constrain this component —
   * nothing forces a crew choice before typing (Explore is still the
   * zero-decision default and this starts closed), the server's 409 crew lock is
   * untouched, and a handoff keeps its own job. A menu choice starts a CLEAN
   * thread; it never moves an existing one, so it cannot bypass the lock.
   *
   * `onSelect` is fired for every activation, including the crew already
   * selected. The no-op guard lives in the caller (App's `selectCrew`) because
   * the caller is the only thing that can start a new chat, and an invariant
   * split across two files is one nobody owns.
   */
  import { tick } from 'svelte';
  import {
    WORKLOADS,
    crewDescriptor,
    crewLifecycle,
    crewName,
    type Workload,
  } from '../lib/workloads';
  import { t } from '../lib/i18n';
  import CrewGlyph from './CrewGlyph.svelte';
  import Icon from './Icon.svelte';

  let {
    value,
    disabled = false,
    threadOpen = false,
    onSelect,
  }: {
    /** The crew the composer will send to. Display only — this component never
     *  writes it; the caller does, after deciding what a change costs. */
    value: Workload;
    /** Busy or resuming. See the inert effect below — this is load-bearing. */
    disabled?: boolean;
    /**
     * A persisted thread is open, so a different crew means a NEW thread. Drives
     * the "starts new chat" hint, which is said BEFORE the click: the complaint
     * this whole control answers was about state changing unannounced, so
     * announcing it only afterwards would repeat the fault one level up.
     */
    threadOpen?: boolean;
    onSelect: (wl: Workload) => void;
  } = $props();

  const uid = nextMenuUid();
  const listId = `${uid}-list`;
  const optionId = (v: Workload) => `${uid}-opt-${v}`;
  const descId = (v: Workload) => `${uid}-desc-${v}`;

  /** Catalog order (Anchor, Patch, Provision, Explore) — the same order the
   *  CapabilityCard lists, so the two surfaces never disagree about which crew
   *  is "first". The menu opens UPWARD, so Explore, the default, is the row
   *  nearest the trigger. */
  const ORDER: Workload[] = WORKLOADS.map((w) => w.value);

  let open = $state(false);
  /** The hovered/focused row, i.e. which lifecycle line is showing. Not the
   *  selection: pointing at a row explains it, it does not choose it. */
  let active = $state<Workload | null>(null);
  let rootEl = $state<HTMLDivElement | null>(null);
  let triggerEl = $state<HTMLButtonElement | null>(null);

  // Inert means inert, and that includes a menu already standing. A selection
  // runs newChat(), which cancels an in-flight live stream — so an actionable
  // row during a live turn could discard a reply the operator is watching,
  // before its conversation has reached the rail. The rail's New chat button
  // stays the deliberate way to abandon a run; this is not a second one.
  $effect(() => {
    if (disabled) {
      open = false;
      active = null;
    }
  });

  function focusOption(v: Workload): void {
    document.getElementById(optionId(v))?.focus();
  }

  async function openMenu(): Promise<void> {
    if (disabled || open) return;
    open = true;
    // Focus opens on the SELECTED option, not the first: the operator's own
    // crew is the anchor they navigate from.
    active = value;
    await tick();
    focusOption(value);
  }

  function closeMenu(restoreFocus: boolean): void {
    open = false;
    active = null;
    if (restoreFocus) triggerEl?.focus();
  }

  function toggle(): void {
    if (open) closeMenu(true);
    else void openMenu();
  }

  function choose(v: Workload): void {
    // Close BEFORE handing over: `onSelect` puts focus in the textarea (the
    // operator's next act is to type, not to reopen this), and a menu still
    // mounted would take that focus move as a reason to close itself twice.
    closeMenu(false);
    onSelect(v);
  }

  function move(from: Workload, delta: number): void {
    const i = ORDER.indexOf(from);
    // Wraps, matching the retired CrewPicker's roving nav. Also what lets the
    // focus-ring smoke walk every row and detect a completed cycle.
    const next = ORDER[(i + delta + ORDER.length) % ORDER.length];
    active = next;
    focusOption(next);
  }

  function optionKeydown(e: KeyboardEvent, v: Workload): void {
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowRight':
        e.preventDefault();
        move(v, 1);
        break;
      case 'ArrowUp':
      case 'ArrowLeft':
        e.preventDefault();
        move(v, -1);
        break;
      case 'Home':
        e.preventDefault();
        active = ORDER[0];
        focusOption(ORDER[0]);
        break;
      case 'End':
        e.preventDefault();
        active = ORDER[ORDER.length - 1];
        focusOption(ORDER[ORDER.length - 1]);
        break;
      // Both, not just Enter: a listbox option is not a button, so the browser
      // synthesises no click for either key and Space would otherwise scroll.
      case 'Enter':
      case ' ':
      case 'Spacebar':
        e.preventDefault();
        choose(v);
        break;
      case 'Escape':
        e.preventDefault();
        closeMenu(true);
        break;
      default:
        break;
    }
  }

  function triggerKeydown(e: KeyboardEvent): void {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    // Open, or — if the menu is already up and focus came back here by
    // Shift+Tab — step back into the list rather than doing nothing.
    if (open) focusOption(active ?? value);
    else void openMenu();
  }

  // Focus left the control: Tab out, or a click on something else focusable.
  // Close, and do NOT pull focus back — wherever it went is where the operator
  // sent it.
  //
  // A null relatedTarget is deliberately NOT treated as leaving. It means focus
  // went nowhere in particular (a programmatic blur(), the window losing focus),
  // which is not the operator navigating away — and the click-outside listener
  // below is what actually covers a click on non-focusable ground.
  function handleFocusOut(e: FocusEvent): void {
    if (!open) return;
    const next = e.relatedTarget as Node | null;
    if (!next || rootEl?.contains(next)) return;
    open = false;
    active = null;
  }

  $effect(() => {
    if (!open) return;
    const onPointerDown = (e: Event) => {
      if (rootEl && !rootEl.contains(e.target as Node)) {
        open = false;
        active = null;
      }
    };
    // Capture: a click on a control that stops propagation would otherwise
    // leave the menu standing over the thing it was just used on.
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  });
</script>

<div
  class="crew-menu"
  class:crew-menu--open={open}
  bind:this={rootEl}
  onfocusout={handleFocusOut}
>
  <button
    type="button"
    class="crew-menu__trigger"
    data-testid="crew-menu-trigger"
    bind:this={triggerEl}
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-controls={listId}
    aria-label={$t('composer.crewMenu.triggerAriaLabel', { crew: crewName(value) })}
    {disabled}
    onclick={toggle}
    onkeydown={triggerKeydown}
  >
    <!-- animated={false} everywhere in this control. The glyph is here as
         IDENTITY, and a loop running forever beside the caret is noise on the
         one surface ds-7ag.5 deliberately quietened. -->
    <CrewGlyph verb={value} size={16} animated={false} />
    <span class="crew-menu__name">{crewName(value)}</span>
    <Icon name="chevron-down" size={12} />
  </button>

  {#if open}
    <div class="crew-menu__popup" data-testid="crew-menu-popup">
      <!-- The lifecycle lines: ALL FOUR are in the DOM, stacked in one grid
           cell, and only the active one is painted. Two jobs, both required:

           1. NO REFLOW. The region's height is the tallest of the four, so it
              cannot change as the operator moves between rows. A region that
              grew under the cursor would shift the rows below it and the
              operator would end up pointing at a different crew. Reserving the
              height by hand would need a magic number per locale; letting the
              content reserve it cannot go stale.
           2. PER-OPTION DESCRIPTION NODES. Each row's aria-describedby points at
              its OWN node here. One shared panel whose text swaps on focus lets
              a screen reader announce the previously focused crew's line.

           aria-hidden on the container keeps the three unpainted lines out of a
           linear read; a node referenced DIRECTLY by aria-describedby is still
           included in the description (accname §4.1), which is the whole basis
           of the visually-hidden-description pattern.

           `lifecycle`, not `summary`: summary names ops-contract.yaml and
           Eventarc and runs to four lines for Anchor. That is CapabilityCard
           depth, which is one click away and stays the place for it. -->
      <div class="crew-menu__detail" aria-hidden="true">
        {#each WORKLOADS as wl (wl.value)}
          <p
            id={descId(wl.value)}
            class="crew-menu__lifecycle"
            class:crew-menu__lifecycle--on={active === wl.value}
          >
            {crewLifecycle(wl.value, $t)}
          </p>
        {/each}
      </div>

      <ul
        id={listId}
        class="crew-menu__list"
        role="listbox"
        aria-label={$t('composer.crewMenu.listAriaLabel')}
      >
        {#each WORKLOADS as wl (wl.value)}
          <li
            id={optionId(wl.value)}
            class="crew-menu__option"
            class:crew-menu__option--current={wl.value === value}
            data-testid="crew-menu-option-{wl.value}"
            role="option"
            aria-selected={wl.value === value}
            aria-describedby={descId(wl.value)}
            tabindex={wl.value === value ? 0 : -1}
            onclick={() => choose(wl.value)}
            onkeydown={(e) => optionKeydown(e, wl.value)}
            onmouseenter={() => (active = wl.value)}
            onfocus={() => (active = wl.value)}
          >
            <CrewGlyph verb={wl.value} size={18} animated={false} />
            <span class="crew-menu__option-name">{crewName(wl.value)}</span>
            <span class="crew-menu__option-descriptor">{crewDescriptor(wl.value, $t)}</span>
            {#if wl.value === value}
              <span class="crew-menu__marker">
                <Icon name="check" size={12} />{$t('composer.crewMenu.current')}
              </span>
            {:else if threadOpen}
              <!-- Never "switch" or "hand over". A handoff continues THIS thread
                   with its context; this opens a clean one. -->
              <span class="crew-menu__hint">{$t('composer.crewMenu.startsNewChat')}</span>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  /* The control sits inside the composer card, at the leading edge of the input
     row — next to the caret, not up in page furniture. flex-start so a textarea
     grown to eight lines does not drag a small pill to eight lines with it. */
  .crew-menu {
    position: relative;
    flex: 0 0 auto;
    align-self: flex-start;
  }

  .crew-menu__trigger {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: 0.5em 0.6em;
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg);
    font-family: inherit;
    font-size: var(--ds-fs-1);
    line-height: 1.4;
    cursor: pointer;
    transition: border-color var(--ds-dur-fast) var(--ds-ease),
      background-color var(--ds-dur-fast) var(--ds-ease);
  }
  .crew-menu__trigger:hover:not(:disabled) {
    border-color: var(--ds-border-strong);
    background: var(--ds-surface-2);
  }
  .crew-menu__trigger:disabled {
    cursor: not-allowed;
    color: var(--ds-muted);
    /* Shed the chrome when inert, the same way the disabled prompt field does,
       so the whole row recedes together instead of one control staying crisp. */
    background: transparent;
    border-color: transparent;
  }
  .crew-menu__name {
    font-weight: var(--ds-fw-medium);
    white-space: nowrap;
  }

  /* Opens UPWARD: the composer is pinned to the bottom of the chat column, so a
     downward menu would leave the page. */
  .crew-menu__popup {
    position: absolute;
    bottom: calc(100% + var(--ds-sp-2));
    left: 0;
    z-index: 30;
    width: max-content;
    min-width: 17rem;
    /* Never wider than the viewport it opens in: `.layout--chat` is
       overflow: hidden, so an over-wide popup would be cut, not scrolled. */
    max-width: min(24rem, calc(100vw - 6rem));
    /* NOT overflow: hidden. A row's focus ring is drawn 4px OUTSIDE its border
       box, and the padding here is what gives it room to land. */
    padding: var(--ds-sp-2);
    background: var(--ds-surface);
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    box-shadow: var(--ds-shadow-lg);
  }

  /* One grid CELL holding all four lines: the region's height is the tallest of
     them, so switching rows cannot move anything. */
  .crew-menu__detail {
    display: grid;
    padding: var(--ds-sp-2) var(--ds-sp-2) var(--ds-sp-3);
    border-bottom: 1px solid var(--ds-border);
    margin-bottom: var(--ds-sp-2);
  }
  .crew-menu__lifecycle {
    grid-area: 1 / 1;
    margin: 0;
    color: var(--ds-fg-soft);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-snug);
    opacity: 0;
    /* NO transition, and this is not an oversight. The four lines share one
       grid cell, so a cross-fade renders the outgoing line ON TOP OF the
       incoming one — two sentences of illegible overlap for the whole duration.
       A screenshot caught it; the no-reflow smoke could not, because it asserts
       on the class and the boxes, and both were correct throughout. */
  }
  .crew-menu__lifecycle--on {
    opacity: 1;
  }

  .crew-menu__list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .crew-menu__option {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    padding: var(--ds-sp-2);
    border-radius: var(--ds-radius-sm);
    cursor: pointer;
    transition: background-color var(--ds-dur-fast) var(--ds-ease);
  }
  .crew-menu__option:hover {
    background: var(--ds-surface-2);
  }
  .crew-menu__option--current {
    background: var(--ds-surface-2);
  }
  /* A fixed leading column so the descriptors line up down the menu. The names
     are proper nouns and are not translated, so this holds in both locales. */
  .crew-menu__option-name {
    min-width: 5rem;
    color: var(--ds-fg);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-medium);
    white-space: nowrap;
  }
  .crew-menu__option-descriptor {
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
  }
  .crew-menu__marker,
  .crew-menu__hint {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-1);
    /* Pushed to the trailing edge so the markers form their own column. */
    margin-inline-start: auto;
    padding-inline-start: var(--ds-sp-3);
    font-size: var(--ds-fs-1);
    white-space: nowrap;
  }
  .crew-menu__marker {
    color: var(--ds-stream-ink);
    font-weight: var(--ds-fw-medium);
  }
  .crew-menu__hint {
    color: var(--ds-muted);
  }
</style>
