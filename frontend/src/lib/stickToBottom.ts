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
 *
 * It took an `enabled` flag until ds-jns, for one caller: historical replay put
 * a static record in this region, where there is no tail to follow and
 * following did measurable harm (the replay opened scrolled to its own bottom,
 * with the "you are reading a past run" banner at -11px, above the top edge of
 * the region). That mode is gone — everything rendering here is a live thread
 * again — so the flag went with it rather than staying on as a parameter no
 * call site passes (ds-1pu).
 */
export function stickToBottom(node: HTMLElement): StickToBottomHandle {
  let following = true;

  // ALWAYS instant, for everyone — not `prefersReducedMotion() ? 'auto' :
  // 'smooth'`, which is what this was and which broke the module's entire
  // purpose in the default case.
  //
  // A smooth scroll animates over ~300ms and fires scroll events THROUGHOUT,
  // carrying intermediate positions. Those reach onScroll below, which reads a
  // distance of hundreds of pixels — the animation's own backlog, not the
  // operator — and disarms. Measured against a 60ms/chunk stream: the distance
  // from the bottom climbed 11 → 131 → 276 → … → 3808px and never recovered,
  // because each re-target restarted an animation that the next chunk
  // immediately outran. Following stopped on the SECOND chunk of every reply,
  // for every user without reduced-motion set — which is nearly all of them.
  //
  // Instant is also simply right here. Smooth is for a one-off navigation the
  // operator asked for; this fires on every token of a streaming reply, where
  // an animation has no time to mean anything and only ever arrives late.
  const toBottom = (): void => {
    node.scrollTo({ top: node.scrollHeight, behavior: 'auto' });
  };

  // Fires for programmatic scrolls too, including toBottom()'s own. That is
  // safe only because the scroll above is instant: by the time the event is
  // dispatched the position has already landed, so the distance it measures is
  // ~0 and the follow stays armed. It is the reason this module cannot animate.
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
