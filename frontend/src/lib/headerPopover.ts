// Tiny event bus so the two header-corner popovers (Pause, Autonomy) are
// mutually exclusive. Each pill announces when it opens; the other listens and
// closes itself. Decoupled on purpose: neither pill becomes a controlled
// component, so existing PausePill tests are untouched.

export const HEADER_POPOVER_EVENT = 'ds:header-popover-open';

export type HeaderPopoverId = 'pause' | 'autonomy' | 'notice' | 'tour';

/** Announce that the popover with `id` just opened. Fail-soft. */
export function announceHeaderPopoverOpen(id: HeaderPopoverId): void {
  try {
    window.dispatchEvent(new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id } }));
  } catch {
    /* best-effort: coordination is a nicety, never load-bearing */
  }
}
