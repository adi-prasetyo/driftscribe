// Reduced-motion gating for the "settle" animation. Svelte JS transitions are
// not CSS animations, so the base.css `@media (prefers-reduced-motion)` block
// can't disable them — we gate them here at runtime instead.

export function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/** Transition duration, collapsed to 0 when the user prefers reduced motion. */
export function motionMs(ms: number): number {
  return prefersReducedMotion() ? 0 : ms;
}
