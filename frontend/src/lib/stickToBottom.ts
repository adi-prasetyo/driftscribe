// stickToBottom — keep a scrollable thread pinned to its newest content while
// the operator is reading the bottom, and get out of the way the moment they
// scroll up to read something older (ds-jns PR 3).
//
// The whole reason this is not just `scrollTop = scrollHeight` on every update:
// a crew turn streams. Tokens, thought rows and tool calls arrive over tens of
// seconds, so an unconditional follow would yank the viewport away from an
// operator who scrolled up mid-run to re-read an earlier decision — repeatedly,
// for as long as the run lasts.
//
// The arm/disarm rule is deliberately evaluated ONLY on user scrolls, never on
// content growth. Growth raises `scrollHeight` a frame before the follow scroll
// lands, so a rule that re-read the geometry on mutation would see a distance
// it created itself and disarm on its own output. Mutations ask a different
// question ("are we following? then follow"), and never revise the answer.

import { prefersReducedMotion } from './motion';

/** How far from the bottom the operator may sit and still count as "reading the
 *  newest". Roughly one turn header — far enough that a stray trackpad nudge or
 *  the browser's own scroll anchoring does not disarm the follow, close enough
 *  that a deliberate scroll up does. */
export const STICK_THRESHOLD_PX = 80;

/** The three numbers any scrollable element reports. Taken as a plain struct so
 *  the rule below is testable without a DOM or a layout engine — jsdom reports
 *  every one of these as 0 (no layout), which would make an element-typed
 *  signature untestable in the unit suite. */
export interface ScrollGeometry {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/**
 * Pixels between the bottom edge of the viewport and the bottom of the content.
 * Zero when pinned to the bottom.
 *
 * Clamped at 0 because two ordinary situations produce a NEGATIVE raw value and
 * neither means "below the bottom": content shorter than its container
 * (scrollHeight < clientHeight), and elastic/rubber-band overscroll, where a
 * touchpad flick drives scrollTop past the maximum for the length of the
 * animation. Both are as pinned as it is possible to be.
 */
export function distanceFromBottom(g: ScrollGeometry): number {
  const raw = g.scrollHeight - g.scrollTop - g.clientHeight;
  return Number.isFinite(raw) ? Math.max(0, raw) : 0;
}

/**
 * Should the region follow new content, given where the operator is sitting?
 *
 * Call this on USER scroll events only — see the module header for why content
 * growth must not re-ask it. A non-finite geometry (a detached node reporting
 * NaN) answers `true`: the failure mode of following when we should not is a
 * viewport nudge, and of not following when we should is a live run that
 * silently stops updating, which looks like the agent hung.
 */
export function shouldFollow(g: ScrollGeometry, threshold = STICK_THRESHOLD_PX): boolean {
  return distanceFromBottom(g) <= threshold;
}

/** Read the geometry off a real element. */
function geometryOf(node: HTMLElement): ScrollGeometry {
  return {
    scrollTop: node.scrollTop,
    scrollHeight: node.scrollHeight,
    clientHeight: node.clientHeight,
  };
}

export interface StickToBottomHandle {
  destroy(): void;
}

/**
 * Svelte action: `<div use:stickToBottom>`.
 *
 * Owns a MutationObserver (content growth) and a scroll listener (the operator's
 * intent), and tears both down on destroy. Kept as an action rather than an
 * `$effect` in the component because the lifetime that matters is the NODE's,
 * and an action is handed the node and its teardown together.
 */
export function stickToBottom(node: HTMLElement): StickToBottomHandle {
  let following = true;

  const toBottom = (): void => {
    node.scrollTo({
      top: node.scrollHeight,
      // A smooth scroll re-entering on every streamed token would never
      // arrive; instant is also what reduced-motion asks for.
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  };

  const onScroll = (): void => {
    following = shouldFollow(geometryOf(node));
  };

  // `subtree: true` because turns render nested content (thought rows, tool
  // calls) — a childList watch on the region alone would miss a turn growing
  // in place, which is exactly what a streaming run does.
  const observer = new MutationObserver(() => {
    if (following) toBottom();
  });
  observer.observe(node, { childList: true, subtree: true, characterData: true });
  node.addEventListener('scroll', onScroll, { passive: true });

  return {
    destroy(): void {
      observer.disconnect();
      node.removeEventListener('scroll', onScroll);
    },
  };
}
