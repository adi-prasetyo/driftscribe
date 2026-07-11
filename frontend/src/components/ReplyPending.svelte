<script lang="ts">
  // ReplyPending — the loading placeholder that occupies the hero slot while a
  // live /chat request is in flight and no reply has landed yet.
  //
  // App.svelte renders this with `{#if busy && finalReply == null}`: it appears
  // the instant Send is pressed (busy=true), and vanishes the moment the reply —
  // or an error — lands (finalReply != null). It sits in the SAME slot the
  // FinalResponse hero will fill, with a matching footprint and eyebrow so the
  // swap to the real prose is seamless; the left accent rail is the in-progress
  // blue (--ds-stream) rather than the hero's settled green.
  //
  // Visual: a shimmer skeleton — three placeholder bars with a light sweeping
  // L→R, evoking the prose about to arrive. Purely decorative (aria-hidden); a
  // single sr-only line carries the announcement for assistive tech. The shimmer
  // is neutralised under prefers-reduced-motion (both the global base.css reset
  // and the explicit rule below, matching the TraceBadge precedent).
  import { t } from '../lib/i18n';
</script>

<section class="ds-card reply-pending" data-testid="reply-pending" role="status">
  <p class="ds-label reply-pending__label">{$t('misc.coordinatorReply.label')}</p>
  <span class="reply-pending__sr">{$t('misc.replyPending.sr')}</span>
  <div class="reply-pending__bars" aria-hidden="true">
    <span class="skel-bar skel-bar--w1"></span>
    <span class="skel-bar skel-bar--w2"></span>
    <span class="skel-bar skel-bar--w3"></span>
  </div>
</section>

<style>
  /* Hero footprint, in-progress blue rail — mirrors FinalResponse so the swap to
     the real reply doesn't shift layout. */
  .reply-pending {
    position: relative;
    border-left: 3px solid var(--ds-stream);
    padding: var(--ds-sp-5) var(--ds-sp-6);
    box-shadow: var(--ds-shadow);
  }

  .reply-pending::before {
    content: '';
    position: absolute;
    inset: 0 auto 0 0;
    width: 3px;
    background: linear-gradient(
      to bottom,
      var(--ds-stream),
      color-mix(in srgb, var(--ds-stream) 35%, transparent)
    );
    border-top-left-radius: var(--ds-radius);
    border-bottom-left-radius: var(--ds-radius);
  }

  .reply-pending__label {
    display: block;
    margin: 0 0 var(--ds-sp-4);
    color: var(--ds-stream-ink);
  }

  /* Visually hidden, still announced. */
  .reply-pending__sr {
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

  .reply-pending__bars {
    display: flex;
    flex-direction: column;
    gap: var(--ds-sp-3);
    max-width: var(--ds-measure);
  }

  /* A placeholder bar: a sunken surface with a soft blue highlight sweeping
     across it. The 200%-wide gradient is slid via background-position. */
  .skel-bar {
    height: 0.85rem;
    border-radius: var(--ds-radius-pill);
    background: linear-gradient(
      90deg,
      var(--ds-surface-2) 0%,
      color-mix(in srgb, var(--ds-stream-surface) 70%, var(--ds-surface-2)) 50%,
      var(--ds-surface-2) 100%
    );
    background-size: 200% 100%;
    animation: reply-shimmer 1.4s var(--ds-ease) infinite;
  }

  /* Staggered widths so the block reads as prose-in-waiting, not a progress bar. */
  .skel-bar--w1 {
    width: 92%;
  }
  .skel-bar--w2 {
    width: 78%;
  }
  .skel-bar--w3 {
    width: 46%;
  }

  @keyframes reply-shimmer {
    0% {
      background-position: 200% 0;
    }
    100% {
      background-position: -200% 0;
    }
  }

  /* Belt-and-suspenders alongside the global base.css reduced-motion reset. */
  @media (prefers-reduced-motion: reduce) {
    .skel-bar {
      animation: none;
    }
  }
</style>
