import '@testing-library/jest-dom/vitest';

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
