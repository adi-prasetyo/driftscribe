# Inline Env-Diff Card (Phase 3, PR 1 of 2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.

**Scope (PR 1 of 2):** This plan is the **frontend-only** env-diff card. A second, independent PR (PR 2) closes a *pre-existing* raw-`rationale` leak at the **backend** — see "Companion: PR 2" below. The two ship separately: the card needs no redeploy; PR 2 needs a coordinator redeploy. The card is safe on its own (it redacts `diffs[]` client-side); the rationale leak is orthogonal and predates this work.

**Goal:** Surface a structured, always-visible env-diff card under the hero when an operator opens a historical drift decision, rendering each `expected`/`live` value exactly as the backend renderer already does in the GitHub PR/issue body (secret-like → `(value redacted: secret-like)`, else the value).

**Architecture:** Two pure, unit-tested lib modules feed one dumb Svelte component. `lib/secret_guard.ts` ports the two redaction regexes from `agent/secret_guard.py` verbatim (`shouldRedact = isSecretName(name) || valueLooksCredentialed(value)`). `lib/diff.ts` turns a `Decision` into a small SAFE set of display rows (mirroring `lib/decision.ts`'s allowlist discipline — never iterate arbitrary keys, never trust shape) via `displayDiffValue` (a mirror of the backend's `_format_value_cell`). `DriftDiffCard.svelte` renders those rows as an auto-escaped 4-column table. It mounts in `App.svelte` after `<FinalResponse>`, gated `historicalActive && historicalDecision`, and self-suppresses when there are no diffs.

**Tech Stack:** Svelte 5 (runes), TypeScript, Vitest (lib unit), Playwright (smoke booting the real FastAPI app).

**Security north star:** The decision doc from `GET /trace` + `/decisions` is **unredacted** — the raw `expected`/`live` values are already in the browser's JSON regardless of what we paint. This card therefore must (a) NEVER render a value through `{@html}` (always auto-escaped `{text}`); (b) redact with the SAME rule the backend uses for the GitHub artifact, so the inline card discloses **nothing the operator couldn't already see** by clicking the Phase-1 GitHub link — zero net-new disclosure; (c) follow `lib/decision.ts`'s discipline: render only a fixed, known set of fields, defensively validating each diff's shape rather than trusting it.

This is **UI disclosure control, not a secrecy boundary** (Codex review). The raw values remain in the JSON on the wire and are reachable by anyone holding the operator token (devtools, network tab, Playwright traces). The redaction reduces shoulder-surf / screenshot / casual-DOM exposure to match exactly what the operator-facing GitHub artifact already shows. The `diffs[]` values stay raw on the wire by design — the card needs them to show non-secret drift (e.g. `LOG_LEVEL: info→debug`) while redacting secrets; the only consumer that renders `diffs[]` is this card.

**Companion: PR 2 (backend rationale scrub — separate plan/PR, NOT built here):** `rendered_body` is scrubbed by `agent/renderer.py`, but the decision doc also persists `rationale: proposal.rationale` **raw/unscrubbed** (`agent/main.py:985`, `:1388`), and BOTH UIs read it raw: `App.svelte:306` (`asString(d.rationale) ?? asString(d.rendered_body)`) AND the legacy `/ui/transparency-legacy` template (`transparency_legacy.html:2129`). So an LLM that quotes a secret value in its rationale prose leaks it — independent of, and predating, this card. PR 2 fixes the **root cause at the backend**: scrub `rationale` in the `/trace` + `/decisions` responses (reusing `_scrub_secret_values_from_rationale` with the doc's own diffs), so every consumer — Svelte SPA, legacy route, future API — gets the scrubbed value, including already-persisted docs. We deliberately do NOT scrub `rationale` client-side here (single source of truth lives in the backend; no triplicated heuristic). Retiring the vestigial legacy route is reasonable independent cleanup but is not the security fix.

**Source-of-truth coupling:** `lib/secret_guard.ts` duplicates the regexes from `agent/secret_guard.py`. A parity unit test pins the TS behaviour against a table copied from the Python, and a header comment names the Python module as the source of truth + the drift risk. If `agent/secret_guard.py`'s `SECRET_NAME_PATTERN` / `_CREDENTIALED_URL` ever change, the TS port and its test must change in lockstep. NB the JS-vs-Python difference: redaction here additionally **clamps** shown values to 256 chars, so display is not byte-for-byte identical to the backend cell — only the **redaction decision** is identical (that is what matters). Python `\b` is Unicode-aware while JS is ASCII-ish; the only divergence is rare Unicode-prefix cases and it errs toward over-redaction (safe).

---

## Reference: backend shapes (read-only, do NOT modify)

- `agent/models.py:24` — `EnvDiff(name: str, expected: str|None, live: str|None, contract_status: ContractStatus, debug_config_value: str|None, recent_pr_match: str|None)`.
- `agent/models.py:4` — `ContractStatus` enum values: `absent`, `present_allow_manual`, `present_disallow_manual`, `match`.
- `agent/secret_guard.py:14` — `SECRET_NAME_PATTERN = /(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE|SALT|DSN|OAUTH|URL|URI|CONNECTION|CONNSTR)/i`.
- `agent/secret_guard.py:22` — `_CREDENTIALED_URL = /\b[a-z][a-z0-9+.-]*:\/\/[^/@\s]*:[^/@\s]*@/i`.
- `agent/secret_guard.py:36` — `should_redact(name, value) = is_secret_name(name) or value_looks_credentialed(value)`.
- `agent/renderer.py:24` — `_format_value_cell(name, value)`: `should_redact` → `(value redacted: secret-like)` if value is not None else `—`; value is None → `—`; else the value. **Empty string is NOT collapsed to `—`** (an explicitly-unset var is real drift).
- `agent/renderer.py:68` — `_scrub_secret_values_from_rationale(rationale, diffs)`: the existing backend scrubber. NOT used in PR 1; it is PR 2's basis (the backend will apply it to the served `rationale`).
- Persisted decision doc carries `diffs: [EnvDiff.model_dump(mode="json"), ...]` raw/unredacted (`agent/main.py:986`, `:1389`).
- `frontend/src/App.svelte:306` — `finalReply = asString(d.rationale) ?? asString(d.rendered_body)`; `:310` — `historicalDecision = (t.decision as Decision) ?? null`. Drift decisions always set `rationale`, so `finalReply != null` and `DecisionSummary` (gated `finalReply == null`) never renders for them — which is why this card needs its own placement.

---

### Task 1: Types — `EnvDiff`, `ContractStatus`, `Decision.diffs`

**Files:**
- Modify: `frontend/src/lib/types.ts`

No test (type-only; `npm run check` is the gate).

**Step 1: Add the enum + interface + field**

In `frontend/src/lib/types.ts`, after the `DecisionGithub` interface (before `Decision`), add:

```ts
/** Mirrors agent/models.py:ContractStatus. The per-var verdict of the live env
 *  against ops-contract.yaml. Rendered as a status pill on the env-diff card. */
export type ContractStatus =
  | 'absent'
  | 'present_allow_manual'
  | 'present_disallow_manual'
  | 'match';

/** One env-var drift row (GET /trace → decision.diffs[]). Mirrors
 *  agent/models.py:EnvDiff. `expected`/`live` are RAW env-var values and may be
 *  secrets (the decision doc is unredacted) — never render them directly; route
 *  every value through `displayDiffValue` (lib/diff.ts). Only the fields the
 *  card renders are typed; the backend also ships debug_config_value /
 *  recent_pr_match, intentionally omitted (YAGNI). */
export interface EnvDiff {
  name: string;
  expected?: string | null;
  live?: string | null;
  contract_status?: ContractStatus | string;
}
```

Then add `diffs` to the `Decision` interface (after `github`):

```ts
  github?: DecisionGithub | null;
  diffs?: EnvDiff[];
```

**Step 2: Verify it type-checks**

Run: `cd frontend && npm run check`
Expected: 0 errors, 0 warnings.

**Step 3: Commit**

```bash
git add frontend/src/lib/types.ts
git commit -m "feat(ui): type Decision.diffs (EnvDiff + ContractStatus) for the env-diff card"
```

---

### Task 2: `lib/secret_guard.ts` — TS port of the backend redaction heuristic

**Files:**
- Create: `frontend/src/lib/secret_guard.ts`
- Test: `frontend/tests/unit/secret_guard.test.ts`

**Step 1: Write the failing parity test**

Create `frontend/tests/unit/secret_guard.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { isSecretName, valueLooksCredentialed, shouldRedact } from '../../src/lib/secret_guard';

// PARITY with agent/secret_guard.py — these cases mirror the Python heuristic.
// If the Python SECRET_NAME_PATTERN / _CREDENTIALED_URL change, change both.
describe('secret_guard parity — isSecretName', () => {
  for (const name of [
    'API_TOKEN', 'DB_PASSWORD', 'SIGNING_KEY', 'CLIENT_SECRET', 'DATABASE_URL',
    'SERVICE_URI', 'DB_CONNECTION', 'CONNSTR', 'JWT_AUDIENCE', 'OAUTH_SCOPE',
    'private_key', 'x_auth_header', 'BEARER_PREFIX', 'PWD_SALT', 'DSN',
  ]) {
    it(`flags ${name}`, () => expect(isSecretName(name)).toBe(true));
  }
  for (const name of ['LOG_LEVEL', 'TIMEOUT_MS', 'FEATURE_FLAG_X', 'REGION', 'ENDPOINT', 'MAX_RETRIES']) {
    it(`passes ${name}`, () => expect(isSecretName(name)).toBe(false));
  }
});

describe('secret_guard parity — valueLooksCredentialed', () => {
  it('flags scheme://user:pass@host', () =>
    expect(valueLooksCredentialed('postgres://u:p4ss@db.internal/prod')).toBe(true));
  it('flags https with embedded auth', () =>
    expect(valueLooksCredentialed('https://admin:hunter2@svc/api')).toBe(true));
  it('passes a plain URL with no userinfo', () =>
    expect(valueLooksCredentialed('https://example.com/path')).toBe(false));
  it('passes a non-URL value', () => expect(valueLooksCredentialed('debug')).toBe(false));
  it('passes null/undefined/empty', () => {
    expect(valueLooksCredentialed(null)).toBe(false);
    expect(valueLooksCredentialed(undefined)).toBe(false);
    expect(valueLooksCredentialed('')).toBe(false);
  });
});

describe('secret_guard parity — shouldRedact (name OR value)', () => {
  it('redacts a secret-named var regardless of value', () =>
    expect(shouldRedact('API_TOKEN', 'anything')).toBe(true));
  it('redacts a non-secret name with a credentialed value', () =>
    expect(shouldRedact('ENDPOINT', 'https://a:b@h/x')).toBe(true));
  it('does not redact a plain non-secret var', () =>
    expect(shouldRedact('LOG_LEVEL', 'debug')).toBe(false));
  it('secret name still redacts when value is null', () =>
    expect(shouldRedact('SECRET_KEY', null)).toBe(true));
});
```

**Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/secret_guard.test.ts`
Expected: FAIL — cannot resolve `../../src/lib/secret_guard`.

**Step 3: Write the implementation**

Create `frontend/src/lib/secret_guard.ts`:

```ts
// secret_guard.ts — TS port of agent/secret_guard.py's redaction heuristic.
//
// SOURCE OF TRUTH: agent/secret_guard.py. These two regexes are duplicated
// verbatim from SECRET_NAME_PATTERN and _CREDENTIALED_URL. The parity test
// (tests/unit/secret_guard.test.ts) pins this port to the Python behaviour; if
// the Python heuristic changes, change BOTH the regex here and the test.
//
// Used by lib/diff.ts (displayDiffValue) so the operator UI redacts env-var
// values with EXACTLY the rule the backend renderer uses when it writes the
// GitHub PR/issue body — the inline env-diff card discloses nothing the operator
// couldn't already see by opening that artifact.

// Name-based: env var names that conventionally hold credentials. Includes
// URL/URI/CONNECTION because `DATABASE_URL=postgres://u:p@h/db` would otherwise
// render with the embedded password.
const SECRET_NAME =
  /(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CRED|PRIVATE|AUTH|BEARER|JWT|SIGNATURE|SALT|DSN|OAUTH|URL|URI|CONNECTION|CONNSTR)/i;

// Value-based: URLs with userinfo (`scheme://user:pass@host`) are credentials
// regardless of the var's name.
const CREDENTIALED_URL = /\b[a-z][a-z0-9+.-]*:\/\/[^/@\s]*:[^/@\s]*@/i;

export function isSecretName(name: string): boolean {
  return SECRET_NAME.test(name);
}

export function valueLooksCredentialed(value: string | null | undefined): boolean {
  if (!value) return false;
  return CREDENTIALED_URL.test(value);
}

/** Combined: redact if the name is secret-like OR the value looks credentialed. */
export function shouldRedact(name: string, value: string | null | undefined): boolean {
  return isSecretName(name) || valueLooksCredentialed(value);
}
```

**Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run tests/unit/secret_guard.test.ts`
Expected: PASS (all cases green).

**Step 5: Commit**

```bash
git add frontend/src/lib/secret_guard.ts frontend/tests/unit/secret_guard.test.ts
git commit -m "feat(ui): port secret_guard redaction heuristic to TS (parity-tested)"
```

---

### Task 3: `lib/diff.ts` — safe display rows for the env-diff card

**Files:**
- Create: `frontend/src/lib/diff.ts`
- Test: `frontend/tests/unit/diff.test.ts`

**Step 1: Write the failing test**

Create `frontend/tests/unit/diff.test.ts`:

```ts
import { describe, it, expect } from 'vitest';
import { displayDiffValue, diffRows } from '../../src/lib/diff';
import type { Decision } from '../../src/lib/types';

const REDACTED = '(value redacted: secret-like)';

describe('displayDiffValue — mirrors agent/renderer.py:_format_value_cell', () => {
  it('shows a plain value for a non-secret var', () =>
    expect(displayDiffValue('LOG_LEVEL', 'debug')).toBe('debug'));
  it('redacts when the name is secret-like (value present)', () =>
    expect(displayDiffValue('API_TOKEN', 'sk-live-abc')).toBe(REDACTED));
  it('redacts when the value is a credentialed URL (non-secret name)', () =>
    expect(displayDiffValue('ENDPOINT', 'https://a:b@h/x')).toBe(REDACTED));
  it('renders em-dash for a null value (not redacted)', () =>
    expect(displayDiffValue('LOG_LEVEL', null)).toBe('—'));
  it('renders em-dash for a secret-named null value', () =>
    expect(displayDiffValue('API_TOKEN', null)).toBe('—'));
  it('preserves empty string (an explicitly-unset var is real drift)', () =>
    expect(displayDiffValue('LOG_LEVEL', '')).toBe(''));
  it('redacts BEFORE clamping — a credentialed URL whose :pass@ is past 256 chars still redacts', () => {
    const pad = 'a'.repeat(300);
    const url = `https://user:secretpw@host.example/${pad}`; // userinfo within 256, but value > 256
    expect(displayDiffValue('ENDPOINT', url)).toBe(REDACTED);
  });
  it('clamps a long NON-secret value to 256 chars + ellipsis', () => {
    const long = 'x'.repeat(300);
    const out = displayDiffValue('LOG_LEVEL', long);
    expect(out.endsWith('…')).toBe(true);
    expect(out.length).toBe(257);
  });
});

describe('diffRows — safe rows from a decision', () => {
  it('returns [] for a decision with no diffs', () =>
    expect(diffRows({ decision_id: 'd', action: 'drift_issue' } as Decision)).toEqual([]));

  it('returns [] for null', () => expect(diffRows(null)).toEqual([]));

  it('maps each diff to a display row, redacting secrets', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [
        { name: 'LOG_LEVEL', expected: 'info', live: 'debug', contract_status: 'present_allow_manual' },
        { name: 'API_TOKEN', expected: 'sk-old', live: 'sk-new', contract_status: 'present_disallow_manual' },
      ],
    } as unknown as Decision;
    expect(diffRows(d)).toEqual([
      { name: 'LOG_LEVEL', expected: 'info', live: 'debug', status: 'present_allow_manual', badge: 'ok' },
      { name: 'API_TOKEN', expected: REDACTED, live: REDACTED, status: 'present_disallow_manual', badge: 'danger' },
    ]);
  });

  it('skips a malformed diff (no string name) but keeps the rest', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [{ expected: 'x' }, null, 'nope', { name: 'OK', live: 'v', contract_status: 'absent' }],
    } as unknown as Decision;
    const rows = diffRows(d);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({ name: 'OK', expected: '—', live: 'v', status: 'absent', badge: 'warn' });
  });

  it('falls back to muted badge for an unknown contract_status', () => {
    const d = {
      decision_id: 'd', action: 'drift_issue',
      diffs: [{ name: 'X', expected: 'a', live: 'b', contract_status: 'bogus' }],
    } as unknown as Decision;
    expect(diffRows(d)[0].badge).toBe('muted');
  });
});
```

**Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/diff.test.ts`
Expected: FAIL — cannot resolve `../../src/lib/diff`.

**Step 3: Write the implementation**

Create `frontend/src/lib/diff.ts`:

```ts
// diff.ts — turn a stored decision's diffs[] into a small, SAFE set of display
// rows for the env-diff card. Mirrors lib/decision.ts's discipline: GET /trace
// returns the decision doc UNREDACTED, so this module NEVER trusts the shape and
// NEVER renders a raw value — every value goes through displayDiffValue, which
// applies the SAME redaction rule the backend uses for the GitHub PR/issue body
// (agent/renderer.py:_format_value_cell + agent/secret_guard.py).

import type { Decision, EnvDiff } from './types';
import type { FieldBadge } from './decision';
import { shouldRedact } from './secret_guard';

const REDACTED = '(value redacted: secret-like)';
const EMDASH = '—';

// Defensive cap (matches lib/decision.ts): clamp a shown value so a malformed /
// oversized one can't blow out the table. Applied AFTER the redaction decision
// so a long credentialed URL is still detected by shouldRedact on the full value.
const MAX_VALUE = 256;
const clamp = (s: string): string => (s.length > MAX_VALUE ? s.slice(0, MAX_VALUE) + '…' : s);

/** Mirror of agent/renderer.py:_format_value_cell. Redact (name secret-like OR
 *  value credentialed) → marker; null → em-dash; else the (clamped) value.
 *  Empty string is preserved — an explicitly-unset var is a real drift signal. */
export function displayDiffValue(name: string, value: string | null | undefined): string {
  if (shouldRedact(name, value)) return value != null ? REDACTED : EMDASH;
  if (value == null) return EMDASH;
  return clamp(value);
}

const CONTRACT_BADGE: Record<string, FieldBadge> = {
  // `match` = live matches the contract = no drift → ok/green. `muted` (grey) is
  // reserved for the `?? 'muted'` unrecognized-status fallback below, so the two
  // stay visually distinct.
  match: 'ok',
  present_allow_manual: 'ok',
  present_disallow_manual: 'danger',
  absent: 'warn',
};

export interface DiffRow {
  name: string;
  /** Already display-formatted (redacted-or-value or em-dash) — safe as text. */
  expected: string;
  live: string;
  /** Raw contract_status enum string (rendered as the pill label). */
  status: string;
  badge: FieldBadge;
}

/**
 * Ordered, safe display rows for a decision's env diffs. Pure. Defensively
 * validates each diff (skips any without a string `name`); never trusts shape.
 * Returns [] for a null decision or one with no diffs[].
 */
export function diffRows(d: Decision | null | undefined): DiffRow[] {
  if (!d || !Array.isArray(d.diffs)) return [];
  const rows: DiffRow[] = [];
  for (const raw of d.diffs as unknown[]) {
    if (!raw || typeof raw !== 'object') continue;
    const o = raw as Partial<EnvDiff>;
    const name = typeof o.name === 'string' ? o.name : '';
    if (!name) continue; // a diff with no name is unrenderable
    const expected = typeof o.expected === 'string' ? o.expected : null;
    const live = typeof o.live === 'string' ? o.live : null;
    const status = typeof o.contract_status === 'string' ? o.contract_status : '';
    rows.push({
      name: clamp(name),
      expected: displayDiffValue(name, expected),
      live: displayDiffValue(name, live),
      status,
      badge: CONTRACT_BADGE[status] ?? 'muted',
    });
  }
  return rows;
}
```

**Step 4: Run the test to verify it passes**

Run: `cd frontend && npx vitest run tests/unit/diff.test.ts`
Expected: PASS.

**Step 5: Commit**

```bash
git add frontend/src/lib/diff.ts frontend/tests/unit/diff.test.ts
git commit -m "feat(ui): lib/diff — safe env-diff display rows with backend-parity redaction"
```

---

### Task 4: `DriftDiffCard.svelte` — the table component

**Files:**
- Create: `frontend/src/components/DriftDiffCard.svelte`

No dedicated component test (covered by the smoke in Task 6 + lib tests in Tasks 2–3; the project has no Svelte component unit tests).

**Step 1: Write the component**

Create `frontend/src/components/DriftDiffCard.svelte`:

```svelte
<script lang="ts">
  // DriftDiffCard — a structured "what drifted" table for a historical drift
  // decision. Renders ONLY the safe rows produced by diffRows() (lib/diff.ts):
  // values are redacted with the same rule the backend uses for the GitHub
  // PR/issue body, and every cell is auto-escaped text ({value}, never {@html}).
  // Self-suppresses when there are no diffs, so App.svelte can mount it
  // unconditionally for any open historical decision.

  import type { Decision } from '../lib/types';
  import { diffRows } from '../lib/diff';

  let { decision }: { decision: Decision | null } = $props();

  const rows = $derived(diffRows(decision));
</script>

{#if rows.length > 0}
  <section class="ds-card drift-diff-card" data-testid="drift-diff-card" aria-label="Environment drift detail">
    <p class="ds-label drift-diff-card__label">Drift detail</p>
    <table class="drift-diff-card__table">
      <thead>
        <tr>
          <th scope="col">Var</th>
          <th scope="col">Expected</th>
          <th scope="col">Live</th>
          <th scope="col">Status</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as r, i (r.name + i)}
          <tr>
            <td><code class="ds-code">{r.name}</code></td>
            <td><code class="ds-code">{r.expected}</code></td>
            <td><code class="ds-code">{r.live}</code></td>
            <td>
              {#if r.status}
                <span class="ds-pill ds-pill--{r.badge}">{r.status}</span>
              {:else}
                <span class="ds-subtle">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </section>
{/if}

<style>
  .drift-diff-card {
    /* A neutral left accent — distinct from FinalResponse's hero green and from
       DecisionSummary's strong border. */
    border-left: 3px solid var(--ds-border-strong);
    padding: var(--ds-sp-5) var(--ds-sp-6);
  }

  .drift-diff-card__label {
    display: block;
    margin: 0 0 var(--ds-sp-4);
    color: var(--ds-muted);
  }

  .drift-diff-card__table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--ds-fs-2);
  }

  .drift-diff-card__table th {
    text-align: left;
    padding: 0 var(--ds-sp-4) var(--ds-sp-2) 0;
    color: var(--ds-muted);
    font-size: var(--ds-fs-1);
    font-weight: var(--ds-fw-semibold);
    text-transform: uppercase;
    letter-spacing: var(--ds-tracking-caps);
    border-bottom: 1px solid var(--ds-border);
  }

  .drift-diff-card__table td {
    padding: var(--ds-sp-3) var(--ds-sp-4) var(--ds-sp-3) 0;
    border-bottom: 1px solid var(--ds-border);
    vertical-align: top;
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .drift-diff-card__table tr:last-child td {
    border-bottom: none;
  }

  @media (max-width: 540px) {
    .drift-diff-card__table th:first-child,
    .drift-diff-card__table td:first-child {
      max-width: 8rem;
    }
  }
</style>
```

**Step 2: Verify it type-checks / builds**

Run: `cd frontend && npm run check`
Expected: 0 errors, 0 warnings.

**Step 3: Commit**

```bash
git add frontend/src/components/DriftDiffCard.svelte
git commit -m "feat(ui): DriftDiffCard — structured env-diff table (auto-escaped, redacted)"
```

---

### Task 5: Wire `DriftDiffCard` into `App.svelte`

**Files:**
- Modify: `frontend/src/App.svelte` (import block + the chat-area template ~line 361)

NOTE: PR 1 does NOT touch the `finalReply` / `openTrace` rationale path — the raw-rationale scrub is PR 2 (backend). The card reads `historicalDecision.diffs` and redacts them itself; it does not depend on the rationale being scrubbed.

**Step 1: Add the import**

In `App.svelte`'s `<script>`, alongside the other component imports (e.g. near `DecisionSummary`), add:

```ts
  import DriftDiffCard from './components/DriftDiffCard.svelte';
```

**Step 2: Render it after FinalResponse**

In the template, immediately after the `<FinalResponse .../>` line (currently `App.svelte:361`) and before the `{#if iacPr && !historicalActive}` block, add:

```svelte
    {#if historicalActive && historicalDecision}
      <DriftDiffCard decision={historicalDecision} />
    {/if}
```

(The component self-suppresses when the decision has no diffs, so this renders for drift/rollback decisions and is invisible for iac_apply / chat traces. It is intentionally independent of the `finalReply == null` guard so it shows ALONGSIDE the rationale prose, unlike `DecisionSummary`.)

**Step 3: Verify it type-checks + builds**

Run: `cd frontend && npm run check && npm run build`
Expected: check 0 errors/0 warnings; build completes (writes to `../agent/static`, which is gitignored — no artifact to commit).

**Step 4: Commit**

```bash
git add frontend/src/App.svelte
git commit -m "feat(ui): mount DriftDiffCard under the hero on historical drift decisions"
```

---

### Task 6: Smoke coverage — values shown, secrets redacted, no raw secret in DOM

**Files:**
- Modify: `frontend/tests/smoke/fixtures.ts`
- Modify: `frontend/tests/smoke/transparency.smoke.ts`

**Step 1: Add the drift trace fixture**

In `frontend/tests/smoke/fixtures.ts`, add a stable trace-id const for the existing `d-drift-1` rail row (its `trace_id`) and a trace response whose decision carries three diffs — one shown, one redacted-by-name, one redacted-by-value. Put the const near the other IDs:

```ts
// d-drift-1's trace_id (see decisionsResponse). Its /trace carries a decision
// doc with env diffs so the smoke can exercise the DriftDiffCard.
export const DRIFT_CARD_TRACE_ID = 'aa11bb22cc33dd44ee55ff6600112233';

// Distinctive raw secret values — the smoke asserts NONE of these appear in the
// DOM (the card redacts them; the rationale below never quotes them).
export const SECRET_TOKEN_VALUE_OLD = 'sk-old-DEADBEEF0000';
export const SECRET_TOKEN_VALUE_NEW = 'sk-new-CAFEBABE1111';
export const SECRET_URL_VALUE_OLD = 'https://admin:hunter2OLD@svc.internal/api';
export const SECRET_URL_VALUE_NEW = 'https://admin:s3cr3tNEW@svc.internal/api';
```

Then add the trace-response fixture (anywhere after `traceResponse`):

```ts
// /trace for the d-drift-1 drift_issue: a decision doc carrying env diffs.
// LOG_LEVEL is non-secret (values shown); API_TOKEN is secret by NAME; ENDPOINT
// is secret by VALUE (credentialed URL) despite a non-secret name. The secret
// raw values live ONLY in diffs[] (not the rationale), so the "no raw secret in
// DOM" assertion isolates the CARD's client-side redaction (PR 1's concern).
// The raw-rationale scrub is PR 2 (backend) and is not exercised here.
export function driftCardTraceResponse() {
  return {
    trace_id: DRIFT_CARD_TRACE_ID,
    events: [],
    decision: {
      decision_id: 'd-drift-1',
      trace_id: DRIFT_CARD_TRACE_ID,
      action: 'drift_issue',
      rationale: 'Three variables drifted from the ops contract; secret values are redacted in the table below.',
      github: { url: 'https://github.com/acme/ops/issues/99', dry_run: false },
      diffs: [
        { name: 'LOG_LEVEL', expected: 'info', live: 'debug', contract_status: 'present_allow_manual' },
        { name: 'API_TOKEN', expected: SECRET_TOKEN_VALUE_OLD, live: SECRET_TOKEN_VALUE_NEW, contract_status: 'present_disallow_manual' },
        { name: 'ENDPOINT', expected: SECRET_URL_VALUE_OLD, live: SECRET_URL_VALUE_NEW, contract_status: 'absent' },
      ],
    },
    complete: true,
  };
}
```

**Step 2: Route the drift trace in the smoke harness**

In `frontend/tests/smoke/transparency.smoke.ts`, extend the import from `./fixtures` to include `DRIFT_CARD_TRACE_ID`, `driftCardTraceResponse`, `SECRET_TOKEN_VALUE_OLD`, `SECRET_TOKEN_VALUE_NEW`, `SECRET_URL_VALUE_OLD`, `SECRET_URL_VALUE_NEW`.

Then update the `**/trace/**` route handler (currently `transparency.smoke.ts:47-58`) to branch the new trace id FIRST:

```ts
  await page.route('**/trace/**', (route: Route) => {
    const url = route.request().url();
    const body = url.includes(DRIFT_CARD_TRACE_ID)
      ? driftCardTraceResponse()
      : url.includes(IAC_TRACE_ID)
        ? iacTraceResponse()
        : traceResponse();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
```

**Step 3: Add the smoke test**

Append a new test inside the `test.describe('transparency UI (mock smoke)', ...)` block:

```ts
  test('drift decision: env-diff card shows non-secret values, redacts secret-named + credentialed-URL values, leaks no raw secret', async ({ page }) => {
    await seedToken(page);
    await mockData(page, freshState());
    await page.goto('/ui/transparency');

    // Open d-drift-1 specifically. Filter by its exact github href so the
    // selector is unambiguous even if another row later also renders a link.
    await page
      .locator(`[data-testid="${TESTIDS.pastDecisionItem}"]`)
      .filter({ has: page.locator('a[data-testid="decision-github-link"][href="https://github.com/acme/ops/issues/99"]') })
      .locator(`[data-testid="${TESTIDS.openTraceButton}"]`)
      .click();

    const card = page.getByTestId('drift-diff-card');
    await expect(card).toBeVisible();

    // Non-secret var: both values shown verbatim.
    const logRow = card.locator('tr', { hasText: 'LOG_LEVEL' });
    await expect(logRow).toContainText('info');
    await expect(logRow).toContainText('debug');

    // Secret-by-NAME and secret-by-VALUE rows show the redaction marker.
    await expect(card.locator('tr', { hasText: 'API_TOKEN' })).toContainText('(value redacted: secret-like)');
    await expect(card.locator('tr', { hasText: 'ENDPOINT' })).toContainText('(value redacted: secret-like)');

    // Hard guarantee: no raw diff secret value appears anywhere in the rendered DOM —
    // checked both as serialized HTML (attributes included) and as visible text.
    const html = await page.content();
    const body = page.locator('body');
    for (const secret of [
      SECRET_TOKEN_VALUE_OLD, SECRET_TOKEN_VALUE_NEW, SECRET_URL_VALUE_OLD, SECRET_URL_VALUE_NEW,
    ]) {
      expect(html, `raw secret must not appear in DOM html: ${secret}`).not.toContain(secret);
      await expect(body, `raw secret must not appear in body text: ${secret}`).not.toContainText(secret);
    }
  });
```

**Step 4: Run the smoke suite**

Run: `cd frontend && npm run build && npm run test:smoke`
Expected: all smoke tests PASS (the new one + the existing 9). The build is required first — `test:smoke` boots the real FastAPI app serving the freshly-built bundle from `agent/static`.

**Step 5: Commit**

```bash
git add frontend/tests/smoke/fixtures.ts frontend/tests/smoke/transparency.smoke.ts
git commit -m "test(ui): smoke the env-diff card — values shown, secrets redacted, no raw leak"
```

---

### Task 7: Final verification gate

**Step 1: Run the full frontend gate**

```bash
cd frontend && npm run check && npx vitest run && npm run build && npm run test:smoke
```

Expected:
- `npm run check` — 0 errors, 0 warnings.
- `npx vitest run` — all unit tests pass (223 prior + the new `secret_guard.test.ts` + `diff.test.ts` cases, incl. the redact-before-clamp cases).
- `npm run build` — clean.
- `npm run test:smoke` — all smoke tests pass (10 total; the new one asserts the card's client-side diff redaction + no raw diff secret in the DOM).

**Step 2: Self-review the diff**

```bash
git diff main...HEAD --stat
git log --oneline main..HEAD
```

Confirm: no backend file touched (`agent/`, `driftscribe_lib/` untouched); no `agent/static` artifact committed; only `frontend/src/lib/{types,secret_guard,diff}.ts`, `frontend/src/components/DriftDiffCard.svelte`, `frontend/src/App.svelte`, the two test files, the two smoke files, and this plan doc changed.

---

## Out of scope (do NOT build in PR 1)

- **Backend `rationale` scrub — this is PR 2, a separate PR.** Scrub the served `rationale` in `/trace` + `/decisions` (root-cause fix covering the Svelte SPA, the legacy `/ui/transparency-legacy` route, and any API consumer). PR 1 deliberately does NOT scrub rationale client-side. PR 2 needs a coordinator redeploy; PR 1 does not.
- **Backend redaction of `diffs[]` in `/trace` / `/decisions`.** The decision doc keeps `diffs[]` unredacted by design; this card redacts at the display layer, matching the GitHub artifact. Changing the served `diffs[]` would break the card's ability to show non-secret drift.
- `recent_pr_match` / `debug_config_value` columns (the backend evidence table has them; YAGNI for v1 — the operator can open the GitHub link for full detail).
- A `redacted: bool` per-diff field shipped from the backend (rejected in the brainstorm: schema change + backfill cost; the client-side mirror is sufficient and pinned by a parity test).
- Phase 2 (dry-run pill) — tracked separately in `docs/plans/2026-06-08-decision-artifact-links.md`.
