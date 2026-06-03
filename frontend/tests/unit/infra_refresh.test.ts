// Unit tests for the InfraDiagram refresh scheduler — the timer/epoch logic the
// adversarial review flagged as the most regression-prone and previously
// untested. Pure + deterministic with fake timers: onFetch is a spy, and the
// scheduler uses the global timer functions which vi.useFakeTimers patches.
//
// Most tests use a huge pollMs so the light poll never interferes with the
// ride-out ladder assertions; the polling test sets it explicitly.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { RefreshScheduler } from '../../src/lib/infra_refresh';

const NO_POLL = 1_000_000; // effectively disables the poll within test windows

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('RefreshScheduler — expand', () => {
  it('a plain expand (no pending apply) fetches exactly once, no ladder', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });
    s.open(0); // epoch 0 === initial lastHandledEpoch → single fetch
    expect(onFetch).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(120_000);
    expect(onFetch).toHaveBeenCalledTimes(1);
  });

  it('an apply observed while collapsed is deferred, then ridden out on expand', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });

    s.onAppliedEpoch(1); // panel closed → deferred, nothing scheduled
    vi.advanceTimersByTime(120_000);
    expect(onFetch).toHaveBeenCalledTimes(0);

    s.open(1); // pending epoch → ride out the 0/10/30/60s ladder
    vi.advanceTimersByTime(0);
    expect(onFetch).toHaveBeenCalledTimes(1); // immediate
    vi.advanceTimersByTime(10_000);
    expect(onFetch).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(20_000); // t=30
    expect(onFetch).toHaveBeenCalledTimes(3);
    vi.advanceTimersByTime(30_000); // t=60 — ladder complete
    expect(onFetch).toHaveBeenCalledTimes(4);
  });
});

describe('RefreshScheduler — appliedEpoch', () => {
  it('an apply observed while collapsed schedules nothing', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });
    s.onAppliedEpoch(1);
    vi.advanceTimersByTime(120_000);
    expect(onFetch).toHaveBeenCalledTimes(0);
  });

  it('an apply observed while OPEN schedules the ride-out ladder', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });
    s.open(0); // single immediate fetch
    onFetch.mockClear();
    s.onAppliedEpoch(1); // open → ladder
    vi.advanceTimersByTime(60_000);
    expect(onFetch).toHaveBeenCalledTimes(4); // 0/10/30/60
  });

  it('the same epoch is handled at most once (no duplicate ladder)', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });
    s.open(3); // epoch 3 → ladder
    vi.advanceTimersByTime(60_000); // drain the ladder
    onFetch.mockClear();
    s.onAppliedEpoch(3); // same epoch → no-op
    vi.advanceTimersByTime(120_000);
    expect(onFetch).toHaveBeenCalledTimes(0);
  });
});

describe('RefreshScheduler — light polling', () => {
  it('polls while open and stops on collapse', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: 45_000 });
    s.open(0); // +1 immediate, starts the poll
    onFetch.mockClear();

    vi.advanceTimersByTime(45_000);
    expect(onFetch).toHaveBeenCalledTimes(1); // poll #1
    vi.advanceTimersByTime(45_000);
    expect(onFetch).toHaveBeenCalledTimes(2); // poll #2

    s.close();
    vi.advanceTimersByTime(200_000);
    expect(onFetch).toHaveBeenCalledTimes(2); // no further polls
  });
});

describe('RefreshScheduler — focus', () => {
  it('focus while open rides out the ladder; while closed is a no-op', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });

    s.onFocus(); // closed → nothing
    vi.advanceTimersByTime(60_000);
    expect(onFetch).toHaveBeenCalledTimes(0);

    s.open(0); // +1 immediate
    onFetch.mockClear();
    s.onFocus(); // open → ladder
    vi.advanceTimersByTime(60_000);
    expect(onFetch).toHaveBeenCalledTimes(4);
  });
});

describe('RefreshScheduler — teardown', () => {
  it('destroy() cancels the poll and any pending ladder', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: 45_000 });
    s.open(0);
    s.onFocus(); // schedule a ladder too
    onFetch.mockClear();
    s.destroy();
    vi.advanceTimersByTime(200_000);
    expect(onFetch).toHaveBeenCalledTimes(0);
  });

  it('a pending ladder timer that fires after close() is inert (open guard)', () => {
    const onFetch = vi.fn();
    const s = new RefreshScheduler({ onFetch, pollMs: NO_POLL });
    s.open(0);
    onFetch.mockClear();
    s.onFocus(); // schedules ladder [0,10,30,60]
    vi.advanceTimersByTime(5_000); // fire the immediate one
    expect(onFetch).toHaveBeenCalledTimes(1);
    s.close(); // clears remaining timers
    vi.advanceTimersByTime(120_000);
    expect(onFetch).toHaveBeenCalledTimes(1); // none of 10/30/60 fired
  });
});
