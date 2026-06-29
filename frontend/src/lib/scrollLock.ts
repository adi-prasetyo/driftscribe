// Ref-counted body scroll lock for modal overlays. A native modal <dialog>
// (showModal) makes the background inert but does NOT stop the body from
// scrolling behind it, so the modal locks `document.body` overflow while open.
//
// The lock is REF-COUNTED rather than a naive save/restore: if two overlays are
// ever open at once (e.g. a search modal over the tour), a naive "restore the
// previous value on close" would unlock while the other is still open. The
// depth counter restores the original overflow only when the last holder
// releases.

let depth = 0;
let previousOverflow = '';

/** Acquire the scroll lock. Idempotent across holders via the depth counter. */
export function lockBodyScroll(): void {
  if (typeof document === 'undefined') return;
  if (depth === 0) {
    previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  depth += 1;
}

/** Release one hold; restores the original overflow only when the last releases. */
export function unlockBodyScroll(): void {
  if (typeof document === 'undefined') return;
  if (depth === 0) return; // defensive: never under-count below zero
  depth -= 1;
  if (depth === 0) {
    document.body.style.overflow = previousOverflow;
  }
}
