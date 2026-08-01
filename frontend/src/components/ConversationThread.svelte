<script lang="ts">
  // ConversationThread — the multi-turn record of one conversation (P2). Renders
  // the persisted turns oldest-first as alternating bubbles: the operator's
  // prompt and the crew's reply. Reply text is ESCAPED PLAIN TEXT (Svelte
  // auto-escapes `{turn.text}`; white-space: pre-wrap keeps the agent's own line
  // breaks) — deliberately NOT Markdown, matching the chat reply-plain-text XSS
  // stance. Each crew turn carries its reasoning INLINE as a collapsed
  // disclosure, and surfaces a PR CTA when that turn opened one.
  import CrewGlyph from './CrewGlyph.svelte';
  import ReasoningDisclosure from './ReasoningDisclosure.svelte';
  import { crewName } from '../lib/workloads';
  import { turnOwnsReasoning } from '../lib/conversations';
  import { iacApprovalHref } from '../lib/approval';
  import { t, locale } from '../lib/i18n';
  import type { TraceCache } from '../lib/traceCache';
  import type { ConversationTurn } from '../lib/types';

  let {
    turns,
    cache,
    conversationId = null,
    autoExpandTraceId = null,
  }: {
    turns: ConversationTurn[];
    /** Per-trace state for the inline disclosures. Required rather than
     *  optional: a thread mounted without it would silently render no
     *  reasoning at all, which is the one thing this view exists to show. */
    cache: TraceCache;
    /** The open thread's id, so a copied disclosure link can carry thread
     *  context (design §4). Null on a not-yet-persisted conversation. */
    conversationId?: string | null;
    /** The turn a `?conversation=&reasoning=` deep link named: its disclosure
     *  opens on mount and scrolls into view (ds-jns PR 2). At most one turn
     *  matches — a trace id belongs to one turn. */
    autoExpandTraceId?: string | null;
  } = $props();

  // Same-origin /iac-approvals/<n> link for a turn that opened an infra PR.
  function prHref(turn: ConversationTurn): string | null {
    return turn.iac_pr ? iacApprovalHref(turn.iac_pr.pr_number, $locale) : null;
  }

  // Server-authored transition rows (`crew_change` / `handoff_declined`) are
  // NOT crew bubbles: nobody said them. They render as a quiet centered rule
  // recording that the conversation changed hands — the one place the thread
  // shows a `workload` rewrite happening, which is why the row names both the
  // crew that left and the crew that arrived rather than just the survivor.
  const TRANSITION_ROLES = ['crew_change', 'handoff_declined'];
  function isTransition(turn: ConversationTurn): boolean {
    return TRANSITION_ROLES.includes(turn.role);
  }
  // `turnOwnsReasoning` (lib/conversations) is the SAME predicate App uses to
  // decide whether a `?conversation=&reasoning=` deep link names a message this
  // thread shows. Shared rather than restated: the two drifted once already,
  // leaving the URL claiming a message that renders no disclosure to open.
  // Both crews, resolved to display names. Falls back to the row's own
  // workload so a pre-`handoff` row (or a truncated one) still reads sensibly
  // instead of rendering an empty proper noun.
  function transitionCrews(turn: ConversationTurn): { from: string; to: string } {
    const fallback = crewName(turn.workload);
    return {
      from: turn.handoff?.from ? crewName(turn.handoff.from) : fallback,
      to: turn.handoff?.to ? crewName(turn.handoff.to) : fallback,
    };
  }
</script>

<!-- tabindex=-1 so openConversation can move focus here on resume (mirrors the
     open-trace focus move into #historical-badge), announcing the loaded thread
     to keyboard / screen-reader users instead of stranding them on the rail. -->
<section
  id="conversation-thread"
  data-testid="conversation-thread"
  aria-label={$t('conversations.thread.ariaLabel')}
  tabindex="-1"
>
  <ol class="thread">
    {#each turns as turn (turn.seq)}
      {#if isTransition(turn)}
        {@const accepted = turn.role === 'crew_change'}
        {@const crews = transitionCrews(turn)}
        <li
          class="turn turn--transition"
          class:turn--declined={!accepted}
          data-testid={accepted ? 'thread-turn-crew-change' : 'thread-turn-handoff-declined'}
        >
          <p class="transition__line">
            <span class="transition__glyph">
              <CrewGlyph verb={turn.workload ?? ''} size={16} animated={false} />
            </span>
            <span
              class="transition__label"
              aria-label={accepted
                ? $t('conversations.thread.crewChangeAria', crews)
                : $t('conversations.thread.handoffDeclinedAria', crews)}
              >{accepted
                ? $t('conversations.thread.crewChange', crews)
                : $t('conversations.thread.handoffDeclined', crews)}</span>
          </p>
          <!-- The proposing crew's stated reason, carried verbatim. Model-
               authored text about operator-supplied content, so it renders as
               escaped plain text behind a quotation rule — never as markup,
               and never styled to look like the system said it. -->
          {#if turn.text}
            <p class="transition__reason" data-testid="thread-transition-reason">{turn.text}</p>
          {/if}
        </li>
      {:else if turn.role === 'user'}
        <li class="turn turn--user" data-testid="thread-turn-user">
          <div class="bubble bubble--user">
            <p class="turn__byline">{$t('conversations.thread.you')}</p>
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
          <!-- An ephemeral turn can carry an error instead of a reply (network
               failure, refused request, interrupted stream). It stays in the
               thread rather than falling back to a different layout, so it has
               to LOOK like what it is. -->
          <div
            class="bubble bubble--crew"
            class:bubble--error={turn.isError === true}
            data-testid={turn.isError === true ? 'thread-turn-error' : undefined}
            role={live ? 'status' : undefined}
            aria-live={live ? 'polite' : undefined}
          >
            <p class="turn__byline">{crewName(turn.workload)}</p>
            <!-- The reasoning line sits between the crew header and the reply,
                 where the thinking happened relative to the answer. It renders
                 on an OPTIMISTIC turn too — unlike the action links below —
                 because a live run's whole value is watching it think. Safe to
                 do so: expanding only reads the per-trace cache (a no-op while
                 the stream runs), where the old open-trace button bumped runSeq
                 and dropped the in-flight settle. -->
            {#if turnOwnsReasoning(turn)}
              <ReasoningDisclosure
                traceId={turn.trace_id}
                {cache}
                {conversationId}
                autoExpand={autoExpandTraceId !== null && turn.trace_id === autoExpandTraceId}
              />
            {/if}
            {#if pending}
              <div class="turn__typing" data-testid="thread-typing" aria-hidden="true">
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
                <span class="typing-dot"></span>
              </div>
              <span class="turn__sr">{$t('conversations.thread.generatingReply')}</span>
            {:else}
              <div class="turn__text">{turn.text}</div>
            {/if}
            <!-- Suppress the PR link on an optimistic turn: it points at an
                 approval page for a turn that has not persisted yet. It
                 reappears on the persisted turn a beat later. -->
            {#if !live}
              <div class="turn__actions">
                {#if prUrl}
                  <a
                    class="turn-link"
                    data-testid="thread-pr-link"
                    href={prUrl}
                    target="_blank"
                    rel="noopener">{$t('conversations.thread.reviewPr', { n: turn.iac_pr?.pr_number ?? 0 })}</a>
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

  /* A transition is neither side of the dialogue — it is the record that the
     dialogue changed hands. Centered between hairlines so it reads as a seam
     in the transcript rather than as another speaker. */
  .turn--transition {
    flex-direction: column;
    align-items: center;
    gap: var(--ds-sp-1);
  }

  .transition__line {
    display: flex;
    align-items: center;
    gap: var(--ds-sp-2);
    width: 100%;
    margin: 0;
  }
  .transition__line::before,
  .transition__line::after {
    content: '';
    flex: 1 1 var(--ds-sp-4);
    border-top: 1px solid var(--ds-border);
  }
  /* A declined handoff gets a broken rule: the seam was offered, not crossed. */
  .turn--declined .transition__line::before,
  .turn--declined .transition__line::after {
    border-top-style: dashed;
  }

  .transition__glyph {
    display: inline-flex;
    align-items: center;
    color: var(--ds-muted);
    flex-shrink: 0;
  }

  .transition__label {
    font-size: 0.6875rem; /* 11px — matches the turn bylines */
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    color: var(--ds-muted);
    text-align: center;
  }

  /* The proposing crew's reason, behind a quotation rule so it reads as
     something a crew said, not as a system statement. */
  .transition__reason {
    margin: 0;
    max-width: min(34rem, 84%);
    padding-left: var(--ds-sp-3);
    border-left: 2px solid var(--ds-border);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-body);
    color: var(--ds-muted);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
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
  .bubble--error {
    border-color: var(--ds-danger-border, var(--ds-danger-ink));
    background: var(--ds-danger-surface, var(--ds-surface));
  }
  .bubble--error .turn__text {
    color: var(--ds-danger-ink);
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
  /* Underline only — brightening to raw --ds-stream would put the text at
     3.42:1 on paper, under the floor (ds-qbo). */
  .turn-link:hover {
    text-decoration: underline;
  }
</style>
