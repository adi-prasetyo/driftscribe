# Pause-as-Header-Pill Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move the operator Pause kill-switch out of its always-on content card into a compact status **pill in the header**; the loud content **banner returns only when paused or in an unknown/fail-closed state**, reclaiming vertical space during normal operation.

**Architecture:** A single source of truth — a Svelte `writable` **store factory** `lib/pauseStore.ts` (created once in `App.svelte`, fed the token-aware `call`) — holds server-truth pause state plus the `fetchPause()` / `confirm()` actions, with the existing monotonic-seq stale guard and single-flight POST guard ported **verbatim** (this is a kill-switch; semantics must not regress). Two thin presentational components subscribe to it: `PausePill.svelte` (header; compact status + a popover that hosts the pause confirm) and `PauseBanner.svelte` (content; renders nothing when running, the loud banner when paused/unknown). Transient per-surface UI (popover open, reason text, inline post-error) stays component-local. The old monolithic `PauseControl.svelte` is deleted.

**Tech Stack:** Svelte 5 (runes `$state`/`$derived`/`$props`/`$effect`), `svelte/store` `writable`, `svelte/transition` `slide`, hand-vendored Lucide `Icon`, vitest + @testing-library/svelte (jsdom). Design tokens: `.ds-pill` / `.ds-pill--ok|--warn|--muted`, `--ds-ok`, `--ds-warn-*`, `--ds-danger-ink`, `--ds-radius-pill`.

**Decision (from design thread):** "Pill + banner" — running = clean compact pill, no content card; paused/unknown = pill turns loud AND the full banner returns below the header. Rejected "pill-only" (kill-switch engaged must not be a tiny chip).

---

## Conventions for the executor

- Icons needed already exist in `lib/icons.ts`: `pause`, `play`, `check`, `x`, `alert-triangle`, `chevron-down`. **No new icons** — do not touch `Icon.test.ts`.
- Mirror the existing `call`-prop fake pattern from `PauseControl.test.ts` (a stub recording `(path, init)` tuples, with controllable POST resolution) for store tests.
- Preserve the exact paused-state copy string: `'DriftScribe is paused — no new agent activity will start.'` and the unknown copy substrings `'Pause state unknown'` + `'fails closed'`.
- Run a single test file with: `npm run test -- tests/unit/<file>.test.ts`. Full suite: `npm run test`. Type-check: `npm run check`. Build: `npm run build`.

---

### Task 1: `pauseStore.ts` — the shared source of truth

**Files:**
- Create: `frontend/src/lib/pauseStore.ts`
- Test: `frontend/tests/unit/pauseStore.test.ts`

**Step 1: Write the failing test** (`tests/unit/pauseStore.test.ts`)

```ts
import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { createPauseStore } from '../../src/lib/pauseStore';

type CallRecord = { path: string; init?: RequestInit };
function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
const RUNNING = { paused: false, reason: null, actor: null, updated_at: null, read_error: false };
const PAUSED = { paused: true, reason: 'maint', actor: 'ops@x', updated_at: '2026-06-10T14:02:00Z', read_error: false };

describe('createPauseStore', () => {
  it('fetchPause → running', async () => {
    const s = createPauseStore(async () => res(RUNNING));
    await s.fetchPause();
    expect(get(s).kind).toBe('running');
  });

  it('fetchPause → paused carries reason/actor/updatedAt', async () => {
    const s = createPauseStore(async () => res(PAUSED));
    await s.fetchPause();
    const st = get(s);
    expect(st.kind).toBe('paused');
    expect(st.actor).toBe('ops@x');
    expect(st.reason).toBe('maint');
    expect(st.updatedAt).toBe('2026-06-10T14:02:00Z');
  });

  it('GET throws → unknown', async () => {
    const s = createPauseStore(async () => { throw new Error('net'); });
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('non-ok GET → unknown', async () => {
    const s = createPauseStore(async () => res('err', 500));
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('malformed 200 (paused not boolean) → unknown', async () => {
    const s = createPauseStore(async () => res({ paused: 'yes' }));
    await s.fetchPause();
    expect(get(s).kind).toBe('unknown');
  });

  it('read_error:true paused doc surfaces readError', async () => {
    const s = createPauseStore(async () => res({ paused: true, read_error: true }));
    await s.fetchPause();
    expect(get(s)).toMatchObject({ kind: 'paused', readError: true });
  });

  it('confirm(true, reason) POSTs {paused:true,reason} and flips from response', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(PAUSED) : res(RUNNING);
    });
    await s.fetchPause();
    const ok = await s.confirm(true, '  maint  ');
    expect(ok).toBe(true);
    expect(get(s).kind).toBe('paused');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: true, reason: 'maint' });
  });

  it('confirm(true) with blank reason omits reason key', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(PAUSED) : res(RUNNING);
    });
    await s.fetchPause();
    await s.confirm(true, '   ');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: true });
  });

  it('confirm(false) POSTs {paused:false} and flips to running', async () => {
    const recs: CallRecord[] = [];
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(RUNNING) : res(PAUSED);
    });
    await s.fetchPause();
    const ok = await s.confirm(false);
    expect(ok).toBe(true);
    expect(get(s).kind).toBe('running');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ paused: false });
  });

  it('confirm POST 500 → returns false, state preserved', async () => {
    const s = createPauseStore(async (path, init) =>
      init?.method === 'POST' ? res('err', 500) : res(RUNNING));
    await s.fetchPause();
    const ok = await s.confirm(true, 'x');
    expect(ok).toBe(false);
    expect(get(s).kind).toBe('running'); // not flipped
  });

  it('confirm POST throws → returns false', async () => {
    const s = createPauseStore(async (path, init) => {
      if (init?.method === 'POST') throw new Error('net');
      return res(RUNNING);
    });
    await s.fetchPause();
    expect(await s.confirm(true, 'x')).toBe(false);
  });

  it('single-flight: overlapping confirm() makes exactly one POST', async () => {
    const recs: CallRecord[] = [];
    let resolve!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolve = r));
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(RUNNING);
    });
    await s.fetchPause();
    const a = s.confirm(true, 'x');
    const b = s.confirm(true, 'y'); // must be rejected by saving guard
    expect(recs.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
    resolve(res(PAUSED));
    expect(await a).toBe(true);
    expect(await b).toBe(false);
  });

  it('seq guard: a confirm() started after fetchPause() drops the stale GET result', async () => {
    // Stale GET resolves AFTER a newer confirm has set fresh state.
    let resolveGet!: (r: Response) => void;
    const slowGet = new Promise<Response>((r) => (resolveGet = r));
    let n = 0;
    const s = createPauseStore(async (path, init) => {
      if (init?.method === 'POST') return res(PAUSED);
      n += 1;
      return n === 1 ? slowGet : res(RUNNING);
    });
    const p = s.fetchPause();              // GET #1 in-flight (stale)
    await s.confirm(true, 'x');            // bumps seq, sets paused
    expect(get(s).kind).toBe('paused');
    resolveGet(res(RUNNING));              // stale GET now resolves running
    await p;
    expect(get(s).kind).toBe('paused');    // seq guard dropped it
  });

  it('seq guard: overlapping fetchPause() — newer GET wins, stale older GET is dropped', async () => {
    // Ports old PauseControl test 12 (GET/GET overlap) to the store level.
    let n = 0;
    let resolveStale!: (r: Response) => void;
    let resolveNewer!: (r: Response) => void;
    const stale = new Promise<Response>((r) => (resolveStale = r));
    const newer = new Promise<Response>((r) => (resolveNewer = r));
    const s = createPauseStore(async () => {
      n += 1;
      return n === 1 ? stale : newer;
    });
    const a = s.fetchPause();              // GET #1 (older / stale)
    const b = s.fetchPause();              // GET #2 (newer) — bumps seq
    resolveNewer(res(RUNNING));            // newer resolves first → running
    await b;
    expect(get(s).kind).toBe('running');
    resolveStale(res(PAUSED));             // stale resolves later → must be dropped
    await a;
    expect(get(s).kind).toBe('running');
  });

  it('commit wins: fetchPause() is a no-op while a confirm() POST is in flight', async () => {
    const recs: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const s = createPauseStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(RUNNING);
    });
    await s.fetchPause();                  // GET #1
    const c = s.confirm(true, 'x');        // POST in flight, saving=true
    const getsBefore = recs.filter((r) => !r.init?.method).length;
    await s.fetchPause();                  // must no-op while saving
    expect(recs.filter((r) => !r.init?.method).length).toBe(getsBefore); // no new GET
    resolvePost(res(PAUSED));
    expect(await c).toBe(true);
    expect(get(s).kind).toBe('paused');    // the POST result applied, not stomped
  });
});
```

**Step 2: Run → all fail** (`createPauseStore` undefined).

**Step 3: Implement** (`src/lib/pauseStore.ts`)

```ts
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
  kind: 'loading', reason: null, actor: null, updatedAt: null, readError: false,
};

function isValidPauseDoc(body: unknown): body is {
  paused: boolean; reason?: string | null; actor?: string | null;
  updated_at?: string | null; read_error?: boolean;
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
    paused: boolean; reason?: string | null; actor?: string | null;
    updated_at?: string | null; read_error?: boolean;
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
    if (!resp.ok) { set({ ...INITIAL, kind: 'unknown' }); return; }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      if (my !== seq) return;
      set({ ...INITIAL, kind: 'unknown' });
      return;
    }
    if (my !== seq) return;
    if (!isValidPauseDoc(body)) { set({ ...INITIAL, kind: 'unknown' }); return; }
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
    if (my !== seq) { saving = false; return false; }
    if (!resp.ok) { saving = false; return false; }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      saving = false;
      return false;
    }
    if (my !== seq) { saving = false; return false; }
    if (!isValidPauseDoc(body)) { saving = false; return false; }
    applyDoc(body);
    saving = false;
    return true;
  }

  return { subscribe, fetchPause, confirm };
}
```

**Step 4: Run → all pass.**

**Step 5: Commit** — `feat(pause): add pauseStore single source of truth (ported kill-switch logic)`

---

### Task 2: `PauseBanner.svelte` — content surface (paused / unknown only)

**Files:**
- Create: `frontend/src/components/PauseBanner.svelte`
- Test: `frontend/tests/unit/PauseBanner.test.ts`

**Behavior:**
- `kind === 'running'` or `'loading'` → renders **nothing** (`{#if}` chain has no else).
- `kind === 'unknown'` → amber fail-closed note (copy: contains `Pause state unknown` + `fails closed`) + `pause-retry` button → `pause.fetchPause()`.
- `kind === 'paused'` → the prominent calm amber banner (ported from PauseControl's paused branch): pause icon + `'DriftScribe is paused — no new agent activity will start.'` (exact string in `pause-state`), meta line (`pausedReadError` → fail-closed note; else actor / `<time datetime>` via `fmtUpdatedAt` / reason), and a `pause-toggle` **Resume** button opening an inline confirm row (`slide`, local `confirming`/`saving`/`postError`) → `const ok = await pause.confirm(false); if (ok) { confirming=false } else { postError=true; confirming=false }`. The Cancel button is `disabled={saving}` (Codex #3 — don't allow dismissal while the POST commits and could still set `postError`).

**Reset on external transition (Codex #4):** an `$effect` resets the local resume UI when the store leaves `paused` — `if (st.kind !== 'paused') { confirming = false; postError = false; }` — so a stale confirm row can't survive an external flip and reappear.

**testids:** wrapper `pause-banner` (role=region, aria-label="DriftScribe pause status"); `pause-state`, `pause-toggle`, `pause-confirm`, `pause-cancel`, `pause-error`, `pause-retry`.

**Props:** `let { pause }: { pause: PauseStore } = $props();` then `const st = $derived($pause);`

**Test outline** (port the relevant PauseControl tests + the new "running renders nothing"):
```ts
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import PauseBanner from '../../src/components/PauseBanner.svelte';
import { createPauseStore } from '../../src/lib/pauseStore';
// helper: make a store, await fetchPause with a fake call, then render.
```
- running → `queryByTestId('pause-banner')` is null (nothing rendered).
- paused → `pause-state` textContent === exact string; wrapper contains actor + reason; `<time datetime>` === iso; `pause-toggle` text trimmed === `Resume`.
- unknown → `pause-state`/note contains `Pause state unknown` + `fails closed`; `pause-retry` present; clicking it triggers a second GET (assert via call records).
- read_error paused → contains `pause state could not be read — failing closed`.
- Resume → confirm row → confirm → POST `{paused:false}` (assert via records) → flips; banner disappears (now running → renders nothing).
- Resume confirm POST 500 → `pause-error` shown, still paused, Resume usable again (Codex #5 — state intact + actionable, not just "error appears").
- Resume confirm button `disabled` while POST is in flight (pending-promise fake; Codex #5).
- Cancel → confirm row gone, no POST.

**Step 5: Commit** — `feat(pause): add PauseBanner content surface (paused/unknown)`

---

### Task 3: `PausePill.svelte` — header surface (status + pause popover)

**Files:**
- Create: `frontend/src/components/PausePill.svelte`
- Test: `frontend/tests/unit/PausePill.test.ts`

**Behavior by `kind`:**
- `loading` → muted `ds-pill ds-pill--muted` **span**: small dot + `Checking…` (testid `pause-pill-state`). Not interactive.
- `running` → `ds-pill ds-pill--ok` **button** (`pause-pill-toggle`, `aria-haspopup="dialog"`, `aria-expanded={open}`): green dot + `Active` + a `chevron-down` icon. `aria-label="DriftScribe is active — agent activity allowed within guardrails. Pause DriftScribe."` Clicking toggles a **popover** (`role="dialog"`, testid `pause-popover`).
- `paused` → `ds-pill ds-pill--warn` **span** (`pause-pill-state`): `pause` icon + `Paused`. (Action lives in the banner — not a button.)
- `unknown` → `ds-pill ds-pill--warn` **span** (`pause-pill-state`): `alert-triangle` icon + `State unknown`.

**Popover (running only):** local `$state` `open`, `saving`, `postError`, `reasonInput`; a bound container ref **wrapping BOTH the toggle and the panel** (Codex — if the container wrapped only the panel, a pointerdown on the toggle would close-then-reopen). Contents: hint `Pause all agent activity? New chats, rechecks, and approvals will be refused until you resume.`; optional reason `<input>` (`pause-popover-reason`, maxlength 500); `Confirm pause` (`pause-popover-confirm`, `disabled={saving}`) → `saving=true; const ok = await pause.confirm(true, reasonInput); saving=false; if (ok) { open=false; reasonInput=''; postError=false; } else { postError=true; }`; `Cancel` (`pause-popover-cancel`, `disabled={saving}`) → close+reset; `pause-popover-error` on failure. (`saving` here is the component's view of the in-flight POST — the store also single-flights, so a double-confirm is doubly safe.)
- **Dismiss — gated on `!saving` (Codex #3):** `<svelte:window onkeydown={...}>` Escape closes **only when `!saving`**; `<svelte:window onpointerdown={...}>` closes when `open && !saving` and the event target is outside the bound container. Never dismiss mid-commit, or a failed POST would set `postError` into a closed popover and be lost.
- **Reset on external transition (Codex #4):** an `$effect` — `if (st.kind !== 'running') { open = false; postError = false; }` — so an external flip away from running (e.g. a refresh that finds it paused) tears down popover state instead of leaving `open=true` to reappear later.
- **Focus (best-effort):** an `$effect` that, when `open` becomes true, focuses the reason input; on close returns focus to the toggle **if it still exists** (after a successful pause the running button unmounts — guard the ref, use `tick()` if needed). jsdom focus is untestable beyond "input exists" — keep it best-effort.
- Position: container `position: relative`; panel `position: absolute; top: calc(100% + 6px); right: 0; z-index` above header; `role="dialog"` with `aria-label="Pause DriftScribe"`; `slide` transition `motionMs(160)`.

**Test outline:**
- loading → `pause-pill-state` text contains `Checking`.
- running → `pause-pill-toggle` present, text contains `Active`; `pause-popover` absent initially.
- click toggle → `pause-popover` appears; `pause-popover-reason` present; `aria-expanded` true.
- type reason + `pause-popover-confirm` → `pause.confirm` POSTs `{paused:true,reason}` (assert via call records on the store's fake), popover closes, pill now shows `Paused`.
- confirm POST 500 → `pause-popover-error` shown, popover stays open, toggle usable again (Codex #5).
- confirm button `disabled` while POST in flight (pending-promise fake; Codex #5).
- reason reset after a successful pause then reopen on the next running cycle shows an empty input (Codex #5 — ports old test at PauseControl.test.ts:259; drive via store flipped back to running).
- `pause-popover-cancel` → closes, no POST.
- Escape key → closes; **Escape while `saving` does NOT close** (Codex #3 — pending-promise fake; assert popover still present).
- outside `pointerdown` (dispatch on `document.body`) → closes; **outside pointerdown while `saving` does NOT close** (Codex #3).
- paused → `pause-pill-state` contains `Paused`, NOT a button (`queryByTestId('pause-pill-toggle')` null).
- unknown → `pause-pill-state` contains `State unknown`.

**Step 5: Commit** — `feat(pause): add PausePill header surface with pause popover`

---

### Task 4: Wire into `App.svelte`; delete the old monolith

**Files:**
- Modify: `frontend/src/App.svelte`
- Delete: `frontend/src/components/PauseControl.svelte`
- Delete: `frontend/tests/unit/PauseControl.test.ts`

**Steps:**
1. Imports: remove `PauseControl`; add
   `import PausePill from './components/PausePill.svelte';`
   `import PauseBanner from './components/PauseBanner.svelte';`
   `import { createPauseStore } from './lib/pauseStore';`
2. After `call` is defined (it is a hoisted `function`, so placement is flexible — keep it readable, just below the `call` definition): `const pause = createPauseStore(call);`
3. In `onMount`, add `void pause.fetchPause();` next to `void loadDecisions();`.
4. Header — inside `.app-header__actions`, **before** the Tour button:
   ```svelte
   <PausePill {pause} />
   ```
5. Content — in the `data-tour="controls"` wrapper, replace `<PauseControl {call} />` with `<PauseBanner {pause} />` (keep `<AutonomyControl {call} />` after it, so the banner renders above the dial when present).
6. `npm run check` (svelte-check) → 0 errors; `npm run test` → green; `npm run build` → clean.

**Note for executor:** the `.tour-target > :global(* + *)` margin rule still applies — when PauseBanner renders nothing (running), the wrapper has a single child (the dial) and no stray margin; when it renders (paused/unknown), it is the first child and the dial gets the inter-component margin. No CSS change required.

**Step 5: Commit** — `refactor(pause): wire PausePill+PauseBanner into App, drop PauseControl`

---

### Task 5: Tour copy — relocate the Pause reference

**Files:**
- Modify: `frontend/src/lib/tour.ts` (`CONTROLS_LINE`)
- Modify: `frontend/tests/unit/tour.test.ts`

**Change:** in `CONTROLS_LINE`, change the closing clause
`'… and the Pause button suspends all agent activity in one click.'`
→ `'… and the Pause control in the top bar suspends all agent activity in one click.'`

**Constraints (existing pins must stay green):** still contains `'infrastructure edits pass your explicit approval gate'`, `'routine dependency updates'`, `'Pause'`; still must NOT contain `'safety'` (lowercase check). Add one assertion:
```ts
expect(CONTROLS_LINE).toContain('top bar');
```

**Note:** the `controls` tour step keeps spotlighting `data-tour="controls"` (now the dial; banner empty when running) — accurate, since the copy now points at the header for Pause. A dedicated pill-spotlight beat is a possible future enhancement, not in scope.

**Step 5: Commit** — `feat(tour): point the Pause reference at the header control`

---

### Task 6: Integration verify, review, ship

**Steps:**
1. `npm run test` (full suite green; net test delta: −12 PauseControl + new pauseStore/PauseBanner/PausePill suites) ; `npm run check` (0/0); `npm run build` (clean, note new bundle hash).
2. **Local visual verify** (rig from memory): `npm run build`; `DRY_RUN=true USE_ADK=false DRIFTSCRIBE_TOKEN=local-dev-token uv run uvicorn agent.main:app --host 127.0.0.1 --port 8765` (restart after each rebuild — manifest is cached at startup); seed token via the modal; confirm:
   - running: header shows a green `● Active` pill, no content pause card; clicking it opens the popover; confirm pauses.
   - paused: header pill turns amber `⏸ Paused`; the loud banner appears below the header with actor/time/reason + Resume.
   - unknown: (force by pointing at a bad path or 500) amber `⚠ State unknown` pill + fail-closed banner + Retry.
   - Playwright smoke suite (run with NO server on :8765): 10/10.
3. **Codex** `mcp__codex__codex-reply` on the existing thread (or a fresh `mcp__codex__codex`) — completed-work review against this plan; fold must-fix, push back on the rest on merits.
4. Open PR; CI green (frontend lint/test/build; plan-builder correctly skipped on a frontend-only change).
5. Per deploy-autonomy: squash-merge to main, rebake the coordinator (Cloud Build `driftscribe-agent:<sha>`), then **`update-traffic --to-revisions=<new>=100`** (traffic is pinned). Live-verify the served bundle contains `pause-pill` markers; confirm content-hash matches local.
6. Update memory `ui_polish_icons_motion_depth.md`: new rev pointer + a PR deploy-history bullet.

---

## Out of scope (deferred, per "Just A for now")
- Moving the composer to the top of the content column.
- A collapsible right-hand tab.
- A dedicated tour spotlight beat on the header pill (copy update covers the relocation honestly for now).
