<script lang="ts">
  /**
   * SealStamp — the 判子 (hanko) approval stamp: vermilion (`--ds-seal`), the
   * ONLY red in the palette, reserved for the single moment a human approved
   * something (docs/plans/2026-07-28-composite-mockup.html §"stamped desk").
   * Never reuse --ds-seal for anything else, and never introduce another red
   * (styles.test.ts pins #c0392b to exactly one occurrence, in tokens.css).
   *
   * Two sizes: `lg` is the desk's stamped hero state (mockup `.seal`), `sm` is
   * the ledger strip's per-row mini stamp (mockup `.mini`). Both share the
   * same `stampIn` overshoot keyframe (1.9 → .94 → 1 scale) — that overshoot
   * and its cubic-bezier are what make it read as a physical stamp hitting
   * paper rather than a fade-in; don't "clean up" those numbers.
   *
   * Positioning: the mockup's `.seal` is `position:absolute` against one
   * specific parent in that static page. This component intentionally does
   * NOT hardcode absolute positioning — where the seal sits (desk hero vs.
   * `align-self:center` in a ledger row) is the consumer's layout call, not
   * this component's. `align-self: center` IS kept on the `sm` variant
   * because it's normal in-flow flex participation (mirrors mockup `.mini`),
   * not an out-of-flow coordinate.
   *
   * `animate` (default FALSE, desk-only): gates the `stampIn` keyframe. The
   * ledger strip renders many `sm` stamps at once on every re-render, and
   * only the single freshly-approved desk stamp should ever fire the
   * animation — so callers opt in explicitly rather than it firing broadly.
   *
   * The un-animated base CSS state (rotate(-11deg), opacity .85, implicit
   * scale(1)) is IDENTICAL to `stampIn`'s 100% frame, so a
   * prefers-reduced-motion user (global reset in base.css, plus the local
   * belt-and-suspenders override below, matching CrewGlyph/ReplyPending/
   * TraceBadge) lands on the correct resting picture, not a broken mid-stamp
   * one.
   *
   * `role="img"` needs an accessible name: the visible glyph text stays 承認
   * in both locales (it's a hanko, not a translated word), but the
   * accessible name is localized via `desk.seal.ariaLabel` so EN screen
   * readers get "Approved" rather than an unexplained kanji glyph.
   */
  import { t } from '../lib/i18n';

  let {
    size = 'lg',
    animate = false,
  }: { size?: 'lg' | 'sm'; animate?: boolean } = $props();
</script>

<span
  class="seal-stamp seal-stamp--{size}"
  class:seal-stamp--animate={animate}
  role="img"
  aria-label={$t('desk.seal.ariaLabel')}
>承認</span>

<style>
  .seal-stamp {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ds-seal);
    font-weight: 700;
    line-height: 1.1;
    text-align: center;
    border-radius: 50%;
    transform: rotate(-11deg);
    opacity: 0.85;
  }

  .seal-stamp--lg {
    width: 76px;
    height: 76px;
    border: 3px solid var(--ds-seal);
    font-size: 19px;
    letter-spacing: 0.04em;
  }

  .seal-stamp--sm {
    width: 30px;
    height: 30px;
    flex: none;
    align-self: center;
    border: 1.5px solid var(--ds-seal);
    font-size: 10px;
    opacity: 0.8;
  }

  .seal-stamp--animate {
    animation: seal-stamp-in 0.38s cubic-bezier(0.2, 0.7, 0.3, 1.2);
  }

  @keyframes seal-stamp-in {
    0% {
      transform: rotate(-11deg) scale(1.9);
      opacity: 0;
    }
    62% {
      transform: rotate(-11deg) scale(0.94);
      opacity: 0.9;
    }
    100% {
      transform: rotate(-11deg) scale(1);
      opacity: 0.85;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .seal-stamp--animate {
      animation: none;
    }
  }
</style>
