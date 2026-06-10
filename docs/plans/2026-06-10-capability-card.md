# Capability Card — "what this agent can and cannot do" Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** A `GET /capabilities` endpoint that serializes the agent's safety cage **from the same constants the enforcement code imports**, plus a collapsible SPA panel that renders it in plain language for ClickOps-to-IaC operators (roadmap Wave 1 item 3, anxiety B: "will the AI run rampage?").

**Architecture:** A new pure module `agent/capabilities.py` builds the DTO from `TOOL_REGISTRY` / `WORKER_REGISTRY` / `ACTION_REGISTRY` / the workload YAML manifests (parsed by the registry's own parse path) / `agent.fanout.MUTATION_TOOL_NAMES` / a new `RULE_DESCRIPTIONS` mapping in `driftscribe_lib/iac_plan_denylist.py`. Every hand-written description is **pinned to its enforcement constant by a test** so a new tool/rule/gated action cannot ship without operator-facing copy (CI fails). The SPA gets a `CapabilityCard.svelte` panel mirroring `InfraDiagram`'s collapsed-`<details>` + injected-`call` conventions.

**Tech stack:** FastAPI route (token-guarded like `/infra/graph`), pydantic `WorkloadSpec` parse, Svelte 5 + Vitest + @testing-library/svelte.

**What this is NOT (YAGNI):** no live `testIamPermissions` probes (roadmap phase 2), no per-worker IAM role enumeration (those live only in shell scripts — serializing them would be a hand-maintained copy that drifts; we serve one honest `iam_note` sentence instead), no edges into the infra graph, no polling (data is static per deploy).

---

## Grounding facts (verified 2026-06-10 — do not re-derive, they are correct)

- `agent/workloads/registry.py`: `TOOL_REGISTRY` (16 keys, `MappingProxyType`, two reserved entries are `None`: `get_session_state`, `set_session_state`) at line ~400; `WORKER_REGISTRY` (8 keys) ~444; `ACTION_REGISTRY` (6 entries, `ActionSpec(name, display_name, requires_approval)`) ~467; `_load_from_path` parses YAML via `yaml.safe_load` + `WorkloadSpec.model_validate` then resolves tools/workers/actions **reading worker-URL env vars** (so full resolution is NOT test-friendly); `_workload_yaml_path(name)` (path-traversal-guarded) ~836.
- `agent/workloads/spec.py`: `WorkloadSpec.name: Literal["drift", "upgrade", "explore", "provision"]` — the closed workload set. `observation_kind: Literal["cloud_run_env", "repo_lockfile", "none"]`; `"none"` marks chat-only workloads. Fields: `display_name`, `description`, `enabled_tool_names`, `worker_names`, `action_names`.
- `agent/main.py:1024`: `CHAT_ONLY_WORKLOAD_NAMES: frozenset[str] = frozenset({"explore", "provision"})` — enforcement truth for chat-only (route-refusal of `/recheck`).
- `agent/fanout.py:258`: `MUTATION_TOOL_NAMES: frozenset[str]` — 8 symbolic names; **semantics are "writes OR rides a write-capable credential"** (`notify` / `search_recent_prs` are in it for credential containment). Tool descriptions must stay honest about this distinction.
- `driftscribe_lib/iac_plan_denylist.py`: 14 rule IDs exist **only as string literals** — first argument of `Violation(...)` constructions inside `load_plan_json` and `evaluate`. The docstring (lines 30–59) lists all 14. `__all__ = ["Violation", "DenylistInput", "load_plan_json", "evaluate"]`.
- Denylist enforcement call sites (for the `enforced_at` copy): plan-builder CI (C2), `GET /iac-approvals/{pr_number}` advisory (`agent/iac_artifacts.py` `load_plan_view`), `workers/tofu_apply/main.py` immediately before apply (final gate).
- Human gates: `ACTION_REGISTRY["rollback"].requires_approval is True` (only one today); rollback approval = single-use, 15-min TTL, HMAC-bound (`driftscribe_lib/approvals.py`), route `/approvals/{approval_id}`; IaC apply = plan-bound HMAC `PlanApproval` with signed expiry window, route `/iac-approvals/{pr_number}` (POST requires `require_cf_operator`).
- Route pattern to mirror: `agent/main.py:1736` `GET /infra/graph` — `Depends(verify_token)`, sets `Cache-Control: no-store`. Route tests to mirror: `tests/integration/test_infra_graph_endpoint.py` (NOT `tests/unit/test_infra_graph.py`, which is a pure graph-builder test). `verify_token` returns **503** when `DRIFTSCRIBE_TOKEN` is unset; real-auth tests need the env var + `get_settings.cache_clear()` + `@pytest.mark.no_auth_override`.
- Frontend: `App.svelte` line ~356 mounts `<InfraDiagram {call} {appliedEpoch} />`; `call: (path: string, init?: RequestInit) => Promise<Response>` is the token-aware fetch wrapper prop. Components are self-styled (scoped `<style>`, design tokens: `--ds-fg`, `--ds-muted`, `--ds-fs-1/2`, `--ds-sp-2/4`, `--ds-radius-sm`, `--ds-neutral-surface`, `--ds-border-strong`, `--ds-ok-surface`). **`base.css` must NOT grow SPA-only classes** (shared with strict-CSP Jinja approval pages).
- Component tests: jsdom keeps closed-`<details>` content in the DOM; `Response` is a global in the vitest jsdom env; stub `call` with a closure recording paths. jsdom does **not** reliably toggle `<details>` from a summary click — tests set `detailsEl.open = true` then `await fireEvent(detailsEl, new Event('toggle'))`.
- **Svelte 5 whitespace gotcha (learned on PR #83):** Svelte trims literal leading whitespace at `{#if}`/`{:else}` block boundaries EVEN inline. Any conditional inline text needing a leading space must use an explicit `{' '}` expression tag, and a glued-exact-string test must pin it.
- Backend tooling: `.venv/bin/python -m pytest -q`, `.venv/bin/python -m ruff check .`. Frontend: `cd frontend && npm test -- --run`, `npx svelte-check`, `npm run build`.

---

## DTO contract (version 1)

```jsonc
{
  "version": 1,
  "provenance": "Generated from the same constants the enforcement code imports — not hand-written documentation.",
  "iam_note": "Each worker runs as its own service account with least-privilege IAM, codified in infra/scripts/. The only identity that can change live infrastructure is the apply worker's service account — and only after an operator approves the exact plan.",
  "workloads": [
    {
      "name": "drift",
      "display_name": "…from workload.yaml…",
      "description": "…from workload.yaml…",
      "autonomous": true,                  // observation_kind != "none"
      "tools": [
        // "write_capable" mirrors MUTATION_TOOL_NAMES semantics exactly:
        // "writes OR rides a write-capable credential" — NOT "mutates infra".
        { "name": "drift_read_live_env", "description": "…", "write_capable": false }
      ],
      "workers": [ { "name": "drift_reader", "description": "…" } ],
      "actions": [
        { "name": "rollback", "display_name": "Rollback (HITL)", "requires_approval": true }
      ]
    }
  ],
  "human_gates": [
    // "method" makes the route pin method-bearing: the gate is the POST
    // (the GET is just the form page). See Task 3's anti-drift test.
    { "id": "iac_apply", "title": "…", "description": "…", "route": "/iac-approvals/{pr_number}", "method": "POST" },
    { "id": "rollback", "title": "…", "description": "…", "route": "/approvals/{approval_id}", "method": "POST" }
  ],
  "denylist": {
    "summary": "Before any apply, the plan is checked against a fail-closed denylist. A violation blocks the apply — operator approval cannot override it.",
    "enforced_at": [
      "the trusted plan-builder CI, before a plan is ever stored",
      "the approval page, as an advisory check before you approve",
      "the tofu-apply worker, immediately before apply (final gate)"
    ],
    "rules": [ { "id": "control-plane-service", "description": "…", "category": "control-plane" } ]
  }
}
```

Ordering is deterministic: workloads in `WorkloadSpec.name` Literal order; tools/workers/actions in manifest order; rules sorted by `(CATEGORY_ORDER.index(category), id)` with `CATEGORY_ORDER = ("control-plane", "iam", "global-v1", "structural")`.

---

### Task 1: `RULE_DESCRIPTIONS` in the denylist lib + AST drift-pin test

**Files:**
- Modify: `driftscribe_lib/iac_plan_denylist.py` (additive only — NO enforcement-code changes)
- Test: `tests/unit/test_denylist_rule_descriptions.py` (new)

**Step 1: Write the failing tests**

```python
"""RULE_DESCRIPTIONS ↔ enforcement drift pin.

The capability card serves operator-facing copy for every denylist rule.
The rule IDs exist only as string literals — the first argument of every
``Violation(...)`` construction in ``driftscribe_lib.iac_plan_denylist``.
This module extracts those literals via AST so a 15th rule cannot ship
without a description (and a deleted rule cannot leave a stale one).
"""
from __future__ import annotations

import ast
import inspect

import driftscribe_lib.iac_plan_denylist as denylist_mod
from driftscribe_lib.iac_plan_denylist import RULE_DESCRIPTIONS


def _emitted_rule_ids() -> set[str]:
    """Every first arg to Violation(...) — FAIL LOUDLY on any non-literal.

    Codex review (2026-06-10): an earlier draft silently skipped calls whose
    first arg wasn't a string literal, so ``Violation(rule_id, ...)`` (a
    dynamic 15th rule) could ship with no description and the pin would
    still pass. Every ``Violation(...)`` call site MUST pass a string
    literal (or keyword ``rule="..."`` literal) — anything else fails this
    scan, which is the correct outcome: rewrite the call site as a literal
    or extend this scanner deliberately.
    """
    tree = ast.parse(inspect.getsource(denylist_mod))
    ids: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Violation"
        ):
            continue
        first = node.args[0] if node.args else next(
            (kw.value for kw in node.keywords if kw.arg == "rule"), None
        )
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"Violation(...) at line {node.lineno} does not pass its rule id "
            f"as a string literal — the RULE_DESCRIPTIONS drift pin cannot "
            f"see it. Use a literal."
        )
        ids.add(first.value)
    return ids


def test_every_emitted_rule_has_a_description_and_no_stale_ones():
    emitted = _emitted_rule_ids()
    assert emitted, "AST scan found no Violation(...) literals — scan is broken"
    assert set(RULE_DESCRIPTIONS) == emitted


def test_there_are_exactly_fourteen_rules():
    # The docstring promises 14 rule IDs; pin it so the AST scan can't
    # silently degrade (e.g. a refactor wrapping Violation in a helper).
    assert len(RULE_DESCRIPTIONS) == 14


def test_descriptions_are_operator_grade():
    for rule_id, text in RULE_DESCRIPTIONS.items():
        assert text and text[0].isupper() and len(text) >= 20, rule_id
        # No raw jargon the audience can't parse:
        assert "tuple" not in text.lower(), rule_id
```

**Step 2: Run to verify failure** — `.venv/bin/python -m pytest tests/unit/test_denylist_rule_descriptions.py -q` → ImportError on `RULE_DESCRIPTIONS`.

**Step 3: Implement** — append to `driftscribe_lib/iac_plan_denylist.py` (after the action-tuple constants; add `MappingProxyType`/`Mapping`/`Final` imports as needed; extend `__all__` with `"RULE_DESCRIPTIONS"`):

```python
# Operator-facing descriptions for every rule ID this module can emit.
# Serialized by the coordinator's GET /capabilities (the operator UI's
# capability card). Keyed by the EXACT Violation(...) first-arg literals —
# tests/unit/test_denylist_rule_descriptions.py extracts those literals via
# AST and pins set equality, so adding/removing a rule without updating
# this mapping fails CI. Descriptions are plain language for operators who
# do not read HCL; keep them honest and specific.
RULE_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType({
    "plan-json-unparseable": (
        "The plan file is not valid JSON — rejected outright (fail-closed)."
    ),
    "plan-json-missing-resource-changes": (
        "The plan has no resource-changes list — rejected outright (fail-closed)."
    ),
    "plan-json-malformed-change": (
        "A change entry is malformed, or a protected resource hides its "
        "identity — rejected outright (fail-closed)."
    ),
    "control-plane-service": (
        "No change may touch DriftScribe's own Cloud Run services."
    ),
    "control-plane-sa": (
        "No change may touch DriftScribe's own service accounts."
    ),
    "control-plane-bucket": (
        "No change may touch the IaC state or artifact buckets, or any "
        "object inside them."
    ),
    "control-plane-secret": (
        "No change may touch DriftScribe's secrets (approval keys, GitHub "
        "token, …) or any of their versions."
    ),
    "control-plane-kms": (
        "No change may touch the state-encryption KMS key or its key ring."
    ),
    "wif-config-change": (
        "No change may touch Workload Identity Federation pools or providers."
    ),
    "iam-change-forbidden-v1": (
        "All IAM changes are refused — even on unrelated resources (v1 floor)."
    ),
    "delete-action-forbidden-v1": (
        "All deletes are refused — the agent cannot destroy any resource "
        "(v1 floor)."
    ),
    "forget-action-forbidden-v1": (
        "All state-forget actions are refused (v1 floor)."
    ),
    "replace-action-forbidden-v1": (
        "All replacements (destroy-and-recreate) are refused (v1 floor)."
    ),
    "unknown-action-forbidden-v1": (
        "Any action shape not in the audited OpenTofu vocabulary is refused "
        "(fail-closed against new verbs)."
    ),
})
```

**Step 4: Extend the CLI shim** — `tools/iac_plan_denylist.py` re-exports the lib's public surface; add `RULE_DESCRIPTIONS` to its re-export imports for consistency (the shim has no `__all__` today — do not introduce one for this).

**Step 5: Run tests + full denylist suite** — new file green AND `.venv/bin/python -m pytest tests/unit -k denylist -q` green (proves no enforcement change).

**Step 6: Commit** — `feat(lib): RULE_DESCRIPTIONS — operator copy pinned to denylist rule literals via AST scan`

---

### Task 2: `agent/capabilities.py` — catalogs, gates, builder + pin tests

**Files:**
- Create: `agent/capabilities.py`
- Modify: `agent/workloads/registry.py` (extract `_parse_spec`, add public `load_workload_spec` — pure refactor of `_load_from_path`'s first lines)
- Test: `tests/unit/test_capabilities.py` (new)

**Step 1: registry refactor (TDD — Codex must-fix #1: parse-only would bypass symbol enforcement).** A naive `model_validate`-only loader could describe a manifest that `load_workload()` would REFUSE (unknown tool, reserved `None` tool, unknown worker/action) — `/capabilities` must never advertise capabilities the loader rejects. In `registry.py`, extract from `_load_from_path`:

```python
def _parse_spec(yaml_path: Path, *, expected_name: str | None = None) -> WorkloadSpec:
    """Parse a workload manifest and validate its SYMBOLS against the
    code-side allowlists, WITHOUT resolving worker URLs (no env reads).

    Shared by ``_load_from_path`` (which additionally resolves workers
    from env) and ``load_workload_spec`` (the GET /capabilities
    serializer, which must work wherever worker URLs are unset). Raises
    exactly what full resolution would raise for a bad symbol:
    UnknownToolError / ReservedToolNotImplementedError (via
    ``_resolve_tool``), UnknownWorkerError (membership check ONLY — no
    env), UnknownActionError (via ``_resolve_action``)."""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkloadSpec.model_validate(raw)
    if expected_name is not None and spec.name != expected_name:
        raise WorkloadManifestMismatchError(...)  # move the existing message here verbatim
    for tool_name in spec.enabled_tool_names:
        _resolve_tool(tool_name)          # env-free; raises on unknown/reserved
    for worker_name in spec.worker_names:
        if worker_name not in WORKER_REGISTRY:   # membership only — _resolve_worker reads env
            raise UnknownWorkerError(...)        # reuse _resolve_worker's message shape
    for action_name in spec.action_names:
        _resolve_action(action_name)      # env-free; raises on unknown
    return spec


def load_workload_spec(name: str) -> WorkloadSpec:
    """Public parse+symbol-validate loader (same path-traversal guard +
    name-match validation as load_workload; never reads env)."""
    return _parse_spec(_workload_yaml_path(name), expected_name=name)
```

`_load_from_path` then calls `_parse_spec(yaml_path, expected_name=expected_name)` and drops its own parse/name-match lines (the resolution maps it builds stay as-is — `_resolve_tool`/`_resolve_action` are idempotent, the double call is fine and keeps the diff minimal). Re-export `load_workload_spec` from `agent/workloads/__init__.py` (import + `__all__`).

Tests FIRST (in `tests/unit/test_capabilities.py` or alongside the existing registry tests — follow where `_load_from_path` error-injection tests live): temp-manifest cases for (a) unknown tool → `UnknownToolError`, (b) reserved tool (`get_session_state`) → `ReservedToolNotImplementedError`, (c) unknown worker → `UnknownWorkerError` **with all worker-URL env vars deleted** (proves no env read), (d) unknown action → `UnknownActionError`, (e) happy path returns the spec with all env vars deleted. Then run the full registry/workload suite — zero behavior change expected.

**Step 2: Write the failing tests for `agent/capabilities.py`.** Cover at minimum:

```python
# Drift pins (the load-bearing tests of this feature):
def test_tool_descriptions_cover_exactly_the_tool_registry():
    assert set(TOOL_DESCRIPTIONS) == set(TOOL_REGISTRY)

def test_worker_descriptions_cover_exactly_the_worker_registry():
    assert set(WORKER_DESCRIPTIONS) == set(WORKER_REGISTRY)

def test_rule_categories_cover_exactly_the_rule_descriptions():
    assert set(RULE_CATEGORIES) == set(RULE_DESCRIPTIONS)
    assert set(RULE_CATEGORIES.values()) <= set(CATEGORY_ORDER)

def test_every_approval_gated_action_has_a_human_gate():
    gated = {n for n, s in ACTION_REGISTRY.items() if s.requires_approval}
    assert gated <= {g["id"] for g in HUMAN_GATES}

def test_chat_only_coherence_with_main():
    # observation_kind == "none" (declarative) must equal main's
    # CHAT_ONLY_WORKLOAD_NAMES (enforcement: /recheck route-refusal).
    from agent.main import CHAT_ONLY_WORKLOAD_NAMES
    declared = {
        n for n in WORKLOAD_NAMES
        if load_workload_spec(n).observation_kind == "none"
    }
    assert declared == set(CHAT_ONLY_WORKLOAD_NAMES)

# Builder shape:
def test_build_capabilities_shape():
    dto = build_capabilities()
    assert dto["version"] == 1
    assert [w["name"] for w in dto["workloads"]] == list(WORKLOAD_NAMES)
    prov = next(w for w in dto["workloads"] if w["name"] == "provision")
    assert prov["autonomous"] is False
    open_pr = next(t for t in prov["tools"] if t["name"] == "provision_open_infra_pr")
    assert open_pr["write_capable"] is True
    read_env = next(t for t in prov["tools"] if t["name"] == "drift_read_live_env")
    assert read_env["write_capable"] is False
    assert {g["id"] for g in dto["human_gates"]} == {"iac_apply", "rollback"}
    assert len(dto["denylist"]["rules"]) == 14
    # Codex review: pin the FULL promised sort, not just category grouping.
    rules = dto["denylist"]["rules"]
    assert rules == sorted(
        rules, key=lambda r: (CATEGORY_ORDER.index(r["category"]), r["id"])
    )

def test_build_capabilities_is_json_serializable_and_env_free(monkeypatch):
    # Must not require worker URL env vars (unlike load_workload). Codex
    # review: derive the list from WORKER_REGISTRY so a future worker's
    # env var cannot be missed by this test.
    for spec in WORKER_REGISTRY.values():
        monkeypatch.delenv(spec.url_env, raising=False)
    json.dumps(build_capabilities())
```

**Step 3: Implement `agent/capabilities.py`.** Module docstring explains the "same constants as enforcement" invariant and points at the pin tests. Contents:

- `WORKLOAD_NAMES: Final[tuple[str, ...]] = get_args(WorkloadSpec.model_fields["name"].annotation)` — the Literal IS the enumeration; never hand-list.
- `TOOL_DESCRIPTIONS: Final[Mapping[str, str]]` — 16 entries (one per `TOOL_REGISTRY` key). Honest copy; mutation-set members that exist for credential containment must say so. Required nuances:
  - `drift_propose_rollback`: "Proposes a rollback — never executes one; it creates an approval that waits for an operator."
  - `notify`: "Sends a notification via the notifier worker (counted as write-capable because it rides a sending credential)."
  - `search_recent_prs`: "Searches the target repo's recent pull requests (counted as write-capable because it rides a repo credential)."
  - `provision_open_infra_pr`: "Authors OpenTofu files under iac/ and opens ONE pull request — never applies anything; applying happens only through the gated approve-then-apply pipeline."
  - `upgrade_merge_pr`: "Merges an upgrade PR this agent opened — only after CI is green on the exact head commit; fails closed."
  - Reserved (`get_session_state` / `set_session_state`): "Reserved — not implemented; no workload can use it." (serializer must still work if a manifest never references them — they simply don't appear in any workload's `tools`).
- `WORKER_DESCRIPTIONS: Final[Mapping[str, str]]` — 8 entries; `infra_reader` must say "read-only by IAM (asset viewer only)"; `tofu_editor` must say "writes iac/-only files and opens PRs; never touches live infrastructure"; `drift_rollback` must say "refuses anything without a valid operator approval token".
- `CATEGORY_ORDER: Final[tuple[str, ...]] = ("control-plane", "iam", "global-v1", "structural")` and `RULE_CATEGORIES: Final[Mapping[str, str]]` (structural ×3 `plan-json-*`; control-plane ×5; iam ×2 `iam-change-forbidden-v1` + `wif-config-change`; global-v1 ×4 delete/forget/replace/unknown).
- `HUMAN_GATES: Final[tuple[Mapping[str, str], ...]]` — the two gates with `id`/`title`/`description`/`route`/`method` exactly as in the DTO contract section. Descriptions must mention: plan-bound HMAC + expiry (iac_apply); single-use + 15-minute TTL + worker-side re-verification (rollback).
- `build_capabilities() -> dict` — assembles the DTO: for each workload name, `load_workload_spec(name)`; tools from `enabled_tool_names` with `write_capable = name in MUTATION_TOOL_NAMES` (import from `agent.fanout`; the field name deliberately mirrors that set's "writes OR rides a write-capable credential" semantics — Codex review flagged `writes` as misleading for `notify`/`search_recent_prs`); workers from `worker_names`; actions from `action_names` via `ACTION_REGISTRY` (use `dataclasses.asdict`); `autonomous = spec.observation_kind != "none"`; denylist rules sorted by `(CATEGORY_ORDER.index(cat), rule_id)`. Returns plain dicts/lists (JSON-serializable).
- Import direction: `capabilities` imports from `workloads.registry`, `workloads.spec`, `fanout`, `driftscribe_lib.iac_plan_denylist`. It must NOT import `agent.main` (main imports capabilities — the coherence test imports main from the test side only).

**Step 4: Run** — new tests green; `.venv/bin/python -m pytest tests/unit -k "registry or workload or capabilities or fanout" -q` green.

**Step 5: Commit** — `feat(agent): capabilities catalog — registry-pinned descriptions, gates, builder`

---

### Task 3: `GET /capabilities` route + route tests

**Files:**
- Modify: `agent/main.py` (route next to `GET /infra/graph`, ~line 1736 block)
- Test: `tests/integration/test_capabilities_endpoint.py` (new — mirror `tests/integration/test_infra_graph_endpoint.py` EXACTLY: that is the real route-test precedent. `tests/unit/test_infra_graph.py` is a pure graph-builder test — Codex must-fix #3 corrected an earlier draft that pointed there.)

**Step 1: Failing tests.** Auth semantics to honor (from `agent/auth.py::verify_token` + the precedent file): token env UNSET → **503**, not 401; the 401 test needs `monkeypatch.setenv("DRIFTSCRIBE_TOKEN", …)` + `get_settings.cache_clear()` + the `@pytest.mark.no_auth_override` marker (the suite's default fixture overrides auth otherwise). Cases:

```python
@pytest.mark.no_auth_override
def test_capabilities_requires_token(...):
    # DRIFTSCRIBE_TOKEN set, request WITHOUT header → 401
    # request with WRONG token → 403  (mirror precedent's case list)

def test_capabilities_ok(...):
    r = client.get("/capabilities")  # default auth-overridden client
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    dto = r.json()
    assert dto["version"] == 1
    assert {w["name"] for w in dto["workloads"]} == {"drift", "upgrade", "explore", "provision"}

def test_gate_routes_exist_with_method_and_guard():
    """Anti-drift, method-bearing (Codex must-fix #4): the gate is the POST —
    a path-only pin would still pass via the unauthenticated GET form page
    even if the gated POST disappeared."""
    from fastapi.routing import APIRoute
    from agent.capabilities import HUMAN_GATES
    from agent.main import app
    routes = {(r.path, m): r for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    for gate in HUMAN_GATES:
        assert (gate["route"], gate["method"]) in routes, gate["id"]
    # The IaC-apply POST must carry the operator-identity guard:
    iac_post = routes[("/iac-approvals/{pr_number}", "POST")]
    dep_names = {d.call.__name__ for d in iac_post.dependant.dependencies}
    assert "require_cf_operator" in dep_names
```

(If `require_cf_operator` is consumed as a parameter-level `Depends` rather than a route-level dependency, walk `iac_post.dependant` recursively — the implementer must verify against the real route and keep the assertion meaningful, not weaken it to a path check.)

**Step 2: Implement the route:**

```python
@app.get("/capabilities")
def get_capabilities_route(
    response: Response,
    _: None = Depends(verify_token),
) -> dict:
    """The agent's safety cage, serialized from the same constants the
    enforcement code imports (agent/capabilities.py — see its module
    docstring for the drift-pin test inventory). Token-guarded like
    /decisions and /infra/graph. Static per deploy; no-store keeps the
    header story consistent with its sibling read routes."""
    response.headers["Cache-Control"] = "no-store"
    return build_capabilities()
```

**Step 3–4: Run** route tests + `.venv/bin/python -m pytest tests/unit -q` (full unit suite) + `.venv/bin/python -m ruff check .`.

**Step 5: Commit** — `feat(api): GET /capabilities — token-guarded safety-cage DTO`

---

### Task 4: frontend types + display helpers (`frontend/src/lib/capabilities.ts`)

**Files:**
- Create: `frontend/src/lib/capabilities.ts`
- Test: `frontend/tests/unit/capabilities.test.ts` (new)

Types exactly mirroring the DTO (`Capabilities`, `CapWorkload`, `CapTool`, `CapWorker`, `CapAction`, `CapGate`, `CapRule`). One pure helper:

```ts
export const CATEGORY_HEADINGS: Record<CapRule['category'], string> = {
  'control-plane': 'Its own control plane is untouchable',
  iam: 'It cannot change who has access',
  'global-v1': 'It cannot destroy or replace anything',
  structural: 'Malformed plans are rejected outright',
};

/** Group rules by category, preserving server order within and across groups. */
export function groupRules(rules: CapRule[]): { category: CapRule['category']; heading: string; rules: CapRule[] }[]
```

Unknown-category behavior (Codex review asked for this to be explicit): a rule whose `category` is not in `CATEGORY_HEADINGS` is **rendered, never dropped** — appended as a trailing group whose heading is the raw category string. Safety information must not silently disappear because the server grew a category before the frontend learned its heading.

Tests: grouping preserves server order; unknown category → trailing group with raw-string heading containing its rules; empty input → `[]`.

**Commit** — `feat(ui): capabilities DTO types + rule grouping helper`

---

### Task 5: `CapabilityCard.svelte` + component tests

**Files:**
- Create: `frontend/src/components/CapabilityCard.svelte`
- Test: `frontend/tests/unit/CapabilityCard.test.ts` (new)

**Behavior:**
- Props: `{ call }` only (same signature as InfraDiagram's `call`).
- Collapsed `<details data-testid="capability-card">` whose `<summary>` reads **"What this agent can — and cannot — do"** with a muted hint span "safety cage, generated from enforcement code".
- Lazy fetch: nothing on mount; on first open (`ontoggle` reading `e.currentTarget.open`, mirroring InfraDiagram) fetch `GET /capabilities` ONCE and cache for the component's lifetime (static per deploy — no polling, no refresh button). Loading row while in flight; on non-OK or thrown fetch → error row `data-testid="cap-error"` with a Retry button (`data-testid="cap-retry"`) that re-runs the fetch.
- Render order (anxiety-first):
  1. `data-testid="cap-gates"` — heading **"Always needs your approval"**; one block per gate: title bold, description in plain prose. Then any `requires_approval` actions from workloads NOT already covered by a gate id are NOT re-listed (gates are the canonical list — keep it simple).
  2. `data-testid="cap-denylist"` — heading **"Blocked outright — approval cannot override these"**; `denylist.summary` paragraph; the `groupRules` groups as sub-lists (heading + per-rule description, rule `id` rendered as a muted `<code>` suffix for cross-referencing logs); a muted "checked at: …" line joining `enforced_at` with " → ".
  3. `data-testid="cap-workloads"` — heading **"What each workload can use"**; one nested `<details>` per workload: summary = `display_name` + a pill `autonomous` ? "autonomous + chat" : "chat-only", body = description, tools list (each: name as `<code>`, description, badge `write_capable` ? "write-capable" : "read") — the badge says "write-capable", NOT "write": for `notify`/`search_recent_prs` the set membership means "rides a write-capable credential", and the per-tool description carries that nuance — workers list, actions list with "needs approval" pill when `requires_approval`.
  4. Footer (muted): `iam_note`, then `provenance`.
- **Whitespace:** any inline conditional text adjacent to static text must use `{' '}` (PR #83 lesson) and a glued-exact-string assertion must pin at least one such seam (e.g. the workload pill seam).
- Styling: scoped `<style>` only, design tokens listed in Grounding; badges reuse the visual language of existing pills (look at `DecisionSummary.svelte` / `InfraDiagram.svelte` for pill styles — match, don't import).

**Tests (≥6):**
1. Renders collapsed with no fetch performed (paths array empty).
2. Open (`el.open = true; await fireEvent(el, new Event('toggle'))`) → fetches `/capabilities` exactly once; re-toggle does not refetch.
3. With a representative DTO fixture: gates section shows both gate titles; denylist section shows a control-plane rule description AND the category heading; workloads section shows all four display names; provision shows the chat-only pill (exact-string pin on the pill seam).
4. `write_capable` badge: `provision_open_infra_pr` row shows "write-capable", a read tool shows "read".
5. Fetch failure (500 Response) → cap-error visible; clicking cap-retry refetches (paths length 2) and renders on success.
6. Accessibility: gates/denylist/workloads sections are headed elements (use `getByRole('heading', { name: … })`).

**Commit** — `feat(ui): CapabilityCard panel — lazy, cached, anxiety-first sections`

---

### Task 6: Mount in `App.svelte` + integration test + full gates

**Files:**
- Modify: `frontend/src/App.svelte` (import + mount `<CapabilityCard {call} />` immediately after `<InfraDiagram {call} {appliedEpoch} />`, ~line 356)
- Test: extend `frontend/tests/unit/` with an App-level smoke OR (preferred, cheaper) a `CapabilityCard` mount assertion inside the existing App test file if one exists; if none exists, a 1-test file asserting App renders the capability-card testid (mirror however InfraDiagram is integration-tested today — investigate first; if InfraDiagram has no App-level test, match that precedent and skip the App test, relying on Task 5 + svelte-check).

**Gates (all must pass):**
1. `cd frontend && npm test -- --run` (full suite)
2. `npx svelte-check` (0 errors, 0 warnings)
3. `npm run build` (vite build OK)
4. `.venv/bin/python -m pytest -q` (FULL backend suite)
5. `.venv/bin/python -m ruff check .`

**Commit** — `feat(ui): mount CapabilityCard on the operator home page`

---

## Final review + ship

1. Final whole-branch reviewer subagent (diff `main...feat/capability-card`) against this spec.
2. PR → CI green → Codex completed-work review on the plan-review thread → autonomous squash-merge → coordinator rebake (`infra/cloudbuild.coordinator-update.yaml`, `_TAG=$(git rev-parse --short HEAD)`) → `update-traffic --to-revisions=<new>=100` (traffic is pinned) → live verify (bundle markers at `/static/*`, `/capabilities` 401 unauthenticated, 200 + shape via authenticated curl if a token is at hand).

## Post-review deltas (as shipped)

_To be filled after Codex review and implementation._
