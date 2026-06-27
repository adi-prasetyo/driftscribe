<script lang="ts">
  // ConversationThread — the multi-turn record of one conversation (P2). Renders
  // the persisted turns oldest-first as alternating bubbles: the operator's
  // prompt and the crew's reply. Reply text is ESCAPED PLAIN TEXT (Svelte
  // auto-escapes `{turn.text}`; white-space: pre-wrap keeps the agent's own line
  // breaks) — deliberately NOT Markdown, matching the chat reply-plain-text XSS
  // stance. Each crew turn links to its reasoning trace, and surfaces a PR CTA
  // when that turn opened one.
  import CrewGlyph from './CrewGlyph.svelte';
  import { WORKLOADS } from '../lib/workloads';
  import { iacApprovalHref } from '../lib/approval';
  import type { ConversationTurn } from '../lib/types';

  let {
    turns,
    onOpenTrace,
  }: {
    turns: ConversationTurn[];
    onOpenTrace: (traceId: string) => void;
  } = $props();

  // workload value → display name ("drift" → "Anchor"), for the crew bubble
  // byline. Falls back to the raw value for an unknown crew.
  const CREW_NAME = new Map(WORKLOADS.map((w) => [w.value, w.name]));
  function crewName(workload: string | undefined): string {
    return (workload && CREW_NAME.get(workload as never)) || workload || 'Crew';
  }

  // Same-origin /iac-approvals/<n> link for a turn that opened an infra PR.
  function prHref(turn: ConversationTurn): string | null {
    return turn.iac_pr ? iacApprovalHref(turn.iac_pr.pr_number) : null;
  }
</script>

<!-- tabindex=-1 so openConversation can move focus here on resume (mirrors the
     open-trace focus move into #historical-badge), announcing the loaded thread
     to keyboard / screen-reader users instead of stranding them on the rail. -->
<section
  id="conversation-thread"
  data-testid="conversation-thread"
  aria-label="Conversation history"
  tabindex="-1"
>
  <ol class="thread">
    {#each turns as turn (turn.seq)}
      {#if turn.role === 'user'}
        <li class="turn turn--user" data-testid="thread-turn-user">
          <div class="bubble bubble--user">
            <p class="turn__byline">You</p>
            <div class="turn__text">{turn.text}</div>
          </div>
        </li>
      {:else}
        {@const prUrl = prHref(turn)}
        <li class="turn turn--crew" data-testid="thread-turn-crew">
          <span class="turn__glyph"><CrewGlyph verb={turn.workload ?? ''} size={22} animated={false} /></span>
          <div class="bubble bubble--crew">
            <p class="turn__byline">{crewName(turn.workload)}</p>
            <div class="turn__text">{turn.text}</div>
            <div class="turn__actions">
              {#if turn.trace_id}
                <button
                  class="turn-link"
                  data-testid="thread-open-trace"
                  type="button"
                  aria-label={`Open trace for turn ${turn.seq + 1}`}
                  onclick={() => onOpenTrace(turn.trace_id as string)}>open trace →</button>
              {/if}
              {#if prUrl}
                <a
                  class="turn-link"
                  data-testid="thread-pr-link"
                  href={prUrl}
                  target="_blank"
                  rel="noopener">Review PR #{turn.iac_pr?.pr_number} →</a>
              {/if}
            </div>
          </div>
        </li>
      {/if}
    {/each}
  </ol>
</section>

<style>
  #conversation-thread {
    display: block;
  }

  .thread {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
  }

  .turn {
    display: flex;
    gap: var(--ds-sp-2);
    max-width: 100%;
  }

  /* The operator's prompt sits to the right; the crew reply to the left with
     its glyph, so the back-and-forth reads as a dialogue. */
  .turn--user {
    justify-content: flex-end;
  }
  .turn--crew {
    justify-content: flex-start;
    align-items: flex-start;
  }

  .turn__glyph {
    display: inline-flex;
    align-items: center;
    color: var(--ds-muted);
    flex-shrink: 0;
    margin-top: var(--ds-sp-2);
  }

  .bubble {
    border: 1px solid var(--ds-border);
    border-radius: var(--ds-radius);
    padding: var(--ds-sp-3) var(--ds-sp-4);
    max-width: min(46rem, 88%);
    min-width: 0;
  }

  .bubble--user {
    background: var(--ds-stream-surface);
    border-color: var(--ds-stream-border);
  }
  .bubble--crew {
    background: var(--ds-surface);
  }

  .turn__byline {
    margin: 0 0 var(--ds-sp-1);
    font-size: 0.6875rem; /* 11px — quiet attribution */
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    color: var(--ds-muted);
  }

  .turn__text {
    font-family: var(--ds-font);
    font-size: var(--ds-fs-2);
    line-height: var(--ds-lh-body);
    color: var(--ds-fg);
    /* Honor the agent's own line breaks; never let a long token blow out width. */
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .turn__actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2) var(--ds-sp-3);
    margin-top: var(--ds-sp-2);
  }

  .turn-link {
    appearance: none;
    border: none;
    background: none;
    padding: 0;
    margin: 0;
    cursor: pointer;
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    color: var(--ds-stream-ink);
    line-height: 1.4;
    text-decoration: none;
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }
  .turn-link:hover {
    color: var(--ds-stream);
    text-decoration: underline;
  }
</style>
