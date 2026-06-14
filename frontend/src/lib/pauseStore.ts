// pauseStore.ts — single source of truth for the operator pause kill-switch.
//
// One writable holding server-truth pause state, shared by the header
// PausePill and the content PauseBanner so the two can never diverge or
// double-fetch. The monotonic-seq stale guard and the single-flight POST
// guard are ported verbatim from the former PauseControl component — this is
// a kill-switch; a slow/stale response must never silently overwrite fresher
// state, and a double activation must produce exactly one POST.
//
// Transient per-surface UI (popover open, reason text, inline post-error) is
// NOT here — it stays component-local. confirm() returns a boolean so each
// surface decides what to show on failure.
import { writable, type Readable } from 'svelte/store';

export type PauseKind = 'loading' | 'running' | 'paused' | 'unknown';

export interface PauseState {
  kind: PauseKind;
  reason: string | null;
  actor: string | null;
  updatedAt: string | null;
  readError: boolean;
}

export interface PauseStore extends Readable<PauseState> {
  /** Eager GET /pause (call on mount). */
  fetchPause(): Promise<void>;
  /** POST the next paused value; resolves true when state flipped from the response. */
  confirm(nextPaused: boolean, reason?: string): Promise<boolean>;
}

const INITIAL: PauseState = {
  kind: 'loading',
  reason: null,
  actor: null,
  updatedAt: null,
  readError: false,
};

function isValidPauseDoc(body: unknown): body is {
  paused: boolean;
  reason?: string | null;
  actor?: string | null;
  updated_at?: string | null;
  read_error?: boolean;
} {
  if (typeof body !== 'object' || body === null) return false;
  return typeof (body as Record<string, unknown>).paused === 'boolean';
}

export function createPauseStore(
  call: (path: string, init?: RequestInit) => Promise<Response>,
): PauseStore {
  const { subscribe, set } = writable<PauseState>({ ...INITIAL });
  // seq is SHARED by fetchPause + confirm so either can invalidate the other's
  // in-flight result (the original PauseControl contract). saving is the
  // confirm-only single-flight guard.
  let seq = 0;
  let saving = false;

  function applyDoc(doc: {
    paused: boolean;
    reason?: string | null;
    actor?: string | null;
    updated_at?: string | null;
    read_error?: boolean;
  }): void {
    if (doc.paused) {
      set({
        kind: 'paused',
        reason: doc.reason ?? null,
        actor: doc.actor ?? null,
        updatedAt: doc.updated_at ?? null,
        readError: doc.read_error ?? false,
      });
    } else {
      set({ kind: 'running', reason: null, actor: null, updatedAt: null, readError: false });
    }
  }

  async function fetchPause(): Promise<void> {
    // A commit-in-progress wins: a refresh GET must NOT invalidate a pending
    // confirm() POST (shared seq would otherwise drop the POST's response and
    // a stray refresh could stomp a legitimate pause/resume). Skip while saving.
    if (saving) return;
    const my = ++seq;
    set({ ...INITIAL, kind: 'loading' });
    let resp: Response;
    try {
      resp = await call('/pause');
    } catch {
      if (my !== seq) return;
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    if (my !== seq) return;
    if (!resp.ok) {
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      if (my !== seq) return;
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    if (my !== seq) return;
    if (!isValidPauseDoc(body)) {
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    applyDoc(body);
  }

  async function confirm(nextPaused: boolean, reason?: string): Promise<boolean> {
    if (saving) return false;
    const my = ++seq;
    saving = true;
    const trimmed = (reason ?? '').trim();
    const requestBody: Record<string, unknown> = { paused: nextPaused };
    if (nextPaused && trimmed.length > 0) requestBody.reason = trimmed;

    let resp: Response;
    try {
      resp = await call('/pause', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      });
    } catch {
      saving = false;
      return false;
    }
    if (my !== seq) {
      saving = false;
      return false;
    }
    if (!resp.ok) {
      saving = false;
      return false;
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      saving = false;
      return false;
    }
    if (my !== seq) {
      saving = false;
      return false;
    }
    if (!isValidPauseDoc(body)) {
      saving = false;
      return false;
    }
    applyDoc(body);
    saving = false;
    return true;
  }

  return { subscribe, fetchPause, confirm };
}
