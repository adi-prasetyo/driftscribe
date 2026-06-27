// Single source of truth for the operator autonomy dial (GET/POST /autonomy),
// shared by the header AutonomyPill and (via autonomyNoteFor) the CapabilityCard
// note. Ported from the former AutonomyControl component: monotonic-seq stale
// guard + single-flight POST guard. The commit-wins guard (fetch no-ops while
// saving) is carried from pauseStore and matters MORE here because two readers
// can call fetchAutonomy()/retry.
import { writable, type Readable } from 'svelte/store';
import { parseAutonomyDoc, type AutonomyMode } from './autonomy';

export type AutonomyKind = 'loading' | 'loaded' | 'unknown';

export interface AutonomyState {
  kind: AutonomyKind;
  /** Last known mode; defaults to propose_apply (matches the server default) but
   *  is only meaningful when kind === 'loaded'. */
  mode: AutonomyMode;
  reason: string | null;
  actor: string | null;
  updatedAt: string | null;
  readError: boolean;
}

export interface AutonomyStore extends Readable<AutonomyState> {
  fetchAutonomy(): Promise<void>;
  /** POST a new mode; resolves true when state was applied from the response. */
  confirm(mode: AutonomyMode, reason?: string): Promise<boolean>;
}

const INITIAL: AutonomyState = {
  kind: 'loading',
  mode: 'propose_apply',
  reason: null,
  actor: null,
  updatedAt: null,
  readError: false,
};

export function createAutonomyStore(
  call: (path: string, init?: RequestInit) => Promise<Response>,
): AutonomyStore {
  const { subscribe, set } = writable<AutonomyState>({ ...INITIAL });
  // seq shared by fetch + confirm; saving = confirm-only single-flight.
  let seq = 0;
  let saving = false;

  function applyDoc(doc: ReturnType<typeof parseAutonomyDoc>): void {
    if (!doc) {
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    set({
      kind: 'loaded',
      mode: doc.mode,
      reason: doc.reason,
      actor: doc.actor,
      updatedAt: doc.updated_at,
      readError: doc.read_error,
    });
  }

  async function fetchAutonomy(): Promise<void> {
    // commit wins: never invalidate a pending confirm POST with a refresh GET.
    if (saving) return;
    const my = ++seq;
    set({ ...INITIAL, kind: 'loading' });
    let resp: Response;
    try {
      resp = await call('/autonomy');
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
    applyDoc(parseAutonomyDoc(body));
  }

  async function confirm(mode: AutonomyMode, reason?: string): Promise<boolean> {
    if (saving) return false;
    const my = ++seq;
    saving = true;
    const trimmed = (reason ?? '').trim();
    const requestBody: Record<string, unknown> = { mode };
    if (trimmed.length > 0) requestBody.reason = trimmed;

    let resp: Response;
    try {
      resp = await call('/autonomy', {
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
    const doc = parseAutonomyDoc(body);
    if (!doc) {
      saving = false;
      return false;
    }
    applyDoc(doc);
    saving = false;
    return true;
  }

  return { subscribe, fetchAutonomy, confirm };
}

/**
 * The CapabilityCard autonomy-mode note. Pure + relocated here so the card is a
 * dumb renderer and the copy is unit-tested. Silent (null) unless the dial is
 * loaded and below propose_apply (Codex #1: loading/unknown/propose_apply → no
 * note). read_error wins over mode (honest fail-closed copy, never "set to").
 */
export function autonomyNoteFor(state: AutonomyState): string | null {
  if (state.kind !== 'loaded') return null;
  if (state.readError) {
    return 'Autonomy state could not be read. The effective mode is Observe (failing closed) until the dial can be read again.';
  }
  if (state.mode === 'observe') {
    return 'The autonomy dial is currently set to Observe. Tools that open pull requests, issues, or approvals, and anything that merges or applies, are disabled until you raise the dial.';
  }
  if (state.mode === 'propose') {
    return 'The autonomy dial is currently set to Propose. Pull requests and issues are enabled; anything that merges or applies is disabled until you raise the dial.';
  }
  return null; // propose_apply
}
