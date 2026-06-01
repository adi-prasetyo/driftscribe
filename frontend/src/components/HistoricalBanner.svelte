<script lang="ts">
  import { shortTrace } from '../lib/format';

  // Slim advisory banner shown while the operator is replaying a *past* trace
  // (historical mode) instead of a live session. The id / data-testid /
  // attribute strings below are a hard e2e contract (Appendix B) — keep verbatim.
  // When `active` is false the component renders nothing at all.
  let {
    active,
    traceId,
    onNewChat,
  }: {
    active: boolean;
    traceId: string | null;
    onNewChat: () => void;
  } = $props();
</script>

{#if active}
  <div
    id="historical-badge"
    data-testid="historical-banner"
    data-active="1"
    aria-live="polite"
    class="historical-banner"
  >
    <span class="historical-banner__label">
      <span class="historical-banner__dot" aria-hidden="true"></span>
      viewing historical trace
      <code id="historical-trace-id" class="ds-code">{shortTrace(traceId ?? '')}</code>
    </span>
    <button
      id="new-chat-btn"
      type="button"
      class="historical-banner__new"
      onclick={onNewChat}
    >
      ← new chat
    </button>
  </div>
{/if}

<style>
  .historical-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--ds-sp-3);
    flex-wrap: wrap;
    padding: var(--ds-sp-2) var(--ds-sp-4);
    background: var(--ds-warn-surface);
    border: 1px solid var(--ds-warn-border);
    border-left: 3px solid var(--ds-warn);
    border-radius: var(--ds-radius-sm);
    color: var(--ds-warn-ink);
    font-size: var(--ds-fs-1);
    line-height: var(--ds-lh-snug);
  }

  .historical-banner__label {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    font-weight: var(--ds-fw-medium);
  }

  /* small pulsing amber dot signalling "not live" */
  .historical-banner__dot {
    width: 0.5em;
    height: 0.5em;
    flex: 0 0 auto;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-warn);
    box-shadow: 0 0 0 0 rgba(154, 107, 0, 0.4);
    animation: historical-pulse 2.2s var(--ds-ease) infinite;
  }

  @keyframes historical-pulse {
    0% {
      box-shadow: 0 0 0 0 rgba(154, 107, 0, 0.4);
    }
    70% {
      box-shadow: 0 0 0 0.35em rgba(154, 107, 0, 0);
    }
    100% {
      box-shadow: 0 0 0 0 rgba(154, 107, 0, 0);
    }
  }

  /* the historical-mode code chip leans amber to match the banner */
  .historical-banner__label :global(.ds-code) {
    background: rgba(255, 255, 255, 0.55);
    border-color: var(--ds-warn-border);
    color: var(--ds-warn-ink);
  }

  /* subtle ghost button — quiet until hovered */
  .historical-banner__new {
    flex: 0 0 auto;
    appearance: none;
    border: 1px solid transparent;
    border-radius: var(--ds-radius-sm);
    background: transparent;
    color: var(--ds-warn-ink);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    line-height: 1.2;
    padding: 0.32em 0.7em;
    cursor: pointer;
    transition: background-color var(--ds-dur) var(--ds-ease),
      border-color var(--ds-dur) var(--ds-ease);
  }

  .historical-banner__new:hover {
    background: rgba(255, 255, 255, 0.6);
    border-color: var(--ds-warn-border);
  }

  .historical-banner__new:active {
    transform: translateY(1px);
  }

  @media (prefers-reduced-motion: reduce) {
    .historical-banner__dot {
      animation: none;
    }
  }
</style>
