# Pending-Approvals in the Infra Panel — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Surface open infra PRs awaiting approval inside the Infrastructure panel — a per-resource "Review pending adoption (PR #N) →" link that *replaces* the Adopt button (killing duplicate-adoption PRs), plus a panel-level "Awaiting your approval (N)" band — and refuse a duplicate adoption at the tool layer.

**Architecture:** A new additive, fail-soft `GET /infra/pending-approvals` endpoint lists open PRs labeled `driftscribe-infra`, parsing each PR body's deterministic `**Import id:** ...` line to recover the adopted resource (no per-PR `.tf` fetch). The SPA fetches it alongside `/infra/graph` and joins client-side by `(asset_type, resource_name)`. `/infra/graph` itself is **untouched** (its L1/L2 cache is delicate). The list self-clears (applied PRs merge → leave `state="open"`) and works retroactively for #168 (no backfill). `propose_adoption_tool` gains a best-effort open-PR guard so even a chat-driven adoption can't dupe.

**Tech Stack:** Python 3.12 / FastAPI / PyGithub (backend), Svelte 5 runes / Vitest (frontend), pytest.

---

## Background facts (verified 2026-06-30)

- Every infra PR carries the GitHub label `driftscribe-infra` (`driftscribe_lib/github.py:273,374`). #168 confirmed labeled.
- Adoption PR bodies are deterministically rendered by `driftscribe_lib/adopt_recipe.py` and contain a line `**Import id:** \`<import_id>\`` (`adopt_recipe.py:510`). #168 body confirmed: `**Import id:** \`projects/driftscribe-hack-2026/topics/adopt-probe-topic\``.
- Import-id shapes per HCL type live in `adopt_recipe._ID_SHAPES` (`adopt_recipe.py:96-102`); HCL-type → CAI asset_type in `driftscribe_lib/infra_graph.PLAN_RTYPE_TO_ASSET_TYPE` (`infra_graph.py:44-48`).
- Infra-graph nodes carry `{name, asset_type}` (`infra_graph.py:239`) — the client-side join key. `name` is the short resource name (e.g. `adopt-probe-topic`).
- The adopt button lives in `frontend/src/components/InfraDiagram.svelte:641-651` (`row.adoptable` → `clickAdopt(row.prefill)`). The legend hover copy is `LEGEND_HELP` (`InfraDiagram.svelte:119-128`). Status-dot tint = `dotClass` (`InfraDiagram.svelte:161-163`).
- `iacApprovalHref(prNumber)` → same-origin `/iac-approvals/<n>` (`frontend/src/lib/approval.ts:58`).
- Live `/decisions` has NO row for #168 (it's never been approved); #168 is `OPEN`, not merged — this is the whole reason the feature exists.

## Known v1 simplifications + assumptions (flag, don't over-build here)

- **Band copy / terminal-failed PRs — RESOLVED: honest-neutral header "Open infra changes (N)".** The band lists every OPEN infra PR. A PR whose apply terminally failed (`failed_state_suspect`) is still open on GitHub; the neutral header is accurate for every state (pending, plan-building, terminally-failed-but-open), so NO `/decisions` cross-reference is needed and the band never implies a phantom approval (DriftScribe's PR #176 norm). The per-card link copy stays specific and honest ("Review pending adoption (PR #N) →").
- **Trusted-label assumption.** Resource identity is parsed from the PR body's `**Import id:**` line on PRs labeled `driftscribe-infra`. Only the tofu-editor worker applies that label (`github.py:374`), so a forged import-id line would require repo write + the label — acceptable in this single-tenant deployment. A future hardening is a hidden, machine-only marker (`<!-- driftscribe:adopt import_id=... -->`) rather than human-readable prose (Codex finding 2). The dupe-guard's worst case from a forged line is a *false refusal* of a legitimate adoption (fail-safe direction), not a bad apply.
- Resource join only succeeds for PRs with a parseable import-id line (adoptions). Freehand / new-resource infra PRs appear in the band only (no card to attach to) — correct by design.

---

## Task 1: Pure pending-approval parsing helpers

**Files:**
- Create: `driftscribe_lib/pending_approvals.py`
- Test: `tests/lib/test_pending_approvals.py`

**Step 1: Write the failing tests**

```python
# tests/lib/test_pending_approvals.py
import pytest
from driftscribe_lib.pending_approvals import (
    extract_import_id,
    import_id_to_resource,
    build_pending_approval,
)


def test_extract_import_id_from_adoption_body():
    body = "Adopts a topic.\n\n**Import id:** `projects/p/topics/adopt-probe-topic`\n\nmore"
    assert extract_import_id(body) == "projects/p/topics/adopt-probe-topic"


def test_extract_import_id_missing_returns_none():
    assert extract_import_id("a freehand PR body with no import line") is None
    assert extract_import_id("") is None
    assert extract_import_id(None) is None


@pytest.mark.parametrize(
    "import_id, asset_type, name",
    [
        ("projects/p/topics/t1", "pubsub.googleapis.com/Topic", "t1"),
        ("projects/p/subscriptions/s1", "pubsub.googleapis.com/Subscription", "s1"),
        ("projects/p/locations/asia/services/svc", "run.googleapis.com/Service", "svc"),
        ("my-bucket", "storage.googleapis.com/Bucket", "my-bucket"),
    ],
)
def test_import_id_to_resource(import_id, asset_type, name):
    assert import_id_to_resource(import_id) == (asset_type, name)


def test_import_id_to_resource_unrecognized_returns_none():
    assert import_id_to_resource("projects/p/widgets/w") is None
    assert import_id_to_resource("") is None


def test_build_pending_approval_adoption():
    body = "x\n\n**Import id:** `projects/p/topics/adopt-probe-topic`\n"
    out = build_pending_approval(168, "Adopt topic", "https://gh/pr/168", body)
    assert out == {
        "pr_number": 168,
        "title": "Adopt topic",
        "url": "https://gh/pr/168",
        "asset_type": "pubsub.googleapis.com/Topic",
        "resource_name": "adopt-probe-topic",
    }


def test_build_pending_approval_freehand_has_blank_resource():
    out = build_pending_approval(170, "Add monitoring", "https://gh/pr/170", "no import")
    assert out["pr_number"] == 170
    assert out["asset_type"] == ""
    assert out["resource_name"] == ""
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/lib/test_pending_approvals.py -v`
Expected: FAIL (module not found).

**Step 3: Implement**

```python
# driftscribe_lib/pending_approvals.py
"""Pure helpers for the operator-facing "awaiting your approval" surface.

No I/O: given a PR's number/title/url/body, derive the adopted resource (when the
PR is an adoption, whose body carries the deterministic ``**Import id:** `...` ``
line rendered by :mod:`driftscribe_lib.adopt_recipe`). The GitHub listing lives in
the agent layer; this module stays import-light and unit-testable.
"""
from __future__ import annotations

import re

# Depend ONLY on adopt_recipe (not infra_graph): _ID_SHAPES + _RTYPE_TO_ASSET_TYPE
# are co-located there for exactly the 4 adoptable types and are drift-pinned by
# adopt_recipe's own tests. Using infra_graph.PLAN_RTYPE_TO_ASSET_TYPE would couple
# this parser to the display-graph module and risk future non-adoptable mappings
# leaking in (Codex review finding 1).
from driftscribe_lib.adopt_recipe import _ID_SHAPES, _RTYPE_TO_ASSET_TYPE

# Matches the body line adopt_recipe renders: ``**Import id:** `<id>` `` (the id is
# inside single backticks). Tolerant of surrounding whitespace; first match wins.
_IMPORT_ID_RE = re.compile(r"\*\*Import id:\*\*\s*`([^`]+)`")


def extract_import_id(pr_body: str | None) -> str | None:
    """The import id from an adoption PR body, or None if absent/empty."""
    if not pr_body:
        return None
    m = _IMPORT_ID_RE.search(pr_body)
    return m.group(1) if m else None


def import_id_to_resource(import_id: str) -> tuple[str, str] | None:
    """Reverse an adoption import id to ``(asset_type, resource_name)``.

    Uses the SAME shape regexes the renderer/static-gate enforce
    (:data:`adopt_recipe._ID_SHAPES`), so an id this accepts is exactly one the
    pipeline could have produced. ``resource_name`` is the bare short name (the
    last path segment), matching the infra-graph node ``name``. Returns None for
    an id matching no adoptable shape.
    """
    if not import_id:
        return None
    for rtype, shape in _ID_SHAPES.items():
        if shape.fullmatch(import_id):
            asset_type = _RTYPE_TO_ASSET_TYPE.get(rtype)
            if not asset_type:
                return None
            name = import_id.rsplit("/", 1)[-1]
            return (asset_type, name)
    return None


def build_pending_approval(
    pr_number: int, title: str, url: str, pr_body: str | None
) -> dict:
    """A single pending-approval DTO. ``asset_type``/``resource_name`` are blank
    when the PR is not a parseable adoption (freehand/new-resource infra PR)."""
    asset_type = ""
    resource_name = ""
    import_id = extract_import_id(pr_body)
    if import_id:
        resolved = import_id_to_resource(import_id)
        if resolved is not None:
            asset_type, resource_name = resolved
    return {
        "pr_number": pr_number,
        "title": title,
        "url": url,
        "asset_type": asset_type,
        "resource_name": resource_name,
    }
```

> Both `_ID_SHAPES` and `_RTYPE_TO_ASSET_TYPE` live in `adopt_recipe.py` (lines 96 and 107) and are already drift-pinned there, so there is no cycle and no infra_graph dependency.

**Step 4: Run to verify pass**

Run: `uv run pytest tests/lib/test_pending_approvals.py -v` → PASS

**Step 5: Commit**

```bash
git add driftscribe_lib/pending_approvals.py tests/lib/test_pending_approvals.py
git commit -m "feat(pending-approvals): pure import-id parser + resource mapping"
```

---

## Task 2: `GET /infra/pending-approvals` endpoint

**Files:**
- Modify: `agent/main.py` (add lister + endpoint near `get_infra_graph`, ~`main.py:2493`)
- Test: `tests/agent/test_pending_approvals_endpoint.py`

**Step 1: Write the failing tests** (fake repo, no network)

```python
# tests/agent/test_pending_approvals_endpoint.py
from types import SimpleNamespace
from fastapi.testclient import TestClient
import agent.main as main


def _issue(number, title, body, *, is_pr, html_url="https://gh/x"):
    # `labels` filtering is server-side now, so the fake returns already-labeled
    # items (as GitHub would). `pull_request` is truthy iff the issue is a PR.
    return SimpleNamespace(
        number=number, title=title, body=body, html_url=html_url,
        pull_request=SimpleNamespace() if is_pr else None,
    )


def test_lists_open_infra_adoption_prs(monkeypatch, client_with_token):
    # As GitHub would return for labels=[driftscribe-infra], state=open:
    issues = [
        _issue(168, "Adopt topic", "**Import id:** `projects/p/topics/adopt-probe-topic`", is_pr=True),
        _issue(169, "Tracking issue", "not a PR", is_pr=False),  # issue, not a PR → excluded
        _issue(171, "Add alerting", "freehand body", is_pr=True),  # infra PR, no resource
    ]
    fake_repo = SimpleNamespace(get_issues=lambda **kw: issues)
    monkeypatch.setattr(main, "get_repo", lambda *a, **k: fake_repo)

    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    body = r.json()
    nums = {a["pr_number"] for a in body["approvals"]}
    assert nums == {168, 171}
    a168 = next(a for a in body["approvals"] if a["pr_number"] == 168)
    assert a168["asset_type"] == "pubsub.googleapis.com/Topic"
    assert a168["resource_name"] == "adopt-probe-topic"
    assert body.get("degraded") in (False, None)


def test_github_failure_degrades_soft(monkeypatch, client_with_token):
    def boom(*a, **k): raise RuntimeError("github down")
    monkeypatch.setattr(main, "get_repo", boom)
    r = client_with_token.get("/infra/pending-approvals")
    assert r.status_code == 200
    assert r.json() == {"approvals": [], "degraded": True}


def test_requires_token(client_no_token):
    assert client_no_token.get("/infra/pending-approvals").status_code in (401, 403)
```

> Reuse existing test fixtures for `client_with_token` / `client_no_token` (grep `tests/agent/` for the `/decisions` or `/infra/graph` tests — copy their token-header fixture).

**Step 2: Run → FAIL** (`uv run pytest tests/agent/test_pending_approvals_endpoint.py -v`)

**Step 3: Implement** (in `agent/main.py`)

```python
# near the other module-level caches (e.g. _INFRA_INVENTORY_CACHE)
_PENDING_APPROVALS_CACHE: tuple[float, list[dict]] | None = None
_PENDING_APPROVALS_TTL_S = 60.0
_INFRA_PR_LABEL = "driftscribe-infra"


def _list_pending_approvals() -> list[dict]:
    """Open infra PRs awaiting approval, newest first. Raises on GitHub error
    (the endpoint maps that to a degraded 200).

    Uses the issues API with a SERVER-SIDE label filter (Codex review finding 4):
    ``get_issues(state="open", labels=[driftscribe-infra])`` returns only the
    labeled items, and a PR is an issue whose ``.pull_request`` is set. The issue
    object already carries ``number/title/body/html_url`` (a PR's body IS its
    issue body), so NO per-PR ``get_pull`` round-trip is needed.
    """
    from driftscribe_lib.pending_approvals import build_pending_approval

    s = get_settings()
    repo = get_repo(s.github_token or None, s.github_repo)
    out: list[dict] = []
    # PyGithub accepts label NAMES (strings) here; it resolves them to the GitHub
    # label query param. (If the pinned PyGithub ever requires Label objects,
    # fetch once via repo.get_label(_INFRA_PR_LABEL).)
    for issue in repo.get_issues(state="open", labels=[_INFRA_PR_LABEL]):
        if getattr(issue, "pull_request", None) is None:
            continue  # a real issue, not a PR
        out.append(
            build_pending_approval(
                issue.number, issue.title or "", issue.html_url or "", issue.body or ""
            )
        )
    # get_issues default sort is created-desc, so `out` is already newest-first.
    return out


@app.get("/infra/pending-approvals")
def get_pending_approvals(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """Open infra PRs awaiting operator approval, for the Infra panel.

    Additive + fail-soft: a GitHub error returns ``{"approvals": [], "degraded":
    True}`` (never a 5xx) so the panel degrades gracefully. Short in-process TTL
    cache (the list changes slowly and the panel polls). ``Cache-Control: no-store``.
    Token-guarded exactly like ``/infra/graph`` and ``/decisions``.
    """
    global _PENDING_APPROVALS_CACHE
    response.headers["Cache-Control"] = "no-store"
    cached = _PENDING_APPROVALS_CACHE
    if cached is not None and (time.monotonic() - cached[0]) <= _PENDING_APPROVALS_TTL_S:
        return {"approvals": cached[1]}
    try:
        approvals = _list_pending_approvals()
    except Exception:  # noqa: BLE001 — fail-soft, never 5xx the panel
        logger.warning("pending-approvals listing failed", exc_info=True)
        return {"approvals": [], "degraded": True}
    _PENDING_APPROVALS_CACHE = (time.monotonic(), approvals)
    return {"approvals": approvals}
```

> Confirm the names `get_settings`, `get_repo`, `verify_token`, `Response`, `Depends`, `time`, `logger` are already imported/defined in `main.py` (they are, per `get_infra_graph`). Match the file's existing logger handle.

**Step 4: Run → PASS**

**Step 5: Commit**

```bash
git add agent/main.py tests/agent/test_pending_approvals_endpoint.py
git commit -m "feat(pending-approvals): GET /infra/pending-approvals (fail-soft, cached)"
```

---

## Task 3: Duplicate-adoption guard in `propose_adoption_tool`

**Files:**
- Modify: `agent/adk_tools.py:857-941` (`propose_adoption_tool`), after `render_adoption` and before `_open_iac_pr_and_notify`
- Test: `tests/agent/test_propose_adoption_dupe_guard.py`

**Step 1: Write the failing tests**

```python
# behavior:
# - an OPEN infra PR whose import_id == r.import_id → reject with that PR# in the reason
# - none open → proceeds to open the PR (existing happy path)
# - GitHub error while checking → FAIL-OPEN (proceed), so a hiccup never blocks provisioning
```

Implement tests by monkeypatching the open-PR lookup helper (below) to return a PR number / None / raise, and asserting the tool result `status` and that `_open_iac_pr_and_notify` is/ isn't called.

**Step 2: Run → FAIL**

**Step 3: Implement** — add a small shared lookup and call it in the tool:

```python
# agent/adk_tools.py
def find_open_adopt_pr_for_resource(asset_type: str, resource_name: str) -> int | None:
    """PR number of an OPEN driftscribe-infra PR already adopting
    ``(asset_type, resource_name)``, or None. Best-effort: any GitHub error
    returns None (fail-OPEN — the UI guard is the primary defense; never block
    provisioning on a probe failure). Matching on resource IDENTITY (not the raw
    import-id string) is the semantically-correct dedup: a second adoption of the
    same resource is exactly the dupe we refuse. Reuses the same issues-by-label
    listing + pure parser as the /infra/pending-approvals endpoint."""
    from driftscribe_lib.pending_approvals import build_pending_approval

    if not asset_type or not resource_name:
        return None
    try:
        s = get_settings()
        repo = get_repo(s.github_token or None, s.github_repo)
        for issue in repo.get_issues(state="open", labels=["driftscribe-infra"]):
            if getattr(issue, "pull_request", None) is None:
                continue
            entry = build_pending_approval(
                issue.number, issue.title or "", issue.html_url or "", issue.body or ""
            )
            if entry["asset_type"] == asset_type and entry["resource_name"] == resource_name:
                return issue.number
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("open-adopt-PR dupe check failed", exc_info=True)
    return None
```

> Confirm `agent/adk_tools.py` has a module `logger` (grep; add `logger = logging.getLogger(__name__)` if absent).

In `propose_adoption_tool`, right after the `conflict = preflight_conflicts(...)` block returns clean (so we only probe once the plan is otherwise valid):

```python
    from driftscribe_lib.pending_approvals import import_id_to_resource

    resolved = import_id_to_resource(r.import_id)
    if resolved is not None:
        existing_pr = find_open_adopt_pr_for_resource(*resolved)
        if existing_pr is not None:
            return {
                "status": "rejected",
                "reason": (
                    f"An adoption PR for this resource is already open: PR #{existing_pr}. "
                    f"Review and approve it at /iac-approvals/{existing_pr} instead of "
                    "opening a duplicate. (Opening a second PR for the same resource "
                    "would create a conflicting adoption.)"
                ),
            }
```

**Step 4: Run → PASS** (+ re-run existing `propose_adoption` tests so the happy path is unbroken)

**Step 5: Commit**

```bash
git add agent/adk_tools.py tests/agent/test_propose_adoption_dupe_guard.py
git commit -m "feat(provision): refuse duplicate adoption when an open PR already imports it"
```

---

## Task 4: Frontend types + client-side join helper

**Files:**
- Modify: `frontend/src/lib/types.ts` (add `PendingApproval`) and/or `frontend/src/lib/infra_graph.ts`
- Modify: `frontend/src/lib/infra_graph.ts` (add `findPendingPr`)
- Test: `frontend/tests/unit/infra_graph.test.ts` (extend) or new `pending_approvals.test.ts`

**Step 1: Write the failing tests**

```ts
import { describe, it, expect } from 'vitest';
import { findPendingPr } from '../../src/lib/infra_graph';

const APPROVALS = [
  { pr_number: 168, title: 'Adopt topic', url: 'u', asset_type: 'pubsub.googleapis.com/Topic', resource_name: 'adopt-probe-topic' },
  { pr_number: 171, title: 'Alerting', url: 'u', asset_type: '', resource_name: '' },
];

describe('findPendingPr', () => {
  it('matches a card row by asset_type + name', () => {
    expect(findPendingPr(APPROVALS, 'pubsub.googleapis.com/Topic', 'adopt-probe-topic')).toBe(168);
  });
  it('returns null when nothing matches', () => {
    expect(findPendingPr(APPROVALS, 'storage.googleapis.com/Bucket', 'x')).toBeNull();
  });
  it('never matches a resource-less (band-only) entry', () => {
    expect(findPendingPr(APPROVALS, '', '')).toBeNull();
  });
  it('is blank-name safe', () => {
    expect(findPendingPr(APPROVALS, 'pubsub.googleapis.com/Topic', '')).toBeNull();
  });
});
```

**Step 2: Run → FAIL**

**Step 3: Implement**

```ts
// frontend/src/lib/infra_graph.ts
export interface PendingApproval {
  pr_number: number;
  title: string;
  url: string;
  asset_type: string;
  resource_name: string;
}

/** PR number of an open adoption PR matching this resource row, or null.
 *  Joins on (asset_type, short name). Guards against the resource-less
 *  band-only entries (blank asset_type/name never match a real row). */
export function findPendingPr(
  approvals: PendingApproval[] | null | undefined,
  assetType: string,
  name: string,
): number | null {
  if (!approvals || !assetType || !name) return null;
  const target = shortName(name);
  for (const a of approvals) {
    if (a.asset_type && a.resource_name && a.asset_type === assetType
        && shortName(a.resource_name) === target) {
      return a.pr_number;
    }
  }
  return null;
}
```

> `shortName` already exists in `infra_graph.ts:249` (un-export → export it, or keep the helper local and reuse). Confirm whether `AdoptRow`/`ResourceCard` expose `name` + `assetType` per row; if the row name lives under a nested field, adjust the call site in Task 5 accordingly.

**Step 4: Run → PASS**

**Step 5: Commit**

```bash
git add frontend/src/lib/infra_graph.ts frontend/tests/unit/*.test.ts
git commit -m "feat(infra-panel): findPendingPr join helper + PendingApproval type"
```

---

## Task 5: InfraDiagram — card swap, panel band, legend + hover copy

**Files:**
- Modify: `frontend/src/components/InfraDiagram.svelte`
- Test: `frontend/tests/unit/InfraDiagram.test.ts` (extend; or the existing component test file)

**Step 1: Write the failing component tests** (vitest + @testing-library/svelte)

Pending approvals are fetched INSIDE the component via the existing `call` prop
(finding 7), so the test stubs `call` to answer both paths. Mirror how the
existing InfraDiagram tests stub `call` for `/infra/graph`.

```ts
// Stub `call` so:
//   /infra/graph             → a graph with an adoptable `adopt-probe-topic` node
//   /infra/pending-approvals → { approvals: [{ pr_number: 168, title: 'Adopt topic',
//        url: 'u', asset_type: 'pubsub.googleapis.com/Topic',
//        resource_name: 'adopt-probe-topic' }] }
// After the panel opens + fetches settle (await tick / findBy*):
//  - the card shows a link "Review pending adoption (PR #168) →" → href /iac-approvals/168
//  - the card has NO "Adopt into IaC" button (data-testid="card-adopt-btn" absent)
//  - a faint "PR open" tag is present (data-testid="card-pending-tag")
//  - the panel band data-testid="pending-approvals-band" shows "Open infra changes (1)"
//    and a link to /iac-approvals/168
//  - the legend includes an "Open PR" entry (data-testid="legend-pending")
// And when /infra/pending-approvals → { approvals: [] } (or rejects):
//    no band, Adopt button present as before (degrades silently).
```

**Step 2: Run → FAIL**

**Step 3: Implement**

1. Add internal state + a self-contained fetch (NOT a prop — finding 7; the
   component already owns `call` + the `RefreshScheduler`):

   ```svelte
   let pendingApprovals = $state<PendingApproval[]>([]);
   let pendingRun = 0; // independent guard, like overlayRun

   async function fetchPending(): Promise<void> {
     const mine = ++pendingRun;
     try {
       const resp = await call('/infra/pending-approvals');
       const body = await resp.json();
       if (mine !== pendingRun) return; // a newer fetch won
       pendingApprovals = Array.isArray(body?.approvals) ? body.approvals : [];
     } catch {
       if (mine === pendingRun) pendingApprovals = []; // degrade silently
     }
   }
   ```

   Call `void fetchPending()` where the graph is first loaded AND on each
   `RefreshScheduler` tick — but FIRE-AND-FORGET, never `await`-ed before the
   graph, so the fast (cached, ~ms) pending list is not blocked by the 10-30s
   CAI graph fetch. (Cards/band stay in sync with graph refreshes this way.)
2. Import `findPendingPr` + `PendingApproval` (from `../lib/infra_graph`) and
   `iacApprovalHref` (from `../lib/approval`).
3. **Card row** (`InfraDiagram.svelte:641-651`): wrap the adopt branch —

```svelte
{:else if row.adoptable}
  {@const pendingPr = findPendingPr(pendingApprovals, card.assetType, row.name)}
  {#if pendingPr}
    <a class="card-pending-link" data-testid="card-pending-link"
       href={iacApprovalHref(pendingPr)} target="_blank" rel="noopener"
    >Review pending adoption (PR #{pendingPr}) →</a>
    <span class="card-pending-tag" data-testid="card-pending-tag">PR open</span>
  {:else}
    <button data-testid="card-adopt-btn" ...>Adopt into IaC</button>
  {/if}
```

   Also suppress the "Start here" chip when `pendingPr` is set (the row is no longer the next action).
4. **Panel band** — near the top of the panel (after the hero / before the card grid):

```svelte
{#if pendingApprovals.length > 0}
  <section class="pending-band" data-testid="pending-approvals-band" aria-label="Open infrastructure changes">
    <h3 class="pending-band__title">Open infra changes ({pendingApprovals.length})</h3>
    <ul class="pending-band__list">
      {#each pendingApprovals as a (a.pr_number)}
        {@const href = iacApprovalHref(a.pr_number)}
        {#if href}
          <!-- guard: only render when pr_number yields a valid same-origin path
               (iacApprovalHref returns null for non-positive-int) — no null-href
               anchors, no "PR #NaN" copy (Codex finding 6). -->
          <li>
            <a {href} target="_blank" rel="noopener">PR #{a.pr_number} →</a>
            <span class="pending-band__pr-title">{a.title}</span>
          </li>
        {/if}
      {/each}
    </ul>
  </section>
{/if}
```

5. **Legend** — add a swatch entry (`data-testid="legend-pending"`) using the existing blue stream accent (`--ds-stream`), labeled "Open PR". Add a `pending` arm to `dotClass` if the node dot should also tint (optional; the card link is the primary cue).
6. **`LEGEND_HELP` copy** — append one sentence, matching the de-AI voice (colons, no em dashes), e.g.:
   `' A blue marker means an adoption PR is already open for that resource: open it from the card or the band at the top to review and approve, instead of adopting it again.'`
7. Scope the new CSS (`.pending-band`, `.card-pending-link`, `.card-pending-tag`) to the component, reusing `--ds-stream*` / `--ds-warn*` tokens. The band reads as an attention surface (warn-tinted), the card link as the stream-ink interactive affordance.

**Step 4: Run → PASS** (`cd frontend && npm run test -- InfraDiagram`)

**Step 5: Commit**

```bash
git add frontend/src/components/InfraDiagram.svelte frontend/tests/unit/InfraDiagram.test.ts
git commit -m "feat(infra-panel): pending-approval card link (replaces Adopt) + band + legend"
```

---

## Task 6: App.svelte — no change required

Because the fetch was colocated inside InfraDiagram (Task 5, finding 7), **App.svelte
needs no changes**: it already passes `{call}` to `<InfraDiagram>` (`App.svelte:750`),
which is all the component needs. No new parent state, no second refresh path.

Sanity-check only: `grep` confirms `InfraDiagram` receives `call` and that nothing
else needs `pendingApprovals` at the App level (the data is panel-local). If a future
surface (e.g. a header badge) needs the count, lift it via an `onPending` callback
mirroring the existing `onGraph` — out of scope here.

---

## Task 7: Full verification + docs

**Step 1:** Backend suite: `uv run pytest -q` → all green.
**Step 2:** Frontend: `cd frontend && npm run test && npm run check && npm run build` → green.
**Step 3:** Smoke tests: per the project recipe (`npm run smoke` / Playwright; restart uvicorn after build — manifest cache).
**Step 4:** Live probe (after deploy) — `/infra/pending-approvals` must list #168:

```bash
TOKEN=$(gcloud secrets versions access latest --secret=coordinator-shared-token)
BASE="https://driftscribe-agent-u272wv52kq-an.a.run.app"
curl -s -H "X-DriftScribe-Token: $TOKEN" "$BASE/infra/pending-approvals" | python3 -m json.tool
# expect an entry: pr_number 168, asset_type pubsub.googleapis.com/Topic, resource_name adopt-probe-topic
```

**Step 5:** Docs — add a one-line note to `docs/OVERVIEW.md` (Infra panel section) and, if read-endpoints are enumerated anywhere operator-facing, list `/infra/pending-approvals`. Update `agent/capabilities.py` only if it enumerates read endpoints.
**Step 6:** Deploy per the project recipe ([[coordinator_deploy_traffic_pinning]], [[live-probe-recipes]]): pristine worktree build, tag → digest → rev verify, then shift traffic. Verify the live SPA bundle carries the band + card link.

---

## Codex review — resolutions (thread `019f142a`)

**Folded in:**
- **(1)** Parser depends only on `adopt_recipe` (`_ID_SHAPES` + `_RTYPE_TO_ASSET_TYPE`), not `infra_graph` — no display-module coupling, no cycle. ✓
- **(4)** Lister uses `get_issues(state="open", labels=[driftscribe-infra])` (server-side filter) + `.pull_request` test — no per-PR `get_pull`, cheaper, scalable. ✓
- **(7)** Pending fetch colocated in `InfraDiagram` (it owns `call` + `RefreshScheduler`); App.svelte unchanged. ✓
- **(6)** Band entries guarded on a non-null `iacApprovalHref(pr_number)`; approve links built only from numeric `pr_number`, never a PR-supplied `url`. ✓
- **(2)** Trusted-label assumption documented; forged-line worst case is a fail-safe false refusal; hidden marker noted as future hardening. ✓
- **(5)** Dupe-guard stays fail-OPEN on GitHub error (matches Codex; never block provisioning on availability). ✓
- **(3)** `.tf`-cache approach rejected (needs head_sha, extra calls, dup of approval-source machinery) — body parse kept. ✓
- **(6, persist)** GitHub-listing v1 confirmed over persisting a resource→PR index (retroactive for #168; persist only if this surface becomes central or rate limits bite). ✓

**Resolved with the user:** band header = honest-neutral **"Open infra changes (N)"** (no `/decisions` cross-ref). Legend swatch = "Open PR". No open decisions remain. The post-implementation Codex follow-up (per global workflow, same thread `019f142a`) reviews the finished work against this plan.
