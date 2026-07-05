<script lang="ts">
  // ConversationsRail — the multi-turn chat history (P2). Mirrors DecisionsRail:
  // a calm left-rail list of cards, here folded into Today/Yesterday/Older day
  // buckets (newest first). Each card resumes its thread on click. The grouping
  // is pure (lib/conversations.groupConversations); this component only renders.
  import { groupConversations, capConversations, matchesConversation } from '../lib/conversations';
  import CrewGlyph from './CrewGlyph.svelte';
  import HelpHint from './HelpHint.svelte';
  import Icon from './Icon.svelte';
  import Modal from './Modal.svelte';
  import type { Conversation } from '../lib/types';

  let {
    conversations,
    activeConversationId,
    onOpen,
    onNewChat,
    max = 5,
  }: {
    conversations: Conversation[];
    activeConversationId: string | null;
    onOpen: (conversationId: string) => void;
    /** Reset to a clean slate (drops the open thread + any historical replay).
     *  Wired to App's `newChat()`, which until now was only reachable from the
     *  historical-trace banner — so a resumed conversation had no way back to a
     *  blank composer short of reloading. */
    onNewChat: () => void;
    /** Cap the rail to the newest `max` chats; the rest live in the search
     *  modal. The active chat is pinned even when it falls outside the cap. */
    max?: number;
  } = $props();

  // The rail shows only the newest `max` (plus the active chat if it would
  // otherwise be hidden); the full list stays reachable via the search modal.
  const capped = $derived(capConversations(conversations, max, activeConversationId));
  // Bucket by day relative to the render-time clock. Recomputed whenever the
  // list changes (a new/updated conversation re-sorts + may re-bucket).
  const groups = $derived(groupConversations(capped, new Date()));

  // ---- search modal ----
  let showSearch = $state(false);
  let query = $state('');
  // Filtered + bucketed full list for the modal (not the capped rail list).
  const searchMatches = $derived(conversations.filter((c) => matchesConversation(c, query)));
  const searchGroups = $derived(groupConversations(searchMatches, new Date()));

  function openSearch(): void {
    query = '';
    showSearch = true;
  }
  // Resume from the modal: close it first so the resumed thread isn't hidden
  // behind the overlay (App scrolls the chat into view).
  function handleOpen(id: string): void {
    showSearch = false;
    onOpen(id);
  }

  // Compact, readable wall-clock for a card. Mirrors DecisionsRail.fmtCreatedAt:
  // falls back to the raw value when it doesn't parse, '' when absent.
  function fmtTime(iso: string | undefined): string {
    if (!iso) return '';
    const parsed = Date.parse(iso);
    if (Number.isNaN(parsed)) return iso;
    try {
      return new Intl.DateTimeFormat(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      }).format(parsed);
    } catch {
      return iso;
    }
  }

  // turn_count is the number of persisted turns — each user prompt or crew
  // reply counts as one. Render it as "N messages"; absent/zero → nothing.
  function turnsLabel(n: number | undefined): string {
    if (!n || n < 1) return '';
    return n === 1 ? '1 message' : `${n} messages`;
  }
</script>

{#snippet conversationItem(c: Conversation)}
  <li
    class="conv-row"
    data-testid="conversation-item"
    class:active={c.conversation_id === activeConversationId}
  >
    <button
      class="conv-open"
      data-testid="conversation-open"
      type="button"
      title={c.title}
      aria-current={c.conversation_id === activeConversationId ? 'true' : undefined}
      onclick={() => handleOpen(c.conversation_id)}
    >
      <span class="conv-glyph"><CrewGlyph verb={c.workload} size={20} animated={false} /></span>
      <span class="conv-body">
        <span class="conv-title">{c.title}</span>
        <span class="conv-meta">
          {#if c.updated_at}<time datetime={c.updated_at}>{fmtTime(c.updated_at)}</time>{/if}
          {#if turnsLabel(c.turn_count)}<span class="conv-count">· {turnsLabel(c.turn_count)}</span>{/if}
        </span>
      </span>
    </button>
  </li>
{/snippet}

<aside id="conversations-rail" data-testid="conversations-pane" aria-label="Conversations">
  <div class="rail-header">
    <h2 class="ds-label rail-eyebrow">
      <span class="eyebrow-icon"><Icon name="message-square" size={14} /></span>Conversations
    </h2>
    <!-- Clean-slate action. Resuming a thread from the rail locks the composer
         to that thread's crew, and (until this) the only "start over" affordance
         lived in the historical-trace banner — so a resumed chat had no way back
         to a blank composer short of a reload. `margin-left:auto` floats it to
         the header's trailing edge, before the help icon. -->
    <button
      type="button"
      class="rail-new-chat"
      data-testid="conversations-new-chat"
      onclick={onNewChat}
    ><Icon name="plus" size={13} />New chat</button>
    <!-- Always shown — it explains what the rail is and where the cross-crew
         "team memory" boundary sits. Mirrors DecisionsRail's header hint; the
         flex-wrap header + HelpHint's flex-basis:100% panel wrap it cleanly. -->
    <HelpHint
      testid="conversations-help"
      ariaLabel="About conversations"
      text="Your chats are saved here, so you can reopen any thread and pick up where you left off. Each conversation stays with the crew that started it. Crews can also look back at redacted snippets of each other's recent chats as shared team memory."
    />
  </div>

  {#if conversations.length === 0}
    <p class="empty ds-subtle">No conversations yet. Chats you start are saved here, so you can reopen any thread and keep going.</p>
  {:else}
    {#each groups as group (group.label)}
      <div class="conv-group" data-testid="conv-group">
        <h3 class="conv-group__label">{group.label}</h3>
        <ul class="conv-list">
          {#each group.items as c (c.conversation_id)}
            {@render conversationItem(c)}
          {/each}
        </ul>
      </div>
    {/each}

    {#if capped.length < conversations.length}
      <!-- Only when the rail actually hides chats (active-pinning can surface an
           otherwise-capped row, so compare rendered vs total, not total vs max). -->
      <button
        class="rail-more"
        data-testid="conversations-search-open"
        type="button"
        onclick={openSearch}
      >Search chats ({conversations.length}) →</button>
    {/if}
  {/if}
</aside>

<Modal open={showSearch} title="Search chats" onClose={() => (showSearch = false)}>
  <div class="search-pane">
    <input
      class="search-input"
      data-modal-autofocus
      data-testid="conversations-search-input"
      type="search"
      aria-label="Search chats by title or crew"
      placeholder="Search by title or crew…"
      bind:value={query}
    />
    <p class="search-count" data-testid="conversations-search-count" aria-live="polite">
      {searchMatches.length} of {conversations.length}
    </p>
    {#if searchMatches.length === 0}
      <p class="empty ds-subtle">No chats match “{query}”.</p>
    {:else}
      {#each searchGroups as group (group.label)}
        <div class="conv-group" data-testid="conv-search-group">
          <h3 class="conv-group__label">{group.label}</h3>
          <ul class="conv-list">
            {#each group.items as c (c.conversation_id)}
              {@render conversationItem(c)}
            {/each}
          </ul>
        </div>
      {/each}
    {/if}
  </div>
</Modal>

<style>
  #conversations-rail {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    min-height: 0;
  }

  .rail-header {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    padding: 0 var(--ds-sp-1);
  }

  .rail-eyebrow {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    color: var(--ds-fg-soft);
  }

  .eyebrow-icon {
    display: inline-flex;
    align-items: center;
    color: var(--ds-muted);
    flex-shrink: 0;
  }

  /* Quiet ghost button, floated to the header's trailing edge. Calm until
     hovered — it's a utility action, not the rail's focal point. */
  .rail-new-chat {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 0.3em;
    appearance: none;
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius-sm);
    background: var(--ds-surface);
    color: var(--ds-fg-soft);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    padding: 0.32em 0.6em;
    cursor: pointer;
    transition:
      background-color var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease),
      color var(--ds-dur) var(--ds-ease);
  }

  .rail-new-chat:hover {
    background: var(--ds-surface-2);
    border-color: var(--ds-border-strong);
    color: var(--ds-fg);
  }

  .rail-new-chat:active {
    transform: translateY(1px);
  }

  .empty {
    margin: var(--ds-sp-1) 0 0;
    padding: 0 var(--ds-sp-1);
    font-style: italic;
    color: var(--ds-faint);
  }

  .conv-group {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
  }

  /* Day bucket header — a quiet eyebrow above each cluster. */
  .conv-group__label {
    margin: 0;
    padding: 0 var(--ds-sp-1);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    color: var(--ds-faint);
  }

  .conv-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-2);
    margin: 0;
    padding: 0;
  }

  .conv-row {
    position: relative;
    border: 1px solid var(--ds-border);
    border-left: 3px solid transparent;
    border-radius: var(--ds-radius);
    background: var(--ds-surface);
    transition:
      border-color var(--ds-dur) var(--ds-ease),
      background-color var(--ds-dur) var(--ds-ease),
      box-shadow var(--ds-dur) var(--ds-ease),
      transform var(--ds-dur-fast) var(--ds-ease);
  }

  .conv-row:hover {
    background: var(--ds-surface-2);
    border-color: var(--ds-border-strong);
    box-shadow: var(--ds-shadow-sm);
    transform: translateY(-1px);
  }

  .conv-row.active {
    border-left-color: var(--ds-stream);
    border-color: var(--ds-stream-border);
    background: var(--ds-stream-surface);
  }

  /* The whole card is the resume affordance — a reset button filling the row. */
  .conv-open {
    appearance: none;
    width: 100%;
    display: flex;
    align-items: flex-start;
    gap: var(--ds-sp-3);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    border: none;
    background: none;
    text-align: left;
    cursor: pointer;
    color: inherit;
    font: inherit;
  }

  .conv-glyph {
    display: inline-flex;
    align-items: center;
    color: var(--ds-muted);
    flex-shrink: 0;
    margin-top: 1px;
  }

  .conv-body {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-1);
    min-width: 0;
    flex: 1 1 auto;
  }

  .conv-title {
    font-size: var(--ds-fs-2);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-fg);
    line-height: var(--ds-lh-snug);
    /* Keep long first-prompt titles to two tidy lines. */
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .conv-meta {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--ds-sp-1);
    font-size: var(--ds-fs-1);
    color: var(--ds-muted);
    font-variant-numeric: tabular-nums;
  }

  .conv-count {
    color: var(--ds-faint);
  }
</style>
