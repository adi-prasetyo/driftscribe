# Crew System-Prompt Viewer (read-only) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let an operator (or anonymous demo visitor) read each crew's *actual* system prompt(s) in-app, from a read-only disclosure inside the Capabilities card — no editing, no runtime mutation.

**Architecture:** A new open (no-auth) backend endpoint `GET /workloads/{name}/prompts` serves a crew's resolved prompt text straight from the baked-in repo files, stamped with the running Cloud Run revision and a demo note. The SPA's `CapabilityCard` gets a per-crew nested `<details>` disclosure that lazy-fetches that endpoint when opened and renders the prompt(s) in an escaped, scroll-capped `<pre>`. This mirrors the existing PR #139 in-app ".tf source view" pattern (open GET, fail-soft, demo note) but for prompts, and keeps prompt text *out* of the `/capabilities` DTO so its env-free invariant is preserved.

**Tech Stack:** FastAPI + Pydantic (coordinator, `agent/`), Svelte 5 runes + Vite SPA (`frontend/`), pytest (`tests/`), Vitest + @testing-library/svelte (`frontend/tests/`).

---

## Decisions & non-goals (read before starting)

- **Read-only only.** The prompt is NOT editable from the UI. Runtime-editing would put config drift inside a drift detector, create a stored-prompt-injection surface, and fight the byte-for-byte golden tests that pin these files. An "edit → opens a PR against `workloads/<name>/*.md`" affordance is a possible *future* feature, explicitly out of scope here.
- **Per-crew lazy endpoint, not a `/capabilities` extension.** `build_capabilities()` calls the env-free `load_workload_spec()` and is pinned by `test_build_capabilities_is_json_serializable_and_env_free`. Inlining ~28 KB of prompt text across four crews into every capabilities fetch would bloat it and risk that invariant. A separate endpoint fetched only when a crew's disclosure opens is pay-for-what-you-use.
- **Open (no token).** Mirrors the `/iac-approvals/{pr}` GET and `/runs/{id}`. The repo is public, so the prompts are already public — showing them leaks nothing and is the whole point.
- **Demo / Worker reachability (verified — do NOT change the CF Worker).** The anonymous-demo path goes browser → CF Worker (`infra/cloudflare/worker/src/proxy.js`) → origin. That Worker is a **pass-through** proxy: it always forwards to origin (`proxy.js:132`) and injects `DEMO_TOKEN` only for an explicit allowlist (`proxy.js:46-55`). `GET /capabilities` **is** allowlisted (`proxy.js:50`), so the Capabilities card already renders for anonymous demo visitors (the demo token is injected for that call). Our new endpoint is **open**, so a sanitized (token-stripped) anonymous request passes straight through to it and returns 200 — **no allowlist entry is required**, and adding one would be wrong (it would inject the operator token into a route that doesn't need it). Net: the disclosure is reachable for anon-demo, operator-token, and CF-Access operators alike, with zero Worker change. (This pre-empts the natural "but `/capabilities` is token-gated" objection: in the demo path the Worker supplies that token; in the operator path the operator does.)
- **Revision stamp via `K_REVISION`.** Cloud Run auto-injects `K_REVISION` (e.g. `driftscribe-agent-00094-7cr`); reading `os.environ.get("K_REVISION", "local")` needs **no cloudbuild/env change**. We do not bake a `GIT_SHA` (avoids a deploy-config change for marginal gain).
- **No size cap / no Firestore cache.** Unlike the `.tf` view (semi-untrusted GitHub content, Firestore-bound), these are our own small repo files read from local disk. `Path.read_text(encoding="utf-8")` is strict-UTF-8 by default. A cap/cache is YAGNI.
- **Missing-file behavior:** the prompt files are guaranteed present by golden tests (`test_drift_workload_loads.py`) and the Dockerfile `COPY workloads/`. If a read genuinely fails it is a deploy corruption, not a transient — we let it surface as a 500 rather than masking it with a fake-empty 200. (Contrast the `.tf` view, which fails soft because GitHub fetches can transiently fail.)
- **Scope:** all four crews (`drift`/Anchor, `upgrade`/Patch, `explore`/Explore, `provision`/Provision). `explore` and `provision` have no separate chat prompt (`chat_system_prompt_file` unset → falls back to the recheck prompt); the endpoint reports `chat_prompt_distinct: false` and `chat_prompt: null` rather than duplicating the text.

---

## Task 1: Env-free prompt resolver `resolve_workload_prompts(name)`

**Files:**
- Modify: `agent/workloads/registry.py` (add a small dataclass + function near `load_workload_spec` / `_load_from_path`)
- Test: `tests/unit/test_resolve_workload_prompts.py` (create)

This is the unit that reads a crew's prompt file(s) without resolving worker URL env vars (so it can't raise `MissingWorkerEnvError` and is testable env-free). It **reuses the existing guarded resolvers** — `_workload_yaml_path(name)` (which owns the path-traversal/existence checks) and `_parse_spec(yaml_path, expected_name=name)` — exactly as `load_workload_spec` does (`registry.py:836`), then reads the prompt files relative to `yaml_path.parent` with the SAME missing-file error messages `_load_from_path` uses (`registry.py:865-895`). It does NOT compute a second workloads-root path, and it leaves the hot `_load_from_path` untouched (the ~6 lines of read logic are intentionally duplicated to keep blast radius off that path; the `test_drift_workload_loads.py` golden tests guard both).

**Step 1: Write the failing test**

```python
# tests/unit/test_resolve_workload_prompts.py
"""Unit tests for the env-free prompt resolver behind GET /workloads/{name}/prompts."""
import pytest

from agent.workloads.registry import resolve_workload_prompts


def test_drift_has_distinct_chat_prompt():
    p = resolve_workload_prompts("drift")
    assert p.chat_prompt_distinct is True
    assert p.chat_prompt is not None
    assert p.recheck_prompt.strip()
    assert p.chat_prompt.strip()
    # The two drift prompts are genuinely different files.
    assert p.recheck_prompt != p.chat_prompt


def test_upgrade_has_distinct_chat_prompt():
    p = resolve_workload_prompts("upgrade")
    assert p.chat_prompt_distinct is True
    assert p.chat_prompt is not None


@pytest.mark.parametrize("name", ["explore", "provision"])
def test_chat_only_workloads_share_one_prompt(name):
    p = resolve_workload_prompts(name)
    assert p.chat_prompt_distinct is False
    assert p.chat_prompt is None
    assert p.recheck_prompt.strip()


def test_unknown_workload_raises():
    with pytest.raises(Exception):
        resolve_workload_prompts("nope")
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_resolve_workload_prompts.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_workload_prompts'`.

**Step 3: Write minimal implementation**

In `agent/workloads/registry.py`, add (reuse the existing `_workload_yaml_path` + `_parse_spec`; `dataclasses` is already imported there — confirm and don't double-import):

```python
@dataclasses.dataclass(frozen=True)
class WorkloadPrompts:
    """Resolved prompt text for one workload — env-free (no worker URLs)."""
    recheck_prompt: str
    chat_prompt: str | None       # None when no distinct chat prompt file exists
    chat_prompt_distinct: bool


def resolve_workload_prompts(name: str) -> WorkloadPrompts:
    """Read a workload's prompt file(s) directly, without resolving worker
    URL env vars (so this never raises MissingWorkerEnvError and is testable
    env-free). Reuses the same guarded path resolver as ``load_workload_spec``
    and mirrors the missing-file messages in ``_load_from_path``.
    """
    yaml_path = _workload_yaml_path(name)          # owns traversal/existence guard; raises on unknown name
    spec = _parse_spec(yaml_path, expected_name=name)
    workload_dir = yaml_path.parent

    prompt_path = workload_dir / spec.system_prompt_file
    if not prompt_path.exists():
        raise FileNotFoundError(
            f"system prompt for workload {spec.name!r} not found: {prompt_path}"
        )
    recheck = prompt_path.read_text(encoding="utf-8")

    if spec.chat_system_prompt_file is not None:
        chat_path = workload_dir / spec.chat_system_prompt_file
        if not chat_path.exists():
            raise RuntimeError(
                f"chat system prompt for workload {spec.name!r} not found: "
                f"{chat_path} (declared via chat_system_prompt_file in {yaml_path})"
            )
        return WorkloadPrompts(
            recheck_prompt=recheck,
            chat_prompt=chat_path.read_text(encoding="utf-8"),
            chat_prompt_distinct=True,
        )
    return WorkloadPrompts(recheck_prompt=recheck, chat_prompt=None, chat_prompt_distinct=False)
```

> Implementer note: `_workload_yaml_path(name)` already raises on an unknown name (closed set) — that satisfies `test_unknown_workload_raises`. Do NOT recompute the workloads root; `yaml_path.parent` IS the workload dir, exactly as `_load_from_path` uses it (`registry.py:868`).

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_resolve_workload_prompts.py -q`
Expected: PASS (5 tests).

**Step 5: Commit**

```bash
git add agent/workloads/registry.py tests/unit/test_resolve_workload_prompts.py
git commit -m "feat(prompts): env-free resolve_workload_prompts() helper"
```

---

## Task 2: Open endpoint `GET /workloads/{name}/prompts`

**Files:**
- Modify: `agent/main.py` (add a demo-note constant + the route; near the other open GETs)
- Test: `tests/integration/test_workload_prompts_endpoint.py` (create)

**Step 1: Write the failing test**

```python
# tests/integration/test_workload_prompts_endpoint.py
"""GET /workloads/{name}/prompts — open, read-only crew prompt view."""
import pytest
from fastapi.testclient import TestClient

from agent.main import app


def test_drift_prompts_ok_and_shaped():
    r = TestClient(app).get("/workloads/drift/prompts")
    assert r.status_code == 200
    b = r.json()
    assert b["workload"] == "drift"
    assert b["display_name"] == "Anchor"
    assert b["descriptor"]
    assert b["recheck_prompt"].strip()
    assert b["chat_prompt_distinct"] is True
    assert b["chat_prompt"].strip()
    assert b["source_dir"] == "workloads/drift"
    assert b["revision"]                  # K_REVISION or "local"
    assert "demo" in b["demo_note"].lower()


def test_explore_prompts_single():
    b = TestClient(app).get("/workloads/explore/prompts").json()
    assert b["chat_prompt_distinct"] is False
    assert b["chat_prompt"] is None
    assert b["recheck_prompt"].strip()


def test_unknown_workload_404():
    assert TestClient(app).get("/workloads/nope/prompts").status_code == 404


@pytest.mark.no_auth_override
def test_prompts_open_without_token(monkeypatch):
    from agent.config import get_settings
    monkeypatch.setenv("DRIFTSCRIBE_TOKEN", "tok-prompts-123")
    get_settings.cache_clear()
    r = TestClient(app).get("/workloads/drift/prompts")   # no X-DriftScribe-Token header
    assert r.status_code == 200
    assert r.status_code not in (401, 403)
```

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_workload_prompts_endpoint.py -q`
Expected: FAIL — 404 from FastAPI for an unregistered route (all asserts fail).

**Step 3: Write minimal implementation**

In `agent/main.py`. Imports to confirm/add: `os`, `HTTPException`, `Response` (likely already imported); `from agent.capabilities import WORKLOAD_NAMES` (it's defined there at `capabilities.py:49`); and `from agent.workloads.registry import load_workload_spec, resolve_workload_prompts` (match the module's existing import style for the registry).

```python
_PROMPTS_DEMO_NOTE = (
    "Demo: each crew's system prompt is shown to everyone here so judges can read "
    "exactly what instructions the agent runs under. The prompts are baked into the "
    "running image from the public repo — and they are soft guidance: the "
    "deterministic post-LLM validators, the fail-closed denylist, and the human "
    "approval gates (not the prompt) are the real safety boundary."
)


@app.get("/workloads/{name}/prompts")
def get_workload_prompts(name: str, response: Response) -> dict:
    """Open, read-only view of a crew's system prompt(s).

    No auth — mirrors the /iac-approvals GET and /runs: the prompts are baked
    from the public repo, so there is nothing to hide and showing them is the
    feature. Served from local disk (no GitHub fetch, no cache); the prompt is
    NOT the enforcement boundary (see the demo note).
    """
    if name not in WORKLOAD_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown workload {name!r}")
    spec = load_workload_spec(name)
    prompts = resolve_workload_prompts(name)
    response.headers["cache-control"] = "no-store"
    return {
        "workload": spec.name,
        "display_name": spec.display_name,
        "descriptor": spec.descriptor,
        "recheck_prompt": prompts.recheck_prompt,
        "chat_prompt": prompts.chat_prompt,
        "chat_prompt_distinct": prompts.chat_prompt_distinct,
        "source_dir": f"workloads/{spec.name}",
        "revision": os.environ.get("K_REVISION", "local"),
        "demo_note": _PROMPTS_DEMO_NOTE,
    }
```

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/integration/test_workload_prompts_endpoint.py -q`
Expected: PASS (4 tests).

**Step 5: Commit**

```bash
git add agent/main.py tests/integration/test_workload_prompts_endpoint.py
git commit -m "feat(prompts): open GET /workloads/{name}/prompts read-only endpoint"
```

---

## Task 3: Frontend type + parser `parseWorkloadPrompts`

**Files:**
- Create: `frontend/src/lib/prompts.ts`
- Test: `frontend/tests/unit/prompts.test.ts` (create)

**Step 1: Write the failing test**

```ts
// frontend/tests/unit/prompts.test.ts
import { describe, it, expect } from 'vitest';
import { parseWorkloadPrompts } from '../../src/lib/prompts';

const OK = {
  workload: 'drift', display_name: 'Anchor', descriptor: 'Cloud Run config',
  recheck_prompt: 'a', chat_prompt: 'b', chat_prompt_distinct: true,
  source_dir: 'workloads/drift', revision: 'driftscribe-agent-00094-7cr',
  demo_note: 'Demo: ...',
};

describe('parseWorkloadPrompts', () => {
  it('accepts a well-formed payload', () => {
    expect(parseWorkloadPrompts(OK)?.display_name).toBe('Anchor');
  });
  it('accepts null chat_prompt when not distinct', () => {
    const p = parseWorkloadPrompts({ ...OK, chat_prompt: null, chat_prompt_distinct: false });
    expect(p?.chat_prompt).toBeNull();
    expect(p?.chat_prompt_distinct).toBe(false);
  });
  it('rejects a non-object / missing required fields', () => {
    expect(parseWorkloadPrompts(null)).toBeNull();
    expect(parseWorkloadPrompts({ workload: 'drift' })).toBeNull();
  });
  it('rejects the inconsistent distinct=true + chat_prompt=null payload', () => {
    expect(parseWorkloadPrompts({ ...OK, chat_prompt: null, chat_prompt_distinct: true })).toBeNull();
  });
  it('tolerates distinct=false + a non-null chat_prompt (renders as single-prompt)', () => {
    const p = parseWorkloadPrompts({ ...OK, chat_prompt: 'leftover', chat_prompt_distinct: false });
    expect(p).not.toBeNull();
    expect(p?.chat_prompt_distinct).toBe(false);
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/prompts.test.ts`
Expected: FAIL — cannot resolve `../../src/lib/prompts`.

**Step 3: Write minimal implementation**

```ts
// frontend/src/lib/prompts.ts
export interface WorkloadPrompts {
  workload: string;
  display_name: string;
  descriptor: string;
  recheck_prompt: string;
  chat_prompt: string | null;
  chat_prompt_distinct: boolean;
  source_dir: string;
  revision: string;
  demo_note: string;
}

export function parseWorkloadPrompts(body: unknown): WorkloadPrompts | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;
  const str = (k: string) => (typeof b[k] === 'string' ? (b[k] as string) : null);
  const workload = str('workload');
  const display_name = str('display_name');
  const recheck_prompt = str('recheck_prompt');
  if (workload === null || display_name === null || recheck_prompt === null) return null;
  if (typeof b.chat_prompt_distinct !== 'boolean') return null;
  const chat_prompt = b.chat_prompt === null ? null : str('chat_prompt');
  if (b.chat_prompt_distinct && chat_prompt === null) return null;
  return {
    workload, display_name,
    descriptor: str('descriptor') ?? '',
    recheck_prompt,
    chat_prompt,
    chat_prompt_distinct: b.chat_prompt_distinct,
    source_dir: str('source_dir') ?? '',
    revision: str('revision') ?? '',
    demo_note: str('demo_note') ?? '',
  };
}
```

**Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run tests/unit/prompts.test.ts`
Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add frontend/src/lib/prompts.ts frontend/tests/unit/prompts.test.ts
git commit -m "feat(prompts): WorkloadPrompts type + parser"
```

---

## Task 4: CapabilityCard per-crew prompt disclosure

**Files:**
- Modify: `frontend/src/components/CapabilityCard.svelte`
- Test: `frontend/tests/unit/CapabilityCard.test.ts` (extend)

Add per-crew lazy-fetched prompt state and a nested `<details class="ds-disclosure">` inside `cap-workload__body` (after the existing tools/workers/actions lists). Render the prompt(s) in `<pre class="ds-pre cap-prompt-pre">` (Svelte auto-escapes — never `{@html}`), with the demo note + a "running artifact · `source_dir` @ `revision`" line. Fail soft: a fetch error shows a calm subtle line, never a red error. Use the `file-text` icon (already registered — no new icon).

**Step 1: Write the failing test** (extend `CapabilityCard.test.ts`; mirror the existing multi-route `makeCallWithAutonomy` stub + the `.open = true` + `new Event('toggle')` disclosure pattern)

```ts
// Add to frontend/tests/unit/CapabilityCard.test.ts
it('lazy-loads and renders a crew prompt with distinct chat prompt', async () => {
  const PROMPTS = {
    workload: 'drift', display_name: 'Anchor', descriptor: 'Cloud Run config',
    recheck_prompt: 'RECHECK-PROMPT-TEXT', chat_prompt: 'CHAT-PROMPT-TEXT',
    chat_prompt_distinct: true, source_dir: 'workloads/drift',
    revision: 'driftscribe-agent-00094-7cr', demo_note: 'Demo: prompts are soft guidance.',
  };
  const call = async (path: string) => {
    if (path === '/capabilities') return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { 'content-type': 'application/json' } });
    if (path === '/autonomy') return new Response(JSON.stringify({ mode: 'propose_apply' }), { status: 200, headers: { 'content-type': 'application/json' } });
    if (path === '/workloads/drift/prompts') return new Response(JSON.stringify(PROMPTS), { status: 200, headers: { 'content-type': 'application/json' } });
    return new Response('not found', { status: 404 });
  };
  const { getByTestId } = render(CapabilityCard, { props: { call } });
  const card = getByTestId('capability-card') as HTMLDetailsElement;
  card.open = true; await fireEvent(card, new Event('toggle'));
  await waitFor(() => getByTestId('cap-workload-drift-summary'));
  const promptsDetails = getByTestId('cap-workload-drift-prompts') as HTMLDetailsElement;
  promptsDetails.open = true; await fireEvent(promptsDetails, new Event('toggle'));
  await waitFor(() => {
    expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('RECHECK-PROMPT-TEXT');
    expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('CHAT-PROMPT-TEXT');
    expect(getByTestId('cap-workload-drift-prompts').textContent).toContain('Demo: prompts are soft guidance.');
  });
});

it('fails soft when the prompt fetch errors (no red error, no throw)', async () => {
  const call = async (path: string) => {
    if (path === '/capabilities') return new Response(JSON.stringify(FIXTURE), { status: 200, headers: { 'content-type': 'application/json' } });
    if (path === '/autonomy') return new Response(JSON.stringify({ mode: 'propose_apply' }), { status: 200, headers: { 'content-type': 'application/json' } });
    return new Response('boom', { status: 500 });   // prompts fetch fails
  };
  const { getByTestId } = render(CapabilityCard, { props: { call } });
  const card = getByTestId('capability-card') as HTMLDetailsElement;
  card.open = true; await fireEvent(card, new Event('toggle'));
  await waitFor(() => getByTestId('cap-workload-drift-summary'));
  const pd = getByTestId('cap-workload-drift-prompts') as HTMLDetailsElement;
  pd.open = true; await fireEvent(pd, new Event('toggle'));
  await waitFor(() => expect(pd.textContent?.toLowerCase()).toContain('unavailable'));
});
```

**Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run tests/unit/CapabilityCard.test.ts`
Expected: FAIL — `cap-workload-drift-prompts` testid not found.

**Step 3: Write minimal implementation** — in `CapabilityCard.svelte`:

Script (add near the other `$state`; import `Icon`, `parseWorkloadPrompts`, type `WorkloadPrompts`):
```ts
import { parseWorkloadPrompts } from '../lib/prompts';
import type { WorkloadPrompts } from '../lib/prompts';

let promptsByName = $state<Record<string, WorkloadPrompts>>({});
let promptLoading = $state<Record<string, boolean>>({});
let promptError = $state<Record<string, boolean>>({});

async function onPromptsToggle(name: string, el: HTMLDetailsElement): Promise<void> {
  if (!el.open) return;
  // Load once; a fetch in flight blocks duplicates. A PRIOR error does NOT
  // block — closing and reopening the disclosure retries (transient failures
  // shouldn't require a page reload).
  if (promptsByName[name] || promptLoading[name]) return;
  promptLoading = { ...promptLoading, [name]: true };
  promptError = { ...promptError, [name]: false };
  try {
    const resp = await call('/workloads/' + encodeURIComponent(name) + '/prompts');
    if (!resp.ok) { promptError = { ...promptError, [name]: true }; return; }
    const parsed = parseWorkloadPrompts(await resp.json());
    if (!parsed) { promptError = { ...promptError, [name]: true }; return; }
    promptsByName = { ...promptsByName, [name]: parsed };
  } catch {
    promptError = { ...promptError, [name]: true };
  } finally {
    promptLoading = { ...promptLoading, [name]: false };
  }
}
```

Markup — inside `cap-workload__body`, after the actions list:
```svelte
<details
  class="ds-disclosure cap-workload__prompts"
  data-testid="cap-workload-{wl.name}-prompts"
  ontoggle={(e) => onPromptsToggle(wl.name, e.currentTarget as HTMLDetailsElement)}
>
  <summary class="cap-workload__prompts-summary">
    <Icon name="file-text" size={14} /> View system prompt{wl.name === 'drift' || wl.name === 'upgrade' ? 's' : ''}
  </summary>
  {#if promptError[wl.name]}
    <p class="ds-subtle">Prompt source is unavailable right now.</p>
  {:else if promptsByName[wl.name]}
    {@const p = promptsByName[wl.name]}
    <p class="ds-subtle" data-testid="cap-workload-{wl.name}-prompts-note">{p.demo_note}</p>
    <p class="ds-subtle">Running artifact · <code class="ds-code">{p.source_dir}</code> @ <code class="ds-code">{p.revision}</code></p>
    <div class="ds-field"><span class="ds-label">recheck prompt</span></div>
    <pre class="ds-pre cap-prompt-pre">{p.recheck_prompt}</pre>
    {#if p.chat_prompt_distinct && p.chat_prompt}
      <div class="ds-field"><span class="ds-label">chat prompt</span></div>
      <pre class="ds-pre cap-prompt-pre">{p.chat_prompt}</pre>
    {:else}
      <p class="ds-subtle">This crew has no separate chat prompt — it ships a single system prompt file.</p>
    {/if}
  {:else}
    <p class="ds-subtle">Loading…</p>
  {/if}
</details>
```

Scoped style (cap the height so a long prompt scrolls instead of dominating the card):
```css
.cap-prompt-pre { max-height: 24rem; overflow-y: auto; }
```

**Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run tests/unit/CapabilityCard.test.ts`
Expected: PASS (existing tests + 2 new).

**Step 5: Commit**

```bash
git add frontend/src/components/CapabilityCard.svelte frontend/tests/unit/CapabilityCard.test.ts
git commit -m "feat(prompts): per-crew read-only prompt disclosure in CapabilityCard"
```

---

## Task 5: Rebuild SPA bundle + full verification

**Files:**
- Modify: `agent/static/*` (generated bundle — `transparency-*.js` + css)

**Step 1: Build the SPA**

Run: `cd frontend && npm run build`
Expected: Vite emits a new `transparency-<hash>.js` into `agent/static/` (confirm the new hash; the old one is replaced).

Verify: `git status --short agent/static` shows the regenerated bundle/css changed.

**Step 2: Run the full frontend suite**

Run: `cd frontend && npx vitest run`
Expected: PASS (all suites, incl. `prompts.test.ts` and `CapabilityCard.test.ts`).

**Step 3: Run the full backend suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (incl. the two new test files; nothing regressed — especially `test_capabilities.py::test_build_capabilities_is_json_serializable_and_env_free`, which must stay green since we did NOT touch `build_capabilities`).

**Step 4: Optional local visual verify** (per the live-probe / visual-verify recipe in memory): rebuild, restart uvicorn, load `/`, open Capabilities → a crew → "View system prompt(s)", confirm the prompt renders escaped + scroll-capped and the demo note + revision show.

**Step 5: Commit the bundle**

```bash
git add agent/static
git commit -m "build(spa): rebuild bundle with crew prompt viewer"
```

---

## Deploy (post-merge, operator-gated — NOT part of code tasks)

This ships via a **coordinator** redeploy (the endpoint is backend, and the SPA bundle is served by the coordinator from `agent/static/`). Standard coordinator deploy: Cloud Build to build the coordinator image, then `gcloud run deploy` + **`update-traffic --to-revisions=<new>=100`** (traffic is pinned — see the coordinator-deploy memory). No worker images change. After deploy, record the new coordinator revision + rollback target, and live-verify `GET /workloads/drift/prompts` returns 200 with a real `K_REVISION` stamp.

---

## Test summary (what proves it works)

- `tests/unit/test_resolve_workload_prompts.py` — distinct (drift/upgrade) vs shared (explore/provision) prompt resolution, env-free.
- `tests/integration/test_workload_prompts_endpoint.py` — 200 shape, explore single-prompt, 404 unknown, **open without token**.
- `frontend/tests/unit/prompts.test.ts` — payload parse/validate incl. null chat prompt.
- `frontend/tests/unit/CapabilityCard.test.ts` — lazy load + render (distinct), fail-soft on fetch error.
- Full `pytest` + `vitest` green; `build_capabilities` env-free invariant untouched.
