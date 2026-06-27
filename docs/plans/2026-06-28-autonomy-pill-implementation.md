# Autonomy header pill — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Collapse the always-on autonomy dial card (`AutonomyControl.svelte`) at the top of the chat area into a compact `Mode: …` pill in the header `app-header__actions` cluster (before `PausePill`) whose click drops an anchored popover hosting the full 3-segment dial.

**Architecture:** Frontend-only; the `GET/POST /autonomy` contract is unchanged. Introduce a shared `lib/autonomyStore.ts` (mirroring `lib/pauseStore.ts`) as the single source of `/autonomy` truth, consumed by the new `AutonomyPill` and feeding a derived note into `CapabilityCard` (which loses its own private `/autonomy` fetch). `AutonomyControl.svelte` is deleted once its dial moves into the popover. Header popover mutual-exclusion (Pause vs Autonomy) is coordinated via a tiny window `CustomEvent` bus so the two siblings can never both be open.

**Tech Stack:** Svelte 5 (runes: `$state`/`$derived`/`$effect`/`$props`/`$bindable`), `svelte/transition` (`slide`), `svelte/store` (`writable`), vitest + `@testing-library/svelte`, `svelte-check`, Vite. Deploy is a coordinator image rebake + traffic shift (SPA is baked in).

**Design doc:** `docs/plans/2026-06-28-autonomy-pill-header-design.md` (all 8 Codex points already folded there).

---

## Conventions for the executing engineer (read once)

- **Worktree:** all paths below are relative to the repo root of the worktree
  `/home/adi/driftscribe/.worktrees/autonomy-header-pill`. The frontend lives under
  `frontend/`. Run all `npx …` commands from `frontend/`.
- **Run a single test file:** `cd frontend && npx vitest run tests/unit/<file>.test.ts`.
  Full suite: `npx vitest run` (baseline at plan time: **41 files, 819 tests, 0 failures**).
- **TDD:** for each task, write the failing test first, run it red, implement, run it
  green, then commit. Keep the whole suite green at every commit boundary.
- **Commit style:** match the repo (`feat(ui): …`, `refactor(ui): …`, `test(ui): …`).
  End commit messages with the `Co-Authored-By:` trailer the harness requires.
- **Testid parallelism:** the new pill mirrors `PausePill`'s testids
  (`pause-pill-toggle` / `pause-pill-state` / `pause-popover`) →
  `autonomy-pill-toggle` / `autonomy-pill-state` / `autonomy-popover`. Inside the
  popover, REUSE the existing dial testids verbatim (`autonomy-mode-{mode}`,
  `autonomy-confirm`, `autonomy-cancel`, `autonomy-reason`, `autonomy-current`,
  `autonomy-current-mode`, `autonomy-explainer-toggle`, `autonomy-explainer-body`,
  `autonomy-read-error`, `autonomy-error`) so the migrated dial tests need only an
  "open the popover first" preamble.
- **Honesty boundary (Codex #4) — DO NOT regress:** at every mode, infrastructure
  applies and rollbacks require explicit approval. Only at Propose + Apply may
  routine dependency updates run end-to-end, and only Anchor self-triggers. Copy
  must never imply "change live infra unattended."

---

## Task 1: header-popover coordination bus (`lib/headerPopover.ts`)

A 2-popover corner (Pause + Autonomy) needs "at most one open." Pointer-outside
already closes the other via each pill's window `pointerdown` handler, but **keyboard**
activation of one toggle does not fire a pointerdown, so both can end up open
(Codex #7). A decoupled window `CustomEvent` fixes it without making either pill a
controlled component (which would churn all 15 `PausePill` tests).

**Files:**
- Create: `frontend/src/lib/headerPopover.ts`
- Test: `frontend/tests/unit/headerPopover.test.ts`

**Step 1: Write the failing test**

```ts
// frontend/tests/unit/headerPopover.test.ts
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
```

**Step 2: Run red** — `npx vitest run tests/unit/headerPopover.test.ts` → FAIL (module missing).

**Step 3: Implement**

```ts
// frontend/src/lib/headerPopover.ts
// Tiny event bus so the two header-corner popovers (Pause, Autonomy) are
// mutually exclusive. Each pill announces when it opens; the other listens and
// closes itself. Decoupled on purpose: neither pill becomes a controlled
// component, so existing PausePill tests are untouched.

export const HEADER_POPOVER_EVENT = 'ds:header-popover-open';

export type HeaderPopoverId = 'pause' | 'autonomy';

/** Announce that the popover with `id` just opened. Fail-soft. */
export function announceHeaderPopoverOpen(id: HeaderPopoverId): void {
  try {
    window.dispatchEvent(new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id } }));
  } catch {
    /* best-effort: coordination is a nicety, never load-bearing */
  }
}
```

**Step 4: Run green.** **Step 5: Commit** (`feat(ui): header-popover coordination bus`).

---

## Task 2: honest Propose + Apply copy (`lib/autonomy.ts`)

`MODE_BLURBS.propose_apply` today reads *"Propose changes and apply them after your
approval (current default)."* — the honest *infra* story but it undersells the
dependency-update autonomy and (read in the pill caption) could imply infra applies
run unattended. Reconcile it to state both facts (Codex #4). The blurb is shown in
the popover caption AND embedded in the arm-to-confirm hint, so fixing the one
constant fixes both surfaces.

**Files:**
- Modify: `frontend/src/lib/autonomy.ts:18`
- Modify (keep green): `frontend/tests/unit/AutonomyControl.test.ts:146` (the one
  assertion on the old blurb — update the literal; the whole file is deleted in
  Task 9, but it must stay green until then)
- Test (new pin): added in Task 3's `autonomyStore.test.ts` copy block — see there.

**Step 1: Edit the constant**

Replace line 18:

```ts
  propose_apply: 'Propose changes and apply them after your approval (current default).',
```

with:

```ts
  propose_apply:
    'Routine dependency updates can run end-to-end. Infrastructure changes still ' +
    'wait for your approval (current default).',
```

**Step 2: Keep the doomed AutonomyControl test green** — in
`tests/unit/AutonomyControl.test.ts`, test 3 asserts
`control.textContent).toContain('Propose changes and apply them')`. Change that
literal to `toContain('Routine dependency updates can run end-to-end')`.

**Step 3: Run** `npx vitest run tests/unit/AutonomyControl.test.ts` → still green.

**Step 4: Commit** (`refactor(ui): honest Propose + Apply blurb (deps end-to-end, infra still gated)`).

> Note: `AUTONOMY_EXPLAINER_BODY/HEADING` are unchanged (still accurate — the
> segmented control still reads as a "dial" inside the popover). Only the
> propose_apply blurb and the tour line (Task 7) change.

---

## Task 3: shared `autonomyStore` + note selector (`lib/autonomyStore.ts`)

Mirror `pauseStore` exactly (shared monotonic `seq`, single-flight `saving`,
**commit-wins guard** — `fetchAutonomy()` no-ops while `saving`, Codex #2). Also
host `autonomyNoteFor(state)`: the CapabilityCard note copy, relocated here as a
pure selector so it is unit-testable and the card becomes a dumb renderer. The
selector returns `null` for `loading`/`unknown`/`propose_apply` (Codex #1: card
stays silent unless `loaded` and below `propose_apply`).

**Files:**
- Create: `frontend/src/lib/autonomyStore.ts`
- Test: `frontend/tests/unit/autonomyStore.test.ts`

**Step 1: Write the failing test** (model on `pauseStore.test.ts`)

```ts
// frontend/tests/unit/autonomyStore.test.ts
import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import { createAutonomyStore, autonomyNoteFor } from '../../src/lib/autonomyStore';
import { MODE_BLURBS } from '../../src/lib/autonomy';

type CallRecord = { path: string; init?: RequestInit };
function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}
const OBSERVE = { mode: 'observe', reason: 'r', actor: 'ops@x', updated_at: '2026-06-11T10:00:00Z', read_error: false };
const PA = { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false };

describe('createAutonomyStore', () => {
  it('fetch → loaded carries mode/reason/actor/updatedAt', async () => {
    const s = createAutonomyStore(async () => res(OBSERVE));
    await s.fetchAutonomy();
    expect(get(s)).toMatchObject({
      kind: 'loaded', mode: 'observe', reason: 'r', actor: 'ops@x',
      updatedAt: '2026-06-11T10:00:00Z', readError: false,
    });
  });

  it('GET throws → unknown', async () => {
    const s = createAutonomyStore(async () => { throw new Error('net'); });
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('non-ok GET → unknown', async () => {
    const s = createAutonomyStore(async () => res('e', 500));
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('malformed 200 (mode invalid) → unknown', async () => {
    const s = createAutonomyStore(async () => res({ mode: 'yolo' }));
    await s.fetchAutonomy();
    expect(get(s).kind).toBe('unknown');
  });

  it('read_error:true surfaces readError on the loaded state', async () => {
    const s = createAutonomyStore(async () => res({ mode: 'observe', read_error: true }));
    await s.fetchAutonomy();
    expect(get(s)).toMatchObject({ kind: 'loaded', mode: 'observe', readError: true });
  });

  it('confirm POSTs {mode,reason} and applies the response', async () => {
    const recs: CallRecord[] = [];
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res({ ...OBSERVE, mode: 'observe' }) : res(PA);
    });
    await s.fetchAutonomy();
    const ok = await s.confirm('observe', '  going read-only  ');
    expect(ok).toBe(true);
    expect(get(s).mode).toBe('observe');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ mode: 'observe', reason: 'going read-only' });
  });

  it('confirm with blank reason omits reason key', async () => {
    const recs: CallRecord[] = [];
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? res(OBSERVE) : res(PA);
    });
    await s.fetchAutonomy();
    await s.confirm('observe', '   ');
    const post = recs.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(post.init!.body as string)).toEqual({ mode: 'observe' });
  });

  it('confirm POST 500 → false, state preserved', async () => {
    const s = createAutonomyStore(async (path, init) =>
      init?.method === 'POST' ? res('e', 500) : res(PA));
    await s.fetchAutonomy();
    expect(await s.confirm('observe')).toBe(false);
    expect(get(s).mode).toBe('propose_apply');
  });

  it('single-flight: overlapping confirm makes exactly one POST', async () => {
    const recs: CallRecord[] = [];
    let resolve!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolve = r));
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(PA);
    });
    await s.fetchAutonomy();
    const a = s.confirm('observe', 'x');
    const b = s.confirm('propose', 'y');
    expect(recs.filter((r) => r.init?.method === 'POST')).toHaveLength(1);
    resolve(res(OBSERVE));
    expect(await a).toBe(true);
    expect(await b).toBe(false);
  });

  it('seq guard: a stale GET resolving after a confirm is dropped', async () => {
    let resolveGet!: (r: Response) => void;
    const slow = new Promise<Response>((r) => (resolveGet = r));
    let n = 0;
    const s = createAutonomyStore(async (path, init) => {
      if (init?.method === 'POST') return res(OBSERVE);
      n += 1;
      return n === 1 ? slow : res(PA);
    });
    const p = s.fetchAutonomy();
    await s.confirm('observe', 'x');
    expect(get(s).mode).toBe('observe');
    resolveGet(res(PA));
    await p;
    expect(get(s).mode).toBe('observe'); // stale GET dropped
  });

  it('commit wins: fetchAutonomy is a no-op while a confirm POST is in flight', async () => {
    const recs: CallRecord[] = [];
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const s = createAutonomyStore(async (path, init) => {
      recs.push({ path, init });
      return init?.method === 'POST' ? pending : res(PA);
    });
    await s.fetchAutonomy();
    const c = s.confirm('observe', 'x');
    const getsBefore = recs.filter((r) => !r.init?.method).length;
    await s.fetchAutonomy(); // must no-op
    expect(recs.filter((r) => !r.init?.method).length).toBe(getsBefore);
    resolvePost(res(OBSERVE));
    expect(await c).toBe(true);
    expect(get(s).mode).toBe('observe');
  });
});

describe('autonomyNoteFor', () => {
  const base = { reason: null, actor: null, updatedAt: null };
  it('loading → null (silent)', () => {
    expect(autonomyNoteFor({ kind: 'loading', mode: 'observe', readError: false, ...base })).toBeNull();
  });
  it('unknown → null (silent, Codex #1)', () => {
    expect(autonomyNoteFor({ kind: 'unknown', mode: 'observe', readError: false, ...base })).toBeNull();
  });
  it('loaded + propose_apply → null', () => {
    expect(autonomyNoteFor({ kind: 'loaded', mode: 'propose_apply', readError: false, ...base })).toBeNull();
  });
  it('loaded + observe → observe note (no "write-capable tools are disabled")', () => {
    const note = autonomyNoteFor({ kind: 'loaded', mode: 'observe', readError: false, ...base });
    expect(note).toBe(
      'The autonomy dial is currently set to Observe. Tools that open pull requests, issues, or approvals, and anything that merges or applies, are disabled until you raise the dial.',
    );
    expect(note).not.toContain('write-capable tools are disabled');
  });
  it('loaded + propose → propose note', () => {
    expect(autonomyNoteFor({ kind: 'loaded', mode: 'propose', readError: false, ...base })).toBe(
      'The autonomy dial is currently set to Propose. Pull requests and issues are enabled; anything that merges or applies is disabled until you raise the dial.',
    );
  });
  it('loaded + read_error → fail-closed note (never says "set to")', () => {
    const note = autonomyNoteFor({ kind: 'loaded', mode: 'observe', readError: true, ...base });
    expect(note).toBe(
      'Autonomy state could not be read. The effective mode is Observe (failing closed) until the dial can be read again.',
    );
    expect(note).not.toContain('currently set to');
  });
});

describe('Propose + Apply blurb honesty (Codex #4)', () => {
  it('names dependency autonomy AND keeps infra gated', () => {
    expect(MODE_BLURBS.propose_apply.toLowerCase()).toContain('dependency');
    expect(MODE_BLURBS.propose_apply.toLowerCase()).toContain('approval');
    expect(MODE_BLURBS.propose_apply.toLowerCase()).toContain('infrastructure');
  });
});
```

**Step 2: Run red** — module missing.

**Step 3: Implement**

```ts
// frontend/src/lib/autonomyStore.ts
// Single source of truth for the operator autonomy dial (GET/POST /autonomy),
// shared by the header AutonomyPill and (via autonomyNoteFor) the CapabilityCard
// note. Ported from the former AutonomyControl component: monotonic-seq stale
// guard + single-flight POST guard. The commit-wins guard (fetch no-ops while
// saving) is carried from pauseStore and matters MORE here because two readers
// can call fetchAutonomy()/retry.
import { writable, type Readable } from 'svelte/store';
import {
  AUTONOMY_MODES,
  MODE_LABELS,
  parseAutonomyDoc,
  type AutonomyMode,
} from './autonomy';

export type AutonomyKind = 'loading' | 'loaded' | 'unknown';

export interface AutonomyState {
  kind: AutonomyKind;
  /** Last known mode; defaults to propose_apply (matches server default) but is
   *  only meaningful when kind === 'loaded'. */
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
```

> `MODE_LABELS`/`AUTONOMY_MODES` imports are used by the pill, not the store — keep
> only what the store references to avoid `svelte-check` unused warnings. If
> `MODE_LABELS`/`AUTONOMY_MODES` end up unused in this file, drop them from the import.

**Step 4: Run green.** **Step 5: Commit** (`feat(ui): shared autonomyStore + autonomyNoteFor selector`).

---

## Task 4: `AutonomyPill.svelte` (header surface + popover)

The big one. Mirror `PausePill` structurally; the popover body is the old
`AutonomyControl` loaded branch (dial + caption + reason/confirm + explainer +
meta), with the measurement effect re-gated for a popover.

**Files:**
- Create: `frontend/src/components/AutonomyPill.svelte`
- Test: `frontend/tests/unit/AutonomyPill.test.ts`

### Behavior spec

- **Pill states** (from `$autonomy`):
  - `loading` → `<span data-testid="autonomy-pill-state">` muted "Mode · checking…",
    not interactive, no popover.
  - `unknown` → `<button data-testid="autonomy-pill-retry">` warn pill with a
    **visible** "State unknown · retry" label (Codex plan-review #6: `title` alone
    isn't discoverable) + `aria-label="Autonomy state could not be read — retry."`;
    click calls `autonomy.fetchAutonomy()` **directly** (no popover — simplest
    recovery; deviates from the design doc's "retry inside popover" for one-click
    UX. Codex plan-review confirmed this is fine.).
  - `loaded` → `<button data-testid="autonomy-pill-toggle">` with `aria-haspopup="dialog"`,
    `aria-expanded`. Label = mode icon + `MODE_LABELS[mode]` + chevron, e.g.
    "⚡ Propose + Apply ▾". When `readError` is true, the pill carries a warn tint
    and the label becomes "Observe · fail-closed" (Codex #5: degraded indicator,
    NOT per-mode alarm coloring). Click opens the popover.
- **Popover** (`data-testid="autonomy-popover"`, `role="dialog"`), anchored
  `top:calc(100%+6px); right:0` like `pause-popover`, `transition:slide`:
  1. 3-segment dial (`autonomy-mode-{mode}`, sliding `--ready`-gated pill, armed state).
  2. current-mode caption (`autonomy-current` / `autonomy-current-mode`) + blurb.
  3. arm-then-confirm: clicking a non-active segment arms it; reason input
     (`autonomy-reason`) + `autonomy-confirm` / `autonomy-cancel`.
  4. collapsed explainer (`autonomy-explainer-toggle` / `-body`).
  5. meta line (set-by / time / reason) OR the `autonomy-read-error` warning.
  6. `autonomy-error` on POST failure.
- **Guards:** dismiss (Esc / outside pointerdown) gated on `!saving`; reset
  armed/pending/reason/postError + the explainer + measurement flags on close;
  auto-close on successful confirm; **focus the active segment** on open, back to
  the toggle on close; announce + listen on the header-popover bus.
- **Focus (Codex plan-review #2):** on open, after `tick()`, focus the *active*
  segment via its bound ref `segmentEls[AUTONOMY_MODES.indexOf(st.mode)]` (type-safe,
  no `querySelector`). On a normal close, return focus to the toggle.
- **Foreign-close must NOT steal focus (Codex plan-review #1):** `closePopover`
  takes a `returnFocus = true` param; the header-bus listener calls
  `closePopover(false)` so that closing THIS popover because the OTHER opened does
  not yank focus back to this (now-closed) toggle and away from the pill the user
  just opened.
- **Measurement (Codex #3 + plan-review #3):** effect keyed on `open`, `kind`,
  `mode`; reset `pillMeasured`/`pillReady` on every open AND close; the effect
  cleanup **cancels any pending `pillRafId`** (the effect re-runs on a mode change
  while open, like AutonomyControl:163) AND disconnects the per-open
  `ResizeObserver` (guard `typeof ResizeObserver !== 'undefined'`).
- **Mutual-exclusion invariant (Codex plan-review #4):** at most one header
  popover is open, EXCEPT during a protected commit — a pill mid-POST (`saving`)
  refuses bus-close so a failed save's `postError` is never lost into a torn-down
  panel. Both-open is therefore possible only transiently while one is saving.

**Step 1: Write the failing test** (model on `PausePill.test.ts` + the migrated
`AutonomyControl` dial assertions). Key cases — keep them small:

```ts
// frontend/tests/unit/AutonomyPill.test.ts
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import AutonomyPill from '../../src/components/AutonomyPill.svelte';
import { createAutonomyStore } from '../../src/lib/autonomyStore';
import { HEADER_POPOVER_EVENT } from '../../src/lib/headerPopover';
import { AUTONOMY_EXPLAINER_HEADING } from '../../src/lib/autonomy';

afterEach(cleanup);

type CallRecord = { path: string; init?: RequestInit };
function res(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}
const PA = { mode: 'propose_apply', reason: null, actor: null, updated_at: null, read_error: false };
const OBSERVE = { mode: 'observe', reason: 'r', actor: 'ops@x', updated_at: '2026-06-11T10:00:00Z', read_error: false };

async function mount(
  getResp: Response | (() => Response | Promise<Response>),
  postResp?: Response | Promise<Response>,
) {
  const records: CallRecord[] = [];
  const call = async (path: string, init?: RequestInit): Promise<Response> => {
    records.push({ path, init });
    if (init?.method === 'POST') return postResp ?? res(OBSERVE);
    return typeof getResp === 'function' ? getResp() : getResp;
  };
  const store = createAutonomyStore(call);
  await store.fetchAutonomy();
  const view = render(AutonomyPill, { props: { autonomy: store } });
  return { ...view, records, store };
}

describe('AutonomyPill', () => {
  it('loading → muted state, not interactive', () => {
    const store = createAutonomyStore(async () => res(PA)); // never fetched
    const { getByTestId, queryByTestId } = render(AutonomyPill, { props: { autonomy: store } });
    expect(getByTestId('autonomy-pill-state').textContent?.toLowerCase()).toContain('checking');
    expect(queryByTestId('autonomy-pill-toggle')).toBeNull();
  });

  it('loaded → pill names the mode; popover closed initially', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    const toggle = getByTestId('autonomy-pill-toggle');
    expect(toggle.textContent).toContain('Propose + Apply');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(queryByTestId('autonomy-popover')).toBeNull();
  });

  it('click opens the popover with the three segments', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    expect(getByTestId('autonomy-mode-observe')).toBeTruthy();
    expect(getByTestId('autonomy-mode-propose')).toBeTruthy();
    expect(getByTestId('autonomy-mode-propose_apply')).toBeTruthy();
    expect(getByTestId('autonomy-pill-toggle').getAttribute('aria-expanded')).toBe('true');
  });

  it('arm + confirm POSTs {mode,reason}; popover closes; pill shows new mode', async () => {
    const post = { mode: 'observe', reason: 'going read-only', actor: 'ops@x', updated_at: null, read_error: false };
    const { getByTestId, queryByTestId, records } = await mount(res(PA), res(post));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.input(getByTestId('autonomy-reason'), { target: { value: 'going read-only' } });
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => {
      expect(queryByTestId('autonomy-popover')).toBeNull();
      expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Observe');
    });
    const p = records.find((r) => r.init?.method === 'POST')!;
    expect(JSON.parse(p.init!.body as string)).toEqual({ mode: 'observe', reason: 'going read-only' });
  });

  it('POST 500 → autonomy-error shown; popover stays open; mode unchanged', async () => {
    const { getByTestId } = await mount(res(PA), res('e', 500));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => expect(getByTestId('autonomy-error')).toBeTruthy());
    expect(getByTestId('autonomy-popover')).toBeTruthy();
    expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Propose + Apply');
  });

  it('Escape closes when not saving; does NOT close mid-POST', async () => {
    let resolvePost!: (r: Response) => void;
    const pending = new Promise<Response>((r) => (resolvePost = r));
    const { getByTestId, queryByTestId } = await mount(res(PA), pending);
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-mode-observe')).toBeTruthy());
    // mid-POST: Escape blocked
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-confirm')).toBeTruthy());
    await fireEvent.click(getByTestId('autonomy-confirm'));
    await waitFor(() => expect((getByTestId('autonomy-confirm') as HTMLButtonElement).disabled).toBe(true));
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(getByTestId('autonomy-popover')).toBeTruthy();
    resolvePost(res(OBSERVE));
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull()); // auto-close on success
  });

  it('outside pointerdown closes the popover', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    await fireEvent.pointerDown(document.body);
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
  });

  it('opening announces on the header bus; a foreign open closes this popover', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    // announce-on-open
    const seen: string[] = [];
    const h = (e: Event) => seen.push((e as CustomEvent).detail?.id);
    window.addEventListener(HEADER_POPOVER_EVENT, h);
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    expect(seen).toContain('autonomy');
    // foreign open (pause) closes us
    window.dispatchEvent(new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id: 'pause' } }));
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
    window.removeEventListener(HEADER_POPOVER_EVENT, h);
  });

  it('a foreign open closes us WITHOUT returning focus to our toggle (plan-review #1)', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    const toggle = getByTestId('autonomy-pill-toggle');
    await fireEvent.click(toggle);
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    window.dispatchEvent(new CustomEvent(HEADER_POPOVER_EVENT, { detail: { id: 'pause' } }));
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
    // focus must NOT have been forced back onto our toggle
    expect(document.activeElement).not.toBe(toggle);
  });

  it('opening focuses the active segment (plan-review #2)', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-popover')).toBeTruthy());
    await waitFor(() => expect(document.activeElement).toBe(getByTestId('autonomy-mode-propose_apply')));
  });

  it('unknown → retry button refetches', async () => {
    let n = 0;
    const store = createAutonomyStore(async () => { n += 1; return n === 1 ? res('e', 500) : res(PA); });
    await store.fetchAutonomy(); // → unknown
    const { getByTestId } = render(AutonomyPill, { props: { autonomy: store } });
    await fireEvent.click(getByTestId('autonomy-pill-retry'));
    await waitFor(() => expect(getByTestId('autonomy-pill-toggle').textContent).toContain('Propose + Apply'));
  });

  it('read_error loaded → pill shows fail-closed; popover shows the warning', async () => {
    const { getByTestId } = await mount(res({ mode: 'observe', read_error: true }));
    expect(getByTestId('autonomy-pill-toggle').textContent?.toLowerCase()).toContain('fail-closed');
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await waitFor(() => expect(getByTestId('autonomy-read-error')).toBeTruthy());
  });

  it('current-mode caption stays committed while a switch is armed', async () => {
    const { getByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-cancel')).toBeTruthy());
    expect(getByTestId('autonomy-current-mode').textContent?.trim()).toBe('Propose + Apply');
  });

  it('explainer is collapsed by default and toggles open', async () => {
    const { getByTestId, queryByTestId } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    const toggle = await waitFor(() => getByTestId('autonomy-explainer-toggle'));
    expect(toggle.textContent).toContain(AUTONOMY_EXPLAINER_HEADING);
    expect(queryByTestId('autonomy-explainer-body')).toBeNull();
    await fireEvent.click(toggle);
    await waitFor(() => expect(getByTestId('autonomy-explainer-body')).toBeTruthy());
  });

  it('cancel closes the arm row with no POST; reopen clears reason', async () => {
    const { getByTestId, queryByTestId, records } = await mount(res(PA));
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-reason')).toBeTruthy());
    await fireEvent.input(getByTestId('autonomy-reason'), { target: { value: 'abc' } });
    await fireEvent.click(getByTestId('autonomy-cancel'));
    await waitFor(() => expect(queryByTestId('autonomy-confirm')).toBeNull());
    expect(records.filter((r) => r.init?.method === 'POST')).toHaveLength(0);
    // close + reopen → armed row + reason cleared
    await fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(queryByTestId('autonomy-popover')).toBeNull());
    await fireEvent.click(getByTestId('autonomy-pill-toggle'));
    await fireEvent.click(getByTestId('autonomy-mode-observe'));
    await waitFor(() => expect(getByTestId('autonomy-reason')).toBeTruthy());
    expect((getByTestId('autonomy-reason') as HTMLInputElement).value).toBe('');
  });
});
```

**Step 2: Run red.**

**Step 3: Implement `AutonomyPill.svelte`.** Skeleton (fill the popover body by
porting markup from `AutonomyControl.svelte:349-481` — the loaded branch — and its
`<style>`; swap `call`-driven fetch/confirm for the store, and `currentMode` etc.
for `st.*`):

```svelte
<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { slide } from 'svelte/transition';
  import {
    AUTONOMY_MODES, MODE_LABELS, MODE_BLURBS,
    AUTONOMY_EXPLAINER_HEADING, AUTONOMY_EXPLAINER_BODY,
    type AutonomyMode,
  } from '../lib/autonomy';
  import type { AutonomyStore } from '../lib/autonomyStore';
  import { announceHeaderPopoverOpen, HEADER_POPOVER_EVENT } from '../lib/headerPopover';
  import { motionMs } from '../lib/motion';
  import Icon from './Icon.svelte';

  let { autonomy }: { autonomy: AutonomyStore } = $props();
  const st = $derived($autonomy);

  const MODE_ICONS = { observe: 'eye', propose: 'git-pull-request', propose_apply: 'zap' } as const;

  // popover UI (local)
  let open = $state(false);
  let confirming = $state(false);
  let pendingMode = $state<AutonomyMode | null>(null);
  let saving = $state(false);
  let postError = $state(false);
  let reasonInput = $state('');
  let explainerOpen = $state(false);

  // refs
  let containerEl = $state<HTMLDivElement | null>(null); // outside-click root
  let toggleEl = $state<HTMLButtonElement | null>(null);
  let reasonEl = $state<HTMLInputElement | null>(null);
  let segmentsEl = $state<HTMLElement | null>(null);      // dial container (measure)
  let segmentEls = $state<(HTMLElement | null)[]>([null, null, null]);

  // sliding pill geometry
  let pillLeft = $state(0), pillWidth = $state(0);
  let pillMeasured = $state(false), pillReady = $state(false);
  let pillRafId: number | undefined;

  const pillLabel = $derived(
    st.readError ? `${MODE_LABELS.observe} · fail-closed` : MODE_LABELS[st.mode],
  );

  function resetPopover(): void {
    confirming = false; pendingMode = null; postError = false;
    reasonInput = ''; explainerOpen = false;
    pillMeasured = false; pillReady = false;  // Codex #3: no stale geometry
    if (pillRafId !== undefined) { cancelAnimationFrame(pillRafId); pillRafId = undefined; }
  }
  // returnFocus=false for foreign-bus closes so we don't yank focus back to this
  // (now-closed) toggle and away from the pill the user just opened (plan-review #1).
  function closePopover(returnFocus = true): void {
    open = false; resetPopover();
    if (returnFocus) toggleEl?.focus();
  }
  function onToggle(): void {
    if (open) { closePopover(); }
    else { resetPopover(); open = true; announceHeaderPopoverOpen('autonomy'); }
  }

  // Focus the ACTIVE segment on open (plan-review #2): type-safe via the bound
  // ref, no querySelector. Guard so we only grab focus while open.
  $effect(() => {
    if (!open) return;
    tick().then(() => {
      if (!open) return;
      segmentEls[AUTONOMY_MODES.indexOf(st.mode)]?.focus();
    });
  });

  // measurement — gate on open AND loaded AND mode; reset flags on close via
  // resetPopover. Effect re-runs on mode change while open → cleanup must cancel
  // the pending rAF too (plan-review #3, mirrors AutonomyControl:163).
  $effect(() => {
    const o = open, kind = st.kind, mode = st.mode;
    let cancelled = false; let ro: { disconnect(): void } | undefined;
    if (o && kind === 'loaded') {
      tick().then(() => {
        if (cancelled) return;
        measurePill();
        if (typeof ResizeObserver !== 'undefined' && segmentsEl) {
          const r = new ResizeObserver(() => { if (!cancelled) measurePill(); });
          r.observe(segmentsEl); ro = r;
        }
      });
    }
    return () => {
      cancelled = true;
      if (pillRafId !== undefined) { cancelAnimationFrame(pillRafId); pillRafId = undefined; }
      ro?.disconnect();
    };
  });
  function measurePill(): void {
    const i = AUTONOMY_MODES.indexOf(st.mode);
    const el = i >= 0 ? segmentEls[i] : null;
    if (!el) return;
    const w = el.offsetWidth;
    if (w > 0) {
      pillLeft = el.offsetLeft; pillWidth = w; pillMeasured = true;
      if (!pillReady && pillRafId === undefined) {
        pillRafId = requestAnimationFrame(() => { pillRafId = undefined; pillReady = true; });
      }
    }
  }

  function onSegmentClick(mode: AutonomyMode): void {
    if (saving || mode === st.mode) return;
    confirming = true; pendingMode = mode; postError = false; reasonInput = '';
    tick().then(() => reasonEl?.focus());
  }
  function onCancelArm(): void { confirming = false; pendingMode = null; postError = false; reasonInput = ''; }

  async function onConfirm(): Promise<void> {
    if (saving || !pendingMode) return;
    saving = true; postError = false;
    const ok = await autonomy.confirm(pendingMode, reasonInput);
    saving = false;
    if (ok) { open = false; resetPopover(); toggleEl?.focus(); }
    else { postError = true; confirming = false; pendingMode = null; }
  }

  function fmtUpdatedAt(iso: string | null): string { /* copy from AutonomyControl */ return iso ?? ''; }

  function onWindowKeydown(e: KeyboardEvent): void { if (e.key === 'Escape' && open && !saving) closePopover(); }
  function onWindowPointerDown(e: PointerEvent): void {
    if (!open || saving) return;
    if (containerEl && !containerEl.contains(e.target as Node)) closePopover();
  }
  onMount(() => {
    const onForeign = (e: Event) => {
      // foreign open closes us WITHOUT returning focus (plan-review #1)
      if ((e as CustomEvent).detail?.id !== 'autonomy' && open && !saving) closePopover(false);
    };
    window.addEventListener(HEADER_POPOVER_EVENT, onForeign);
    return () => window.removeEventListener(HEADER_POPOVER_EVENT, onForeign);
  });

  const confirmLabel = $derived(saving ? 'Saving…' : 'Confirm');
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<div class="autonomy-pill" bind:this={containerEl}>
  {#if st.kind === 'loading'}
    <span class="ds-pill ds-pill--muted autonomy-pill__status" data-testid="autonomy-pill-state">Mode · checking…</span>
  {:else if st.kind === 'unknown'}
    <button class="ds-pill ds-pill--warn autonomy-pill__btn" type="button"
      data-testid="autonomy-pill-retry"
      aria-label="Autonomy state could not be read — retry."
      title="Autonomy state could not be read; the agent is failing closed to Observe. Click to retry."
      onclick={() => void autonomy.fetchAutonomy()}><Icon name="alert-triangle" size={12} />State unknown · retry</button>
  {:else}
    <button class="ds-pill {st.readError ? 'ds-pill--warn' : 'ds-pill--muted'} autonomy-pill__btn"
      class:autonomy-pill__btn--open={open}
      type="button" data-testid="autonomy-pill-toggle"
      aria-haspopup="dialog" aria-expanded={open}
      aria-label={`Autonomy mode: ${pillLabel}. Change it.`}
      bind:this={toggleEl} onclick={onToggle}
      ><Icon name={MODE_ICONS[st.mode]} size={12} />{pillLabel}<Icon name="chevron-down" size={12} extraClass="autonomy-pill__chev" /></button>

    {#if open}
      <div class="autonomy-popover" data-testid="autonomy-popover" role="dialog"
        aria-label="Autonomy mode" transition:slide={{ duration: motionMs(160) }}>
        <!-- PORT: the 3-segment dial, caption+blurb, arm-then-confirm row,
             explainer, meta line / read-error, and inline error from
             AutonomyControl.svelte's loaded branch. Replace currentMode→st.mode,
             currentReason/Actor/UpdatedAt/ReadError→st.*, segmentEls binding,
             bind:this={segmentsEl} on the .autonomy-segments container, and
             onSegmentClick/onCancelArm/onConfirm wired as above. -->
      </div>
    {/if}
  {/if}
</div>

<style>
  /* PORT the .autonomy-segments* / caption / explainer / meta / confirm-row /
     error styles from AutonomyControl.svelte. ADD the pill + popover chrome
     mirroring PausePill: .autonomy-pill (position:relative; inline-flex),
     .autonomy-pill__btn (strip native chrome), .autonomy-pill__chev rotate on
     open, and .autonomy-popover (position:absolute; top:calc(100%+6px); right:0;
     z-index:30; width:min(22rem,85vw); …). Reuse ds-* tokens only. */
</style>
```

Notes for the port:
- The dial container must be `bind:this={segmentsEl}` (the measurement target),
  distinct from `containerEl` (the outside-click root). Each segment button keeps
  `bind:this={segmentEls[i]}` and `data-testid="autonomy-mode-{mode}"`.
- The arm-row hint reuses `MODE_BLURBS[pendingMode]`, so raising to Propose + Apply
  now shows the honest dependency/infra boundary automatically (Task 2).
- The meta-line / `autonomy-read-error` markup is the same as `AutonomyControl`,
  reading `st.reason/st.actor/st.updatedAt/st.readError`.
- Keep `aria-pressed` on segments bound to `st.mode` (NOT pendingMode) — no
  optimistic update.

**Step 4: Run green** (`npx vitest run tests/unit/AutonomyPill.test.ts`). Iterate on
the port until all cases pass. **Step 5: Commit** (`feat(ui): AutonomyPill header pill + popover dial`).

---

## Task 5: PausePill mutual-exclusion (`components/PausePill.svelte`)

Make Pause a participant on the bus so opening it closes Autonomy and vice versa.
Minimal, additive — existing PausePill tests stay green.

**Files:**
- Modify: `frontend/src/components/PausePill.svelte`
- Test: `frontend/tests/unit/PausePill.test.ts` (add 1 test)

**Step 1: Add the failing test**

```ts
  it('opening announces "pause"; a foreign open closes the popover (Codex #7)', async () => {
    const { getByTestId, queryByTestId } = await mount(res(RUNNING));
    const seen: string[] = [];
    const h = (e: Event) => seen.push((e as CustomEvent).detail?.id);
    window.addEventListener('ds:header-popover-open', h);
    await fireEvent.click(getByTestId('pause-pill-toggle'));
    await waitFor(() => expect(getByTestId('pause-popover')).toBeTruthy());
    expect(seen).toContain('pause');
    window.dispatchEvent(new CustomEvent('ds:header-popover-open', { detail: { id: 'autonomy' } }));
    await waitFor(() => expect(queryByTestId('pause-popover')).toBeNull());
    window.removeEventListener('ds:header-popover-open', h);
  });
```

**Step 2: Run red.**

**Step 3: Implement** — in `PausePill.svelte`:
- add `import { onMount } from 'svelte';` and
  `import { announceHeaderPopoverOpen, HEADER_POPOVER_EVENT } from '../lib/headerPopover';`
- give `closePopover` a `returnFocus = true` param (plan-review #1) and gate the
  `toggleEl?.focus()` on it; existing callers pass nothing → unchanged:

```ts
  function closePopover(returnFocus = true): void {
    open = false;
    postError = false;
    reasonInput = '';
    if (returnFocus) toggleEl?.focus();
  }
```

- in `onToggle()`'s open branch, after `open = true; postError = false;`, add
  `announceHeaderPopoverOpen('pause');`
- add the foreign-open listener (closes without stealing focus):

```ts
  onMount(() => {
    const onForeign = (e: Event) => {
      if ((e as CustomEvent).detail?.id !== 'pause' && open && !saving) closePopover(false);
    };
    window.addEventListener(HEADER_POPOVER_EVENT, onForeign);
    return () => window.removeEventListener(HEADER_POPOVER_EVENT, onForeign);
  });
```

> The existing `$effect` that resets popover state when the store leaves `running`
> (PausePill.svelte:35-42) sets `open = false` directly (no focus call) — leave it;
> it is not a user-driven close and must not move focus.

**Step 4: Run green** (`npx vitest run tests/unit/PausePill.test.ts` — new + all 15 existing). **Step 5: Commit** (`feat(ui): PausePill joins header-popover mutual exclusion`).

---

## Task 6: CapabilityCard reads the note via prop (`components/CapabilityCard.svelte`)

Drop the card's private best-effort `/autonomy` fetch; accept the note as an
optional prop (App derives it from the shared store via `autonomyNoteFor`). Best-
effort silence (loading/unknown/propose_apply → no note) is preserved because
`autonomyNoteFor` returns null there (Codex #1).

**Files:**
- Modify: `frontend/src/components/CapabilityCard.svelte`
- Test: `frontend/tests/unit/CapabilityCard.test.ts` (rewrite the Task-10 block;
  trim `/autonomy` from the Task-4 stubs)

**Step 1: Adjust tests first (red).** In `CapabilityCard.test.ts`:
- Replace the entire `describe('CapabilityCard — autonomy mode note (Task 10)', …)`
  block (lines ~419-581) with two small render tests driving the prop (the COPY
  itself is now pinned in `autonomyStore.test.ts`):

```ts
describe('CapabilityCard — autonomy note (via prop)', () => {
  const okJson = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { 'content-type': 'application/json' } });
  function open(call: (p: string) => Promise<Response>, props: Record<string, unknown> = {}) {
    const view = render(CapabilityCard, { props: { call, ...props } });
    const el = view.getByTestId('capability-card') as HTMLDetailsElement;
    el.open = true; return { view, el };
  }
  it('renders the note when autonomyNote is provided', async () => {
    const { view, el } = open(async () => okJson(FIXTURE), { autonomyNote: 'NOTE-TEXT' });
    await fireEvent(el, new Event('toggle'));
    await waitFor(() => expect(view.getByTestId('capability-autonomy-note').textContent).toContain('NOTE-TEXT'));
  });
  it('omits the note when autonomyNote is null (default)', async () => {
    const { view, el } = open(async () => okJson(FIXTURE));
    await fireEvent(el, new Event('toggle'));
    await waitFor(() => expect(view.getByTestId('cap-gates')).toBeTruthy());
    expect(view.queryByTestId('capability-autonomy-note')).toBeNull();
  });
});
```

- In the Task-4 prompt-disclosure stubs, the `if (path === '/autonomy') …` branches
  are now dead (the card never fetches it) — leave them or delete them; either is
  fine since the card ignores `/autonomy`. (Deleting is tidier.)

**Step 2: Run red** — the rewritten tests fail (prop not wired; old note logic still fetches).

**Step 3: Implement** — in `CapabilityCard.svelte`:
- Remove the `import { parseAutonomyDoc }` and `import type { AutonomyDoc }` lines.
- Add `autonomyNote` to props:

```ts
  let {
    call,
    autonomyNote = null,
  }: {
    call: (path: string, init?: RequestInit) => Promise<Response>;
    autonomyNote?: string | null;
  } = $props();
```

- Delete `let autonomyDoc = …`, the `fetchAutonomyBestEffort()` function, and the
  `const autonomyNote = $derived(…)` block.
- In `onToggle`, remove the `void fetchAutonomyBestEffort();` call (keep the rest).
- The template `{#if autonomyNote}` block stays as-is (now reads the prop).

**Step 4: Run green** (`npx vitest run tests/unit/CapabilityCard.test.ts`). **Step 5: Commit** (`refactor(ui): CapabilityCard autonomy note via prop (shared store)`).

---

## Task 7: tour copy + spotlight target (`lib/tour.ts`)

Reword `CONTROLS_LINE` to name the visible control ("Mode control in the top bar",
not "dial"); keep the honest boundary. The `data-tour="controls"` MARKER moves in
Task 8 (App). Pure-copy change here.

**Files:**
- Modify: `frontend/src/lib/tour.ts:133-139`
- Test: `frontend/tests/unit/tour.test.ts:223-230`

**Step 1: Update the test (red)** — extend the existing `CONTROLS_LINE` test:

```ts
  it('CONTROLS_LINE scopes the gate claim to infrastructure edits (T2)', () => {
    expect(CONTROLS_LINE).toContain('infrastructure edits pass your explicit approval gate');
    expect(CONTROLS_LINE).toContain('routine dependency updates');
    expect(CONTROLS_LINE).toContain('Pause');
    expect(CONTROLS_LINE.toLowerCase()).not.toContain('safety');
    expect(CONTROLS_LINE).toContain('top bar');
    // reworded to name the visible control, not "dial" (header pill redesign)
    expect(CONTROLS_LINE).toContain('Mode control');
    expect(CONTROLS_LINE.toLowerCase()).not.toContain('dial');
  });
```

**Step 2: Run red.**

**Step 3: Implement** — replace `CONTROLS_LINE` (keep every pinned phrase):

```ts
export const CONTROLS_LINE =
  'The Mode control in the top bar governs what Anchor does on its own when it ' +
  'spots a change, and what the other agents may do when you ask: Observe (they ' +
  'only watch and report), Propose (they draft changes for your review), or ' +
  'Propose + Apply (they may also complete routine dependency updates ' +
  'end-to-end). At every setting, infrastructure edits pass your explicit ' +
  'approval gate. The Pause control sits next to it in the top bar and suspends ' +
  'all agent activity in one click.';
```

**Step 4: Run green** (`npx vitest run tests/unit/tour.test.ts`). **Step 5: Commit** (`refactor(ui): tour controls copy names the Mode pill, not a dial`).

---

## Task 8: wire it into `App.svelte`

Create the store, fetch on mount, mount the pill in the header (before Pause) under
a `data-tour="controls"` anchor, remove the chat-area dial card, pass the derived
note to CapabilityCard, and make the header actions wrap (Codex #6).

**Files:**
- Modify: `frontend/src/App.svelte` (imports ~43-47, store ~212, mount ~659,
  header ~671-688, chat-area ~704-707 + 719, CSS ~793)
- Test: covered by `App.test.ts` (no change needed — its catch-all `okJson({})`
  makes `/autonomy` resolve to `unknown`; the pill renders the inert "State unknown"
  button, asserted by nothing). Run it to confirm.

**Step 1: Imports** — after the PausePill import block:
- add `import AutonomyPill from './components/AutonomyPill.svelte';`
- add `import { createAutonomyStore, autonomyNoteFor } from './lib/autonomyStore';`
- **remove** `import AutonomyControl from './components/AutonomyControl.svelte';` (line 47)

**Step 2: Store + derived note** — after `const pause = createPauseStore(call);` (line 212):

```ts
  // ---- autonomy dial (one shared store → header AutonomyPill + the capability
  // card note, so the two surfaces never diverge or double-fetch) ----
  const autonomy = createAutonomyStore(call);
  const capabilityAutonomyNote = $derived(autonomyNoteFor($autonomy));
```

**Step 3: Mount fetch** — in `onMount` (after `void pause.fetchPause();`, line 659):
`void autonomy.fetchAutonomy();`

**Step 4: Header** — in `.app-header__actions` (line 678), put the pill BEFORE
PausePill, wrapped in the tour anchor:

```svelte
  <div class="app-header__actions">
    <div class="header-tour-anchor" data-tour="controls">
      <AutonomyPill {autonomy} />
    </div>
    <PausePill {pause} />
    <button … >Tour</button>
    <TokenStatus … />
  </div>
```

**Step 5: Chat area** — replace lines 704-707:

```svelte
    <div class="tour-target" data-tour="controls">
      <PauseBanner {pause} />
      <AutonomyControl {call} />
    </div>
```

with just (the spotlight + dial are gone; PauseBanner stays, only shown when paused):

```svelte
    <PauseBanner {pause} />
```

**Step 6: CapabilityCard** — line 719, pass the note:

```svelte
    <CapabilityCard {call} autonomyNote={capabilityAutonomyNote} />
```

**Step 7: CSS (Codex #6)** — make actions wrap; anchor is inline so it doesn't
disturb layout:

```css
  .app-header__actions {
    display: inline-flex;
    align-items: center;
    gap: var(--ds-sp-3);
    flex-wrap: wrap;
    justify-content: flex-end;
  }
  .header-tour-anchor {
    display: inline-flex;
    align-items: center;
  }
  @media (max-width: 640px) {
    .app-title__sub { display: none; }
  }
```

**Step 8: Run** `npx vitest run tests/unit/App.test.ts` → green. **Step 9: Commit**
(`feat(ui): mount AutonomyPill in header; retire the chat-area dial card`).

---

## Task 9: delete the old dial

**Files:**
- Delete: `frontend/src/components/AutonomyControl.svelte`
- Delete: `frontend/tests/unit/AutonomyControl.test.ts`

**Step 1:** `git rm frontend/src/components/AutonomyControl.svelte frontend/tests/unit/AutonomyControl.test.ts`

**Step 2: Verify no dangling references** —
`grep -rn "AutonomyControl\|autonomy-control" frontend/src frontend/tests` → empty.

**Step 3: Run full suite** `cd frontend && npx vitest run` → all green (count
≈ 819 − AutonomyControl tests + new autonomyStore/AutonomyPill/headerPopover tests).

**Step 4: Commit** (`refactor(ui): remove AutonomyControl (moved into AutonomyPill)`).

---

## Task 10: full gate + visual verify + reviews

**Step 1: Static + build gate** (from `frontend/`):
- `npx svelte-check --tsconfig ./tsconfig.json` → **0 errors / 0 warnings**
- `npx vitest run` → all green
- `npx vite build` → succeeds

**Step 2: Playwright smoke** — `tests/smoke/` does not reference the autonomy
testids (verified), so no fixture repoint is needed. Run the smoke per the repo's
recipe (live-probe memory) to confirm the SPA still boots and the header renders.

**Step 3: Manual visual verify** (the live-rig recipe from the ui-polish memory:
rebuild, restart uvicorn, free :8765): open `/`, confirm the header shows
`[⚡ Propose + Apply ▾] [Active ▾] [Tour] [token]`; clicking the autonomy pill drops
the dial popover; opening Pause closes Autonomy and vice versa; the chat area no
longer has the big dial card; the Tour "You set the pace" step now spotlights the
header pill.

**Step 4: Adversarial review (Workflow)** — fan out reviewers over: focus
management + a11y (aria-haspopup/expanded, focus trap-ish return), the commit-wins
+ single-flight guards under the new two-reader topology, popover measurement
flash on reopen/resize, header overflow at narrow widths, the honest-copy boundary,
and tour spotlight resolution in loading/unknown states. Verify each finding before
acting.

**Step 5: Codex post-implementation review** — `mcp__codex__codex-reply` on the
plan thread (`019f0a05`): completed work vs the original plan + the 8 folded points.
Evaluate findings on merits (advisory).

**Step 6:** open the PR; deploy via coordinator rebake + traffic shift once
CI-green + Codex-SHIP (deploy-autonomy memory). Confirm `_TAG` == `git log
origin/main` HEAD before the rebake (race lesson) — and note the OTHER agent is
also working on main, so re-check the merge base before deploying.

---

## Risk notes / invariants

- **Two `/autonomy` readers, one store** — the commit-wins guard (Task 3) is now
  load-bearing: a Capability-card-triggered refresh must not stomp a pill confirm.
- **Header popover exclusivity** is best-effort (window CustomEvent); it degrades
  to "pointer-outside already closes the other" if the bus ever fails. Acceptable.
- **`data-tour="controls"` must live on an always-rendered element** (the
  `.header-tour-anchor` wrapper) so the spotlight resolves even while `/autonomy`
  is loading/unknown (Codex #8).
- **Merge conflict watch:** the other agent has uncommitted edits to
  `frontend/src/lib/tour.ts` + `frontend/tests/unit/tour.test.ts` on main. This
  branch also edits both (Task 7). Reconcile at merge time.
- **CapabilityCard behavior change (intentional, Codex plan-review #7):** opening
  the card no longer performs an independent `/autonomy` retry after an App-level
  fetch failure — the note simply stays silent (kind `unknown` → null). This is the
  single-source-of-truth tradeoff; call it out in the PR body.

## Codex plan-review disposition (thread 019f0a19)

Verdict: **proceed-with-changes.** 7 points, all evaluated and **folded** (none
rejected): (1) foreign-bus close must not steal focus → `closePopover(returnFocus)`;
(2) the focus-on-open effect was fragile/type-invalid → focus the active segment via
its bound ref after `tick()`; (3) the measurement effect cleanup must cancel the
pending rAF, not only `ResizeObserver`; (4) name the "≤1 open except during a saving
commit" invariant; (5) add tests for focus-not-stolen + focus-into-popover; (6) the
unknown pill needs a visible "· retry" affordance + aria-label, not just `title`;
(7) document CapabilityCard's loss of an independent open-time `/autonomy` retry as
intentional. The architecture, commit sequencing, and the App-test `{}`-catch-all
analysis were confirmed sound.
