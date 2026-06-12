<script lang="ts">
  import { shortTrace } from '../lib/format';
  import type { TimelineStatus } from '../lib/timeline';
  import Icon from './Icon.svelte';

  // The live identity strip for the current run: a copy-to-clipboard trace
  // pill + a lifecycle status pill. Rendered into <section id="trace-badge"
  // aria-live="polite"> (Appendix B). When traceId is null the section stays
  // in the DOM but empty (so the aria-live region is stable across renders).
  let {
    traceId,
    status,
  }: {
    traceId: string | null;
    status: TimelineStatus;
  } = $props();

  // status -> { label shown on the pill, ds-pill modifier class }.
  // 'streaming' has no ds-pill modifier — its blue variant is styled below
  // via the scoped .status-pill.streaming rule, so its modifier is ''.
  const STATUS_META: Record<TimelineStatus, { label: string; pill: string }> = {
    pending: { label: 'pending', pill: 'ds-pill--muted' },
    streaming: { label: 'streaming', pill: '' },
    complete: { label: 'complete', pill: 'ds-pill--ok' },
    stalled: { label: 'stalled · logs lagging', pill: 'ds-pill--warn' },
    error: { label: 'error', pill: 'ds-pill--danger' },
    // A past decision opened from the rail — a snapshot replay, not a live
    // stream. Green (settled) styling, no streaming dot. Matches the legacy
    // setStatusPill("complete", "historical").
    historical: { label: 'historical', pill: 'ds-pill--ok' },
  };

  const meta = $derived(STATUS_META[status]);
  // Keep the base 'status-pill' class AND the per-state class (the ds-pill
  // modifier, or 'streaming' for the scoped blue variant) on the span — the
  // per-state class IS the modifier.
  const statusClass = $derived(
    'ds-pill status-pill ' + (status === 'streaming' ? 'streaming' : meta.pill),
  );

  // Briefly reflect the copy action on the pill (visual + a11y title).
  let copied = $state(false);
  let copyTimer: ReturnType<typeof setTimeout> | undefined;

  async function copyTrace(): Promise<void> {
    if (!traceId) return;
    try {
      await navigator.clipboard.writeText(traceId);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => {
        copied = false;
      }, 1200);
    } catch {
      // Clipboard unavailable (insecure context / denied) — fail quietly;
      // the full id is still visible via the title attribute.
    }
  }
</script>

<section id="trace-badge" aria-live="polite">
  {#if traceId !== null}
    <button
      type="button"
      class="trace-pill ds-code"
      class:copied
      title="click to copy trace id"
      onclick={copyTrace}
    >
      <Icon name="copy" size={12} />
      <span class="trace-pill__id">{shortTrace(traceId)}</span>
      <span class="trace-pill__hint" aria-hidden="true">{copied ? 'copied' : 'copy'}</span>
    </button>
    <span id="status-pill" class={statusClass}>
      {#if status === 'streaming'}<span class="status-pill__dot" aria-hidden="true"></span>{/if}
      {meta.label}
    </span>
  {/if}
</section>

<style>
  #trace-badge {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--ds-sp-2) var(--ds-sp-3);
    min-height: 1.9rem; /* reserve the row so layout doesn't jump when it fills */
  }

  /* --- Trace pill: a copy-to-clipboard chip wrapping the .ds-code mono id --- */
  .trace-pill {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-2);
    /* ds-code supplies the mono face + inset surface; override the padding so
       it reads as an interactive chip rather than inline code. */
    padding: 0.18em 0.55em 0.18em 0.7em;
    cursor: pointer;
    line-height: 1.4;
    transition: background-color var(--ds-dur-fast) var(--ds-ease),
      border-color var(--ds-dur-fast) var(--ds-ease),
      color var(--ds-dur-fast) var(--ds-ease);
  }

  .trace-pill:hover {
    background: var(--ds-stream-surface);
    border-color: var(--ds-stream-border);
    color: var(--ds-stream-ink);
  }

  .trace-pill:active {
    transform: translateY(1px);
  }

  .trace-pill__id {
    letter-spacing: 0.01em;
  }

  /* tiny affordance label that fades from "copy" to "copied" */
  .trace-pill__hint {
    font-size: 0.78em;
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    color: var(--ds-faint);
    transition: color var(--ds-dur-fast) var(--ds-ease);
  }

  .trace-pill:hover .trace-pill__hint {
    color: var(--ds-stream-ink);
  }

  .trace-pill.copied {
    background: var(--ds-ok-surface);
    border-color: var(--ds-ok-border);
    color: var(--ds-ok-ink);
  }

  .trace-pill.copied .trace-pill__hint {
    color: var(--ds-ok-ink);
  }

  /* --- Status pill: streaming variant (blue) per contract ------------------ */
  .status-pill.streaming {
    background: var(--ds-stream-surface);
    color: var(--ds-stream-ink);
    border-color: var(--ds-stream-border);
  }

  /* a soft pulsing dot to signal the live stream */
  .status-pill__dot {
    width: 0.5em;
    height: 0.5em;
    border-radius: var(--ds-radius-pill);
    background: var(--ds-stream);
    animation: trace-badge-pulse 1.4s var(--ds-ease) infinite;
  }

  @keyframes trace-badge-pulse {
    0%,
    100% {
      opacity: 1;
      transform: scale(1);
    }
    50% {
      opacity: 0.4;
      transform: scale(0.78);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .status-pill__dot {
      animation: none;
    }
  }
</style>
