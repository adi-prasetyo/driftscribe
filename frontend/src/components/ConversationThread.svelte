<script lang="ts">
  // ConversationThread — the multi-turn record of one conversation (P2). Renders
  // the persisted turns oldest-first as alternating bubbles: the operator's
  // prompt and the crew's reply. Reply text is ESCAPED PLAIN TEXT (Svelte
  // auto-escapes `{turn.text}`; white-space: pre-wrap keeps the agent's own line
  // breaks) — deliberately NOT Markdown, matching the chat reply-plain-text XSS
  // stance. Each crew turn links to its reasoning trace, and surfaces a PR CTA
  // when that turn opened one.
  import CrewGlyph from './CrewGlyph.svelte';
  import { crewName } from '../lib/workloads';
  import { iacApprovalHref } from '../lib/approval';
  import type { ConversationTurn } from '../lib/types';

  let {
    turns,
    onOpenTrace,
  }: {
    turns: ConversationTurn[];
    onOpenTrace: (traceId: string) => void;
  } = $props();

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
        {@const live = turn.optimistic === true}
        {@const pending = turn.pending === true}
        <li class="turn turn--crew" data-testid="thread-turn-crew">
          <!-- The glyph loops only while the reply is still streaming (pending);
               it rests on its static healthy frame otherwise. CrewGlyph honors
               prefers-reduced-motion internally. -->
          <span class="turn__glyph"><CrewGlyph verb={turn.workload ?? ''} size={22} animated={pending} /></span>
          <!-- Optimistic (live) crew bubble is a polite live region so screen
               readers hear the "generating" state and then the reply landing in
               the SAME node. Persisted / historical turns get no live region
               (else a rehydrated thread would re-announce every past reply). -->
          <div
            class="bubble bubble--crew"
            role={live ? 'status' : undefined}
            aria-live={live ? 'polite' : undefined}
          >
            <p class="turn__byline">{crewName(turn.workload)}</p>
            {#if pending}
              <div class="turn__typing" data-testid="thread-typing" aria-hidden="true">
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
              </div>
              <span class="turn__sr">Generating reply&hellip;</span>
            {:else}
              <div class="turn__text">{turn.text}</div>
            {/if}
            <!-- Suppress the action links on an optimistic turn: clicking "open
                 trace" before it settles bumps runSeq and drops the in-flight
                 settle (the turn would never persist). They reappear on the
                 persisted turn a beat later. -->
            {#if !live}
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
            {/if}
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

  /* Pending crew bubble: three dots gently rise in sequence while the reply
     streams. The base (un-animated) state rests at a visible dim opacity, so
     under prefers-reduced-motion the dots are a legible static "typing" mark
     and the sr-only line carries the meaning for assistive tech. */
  .turn__typing {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    padding: var(--ds-sp-1) 0;
  }
  .typing-dot {
    width: 0.4rem;
    height: 0.4rem;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-muted);
    opacity: 0.35;
    animation: thread-typing 1.2s var(--ds-ease) infinite;
  }
  .typing-dot:nth-child(2) {
    animation-delay: 0.16s;
  }
  .typing-dot:nth-child(3) {
    animation-delay: 0.32s;
  }
  @keyframes thread-typing {
    0%,
    60%,
    100% {
      opacity: 0.3;
      transform: translateY(0);
    }
    30% {
      opacity: 0.9;
      transform: translateY(-2px);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .typing-dot {
      animation: none;
    }
  }

  /* Visually hidden, still announced by the bubble's live region. */
  .turn__sr {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    border: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    clip-path: inset(50%);
    white-space: nowrap;
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
