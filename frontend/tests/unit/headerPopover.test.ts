import { describe, it, expect, afterEach, vi } from 'vitest';
import { HEADER_POPOVER_EVENT, announceHeaderPopoverOpen } from '../../src/lib/headerPopover';

afterEach(() => vi.restoreAllMocks());

describe('headerPopover bus', () => {
  it('announce dispatches a CustomEvent carrying the source id', () => {
    const seen: string[] = [];
    const handler = (e: Event) => seen.push((e as CustomEvent).detail?.id);
    window.addEventListener(HEADER_POPOVER_EVENT, handler);
    try {
      announceHeaderPopoverOpen('autonomy');
      expect(seen).toEqual(['autonomy']);
    } finally {
      window.removeEventListener(HEADER_POPOVER_EVENT, handler);
    }
  });

  it('announce never throws even if dispatch is unavailable', () => {
    const spy = vi.spyOn(window, 'dispatchEvent').mockImplementation(() => {
      throw new Error('no');
    });
    expect(() => announceHeaderPopoverOpen('pause')).not.toThrow();
    spy.mockRestore();
  });
});
