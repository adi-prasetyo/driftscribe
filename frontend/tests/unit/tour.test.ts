import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  TOUR_DONE_KEY,
  tourDone,
  markTourDone,
  shouldOfferTour,
} from '../../src/lib/tour';

describe('tour done flag (localStorage)', () => {
  beforeEach(() => window.localStorage.clear());

  it('tourDone is false on a fresh profile', () => {
    expect(tourDone()).toBe(false);
  });

  it('markTourDone persists and tourDone reads it back', () => {
    markTourDone();
    expect(window.localStorage.getItem(TOUR_DONE_KEY)).toBe('1');
    expect(tourDone()).toBe(true);
  });

  it('swallows storage failures (strict privacy modes)', () => {
    const get = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    const set = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('denied');
      });
    try {
      expect(tourDone()).toBe(false);
      expect(() => markTourDone()).not.toThrow();
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });
});

describe('shouldOfferTour', () => {
  it('offers on a clean first visit', () => {
    expect(shouldOfferTour('', false)).toBe(true);
    expect(shouldOfferTour('?other=1', false)).toBe(true);
  });

  it('never offers once done', () => {
    expect(shouldOfferTour('', true)).toBe(false);
  });

  it('suppressed when the operator arrived with intent (?ask_pr / ?preview_pr)', () => {
    expect(shouldOfferTour('?ask_pr=102', false)).toBe(false);
    expect(shouldOfferTour('?preview_pr=7', false)).toBe(false);
  });
});
