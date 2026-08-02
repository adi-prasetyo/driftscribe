import '@testing-library/jest-dom/vitest';
import { beforeEach, afterEach } from 'vitest';
import { setLocale } from '../../src/lib/i18n';

// i18n: the whole suite asserts rendered ENGLISH (the EN catalog is byte-for-byte
// the app's original inline text), but the app DEFAULTS to Japanese. Pin the
// persisted choice to `en`, and force the shared `locale` store back to `en`
// before AND after every test so a JA-toggle test can never leak into the next.
// (ESM import hoisting means i18n.ts evaluates before the setItem below, so the
// beforeEach — not module-eval — is what guarantees EN at render time.)
try {
  localStorage.setItem('driftscribe.locale', 'en');
} catch {
  /* ignore */
}
beforeEach(() => setLocale('en'));
afterEach(() => setLocale('en'));

// Svelte transitions (slide, fly, fade) use the Web Animations API
// (element.animate), which jsdom does not implement. Stub it out so
// transition:slide / transition:fly in components don't throw during tests.
// The stub dispatches a synchronous 'finish' event so Svelte's transition
// machinery removes elements from the DOM immediately in the test environment.
if (typeof Element !== 'undefined' && !Element.prototype.animate) {
  Element.prototype.animate = function (
    _keyframes: Keyframe[] | PropertyIndexedKeyframes | null,
    _options?: number | KeyframeAnimationOptions,
  ): Animation {
    const listeners: Record<string, EventListenerOrEventListenerObject[]> = {};

    const anim: Animation = {
      cancel: () => {},
      finish: () => {
        const handlers = listeners['finish'] ?? [];
        for (const h of handlers) {
          if (typeof h === 'function') h(new Event('finish'));
          else h.handleEvent(new Event('finish'));
        }
      },
      pause: () => {},
      play: () => {},
      reverse: () => {},
      addEventListener: (type: string, handler: EventListenerOrEventListenerObject) => {
        if (!listeners[type]) listeners[type] = [];
        listeners[type].push(handler);
      },
      removeEventListener: (type: string, handler: EventListenerOrEventListenerObject) => {
        if (listeners[type]) {
          listeners[type] = listeners[type].filter((h) => h !== handler);
        }
      },
      dispatchEvent: () => false,
      currentTime: 0,
      effect: null,
      finished: Promise.resolve({} as Animation),
      id: '',
      oncancel: null,
      onfinish: null,
      onremove: null,
      pending: false,
      playState: 'running' as AnimationPlayState,
      playbackRate: 1,
      ready: Promise.resolve({} as Animation),
      replaceState: 'active' as AnimationReplaceState,
      startTime: 0,
      timeline: null,
      commitStyles: () => {},
      persist: () => {},
      updatePlaybackRate: () => {},
    } as unknown as Animation;

    // Svelte listens for the 'finish' event to remove elements post-transition.
    // Schedule it on the next microtask so Svelte can attach its listener first.
    Promise.resolve().then(() => {
      (anim as { finish: () => void }).finish();
    });

    return anim;
  };
}

// Mock window.matchMedia so motionMs() returns 0 in jsdom — this collapses
// all JS transition durations to zero and prevents real-timer dependencies
// in tests. Svelte's slide/fly duration becomes 0 → transitions are instant.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = (_query: string): MediaQueryList => {
    return {
      matches: _query.includes('reduce'), // prefers-reduced-motion: reduce → true
      media: _query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    } as MediaQueryList;
  };
}

// jsdom does not implement HTMLDialogElement.showModal()/show()/close() (they
// throw "Not implemented"). Modal.svelte drives a native <dialog> via these, so
// stub them to track `open` and fire the native `close` event — enough for unit
// tests of open/close + content; the focus trap and ::backdrop are runtime-only
// (Playwright-verified). Mirrors the animate/matchMedia stubs above.
if (typeof HTMLDialogElement !== 'undefined') {
  const proto = HTMLDialogElement.prototype;
  proto.showModal = function showModal(this: HTMLDialogElement): void {
    this.open = true;
  };
  proto.show = function show(this: HTMLDialogElement): void {
    this.open = true;
  };
  proto.close = function close(this: HTMLDialogElement, returnValue?: string): void {
    if (!this.open) return;
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    this.dispatchEvent(new Event('close'));
  };
}

// jsdom implements window.scrollTo but NOT Element.prototype.scrollTo — calling
// it is a TypeError, not a silent no-op. The chat thread's stickToBottom action
// calls it from a MutationObserver callback, so without this stub every test
// that mounts the chat view fails with an UNHANDLED exception (thrown outside
// any test's call stack, which reports as a suite-level error rather than a
// clean assertion failure). Mirrors the animate/matchMedia/dialog stubs above.
//
// Writing through to scrollTop keeps the stub honest about what the real API
// does; jsdom has no layout, so it clamps to 0 and nothing can read a scroll
// position back. Tests that need to observe a follow spy on scrollTo itself.
if (typeof Element !== 'undefined' && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo(
    this: Element,
    xOrOptions?: number | ScrollToOptions,
    y?: number,
  ): void {
    if (typeof xOrOptions === 'object' && xOrOptions !== null) {
      if (typeof xOrOptions.top === 'number') this.scrollTop = xOrOptions.top;
      if (typeof xOrOptions.left === 'number') this.scrollLeft = xOrOptions.left;
      return;
    }
    if (typeof xOrOptions === 'number') this.scrollLeft = xOrOptions;
    if (typeof y === 'number') this.scrollTop = y;
  };
}
